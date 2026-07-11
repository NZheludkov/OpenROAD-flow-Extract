#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import pandas as pd


REQUIRED_OUTPUT_COLUMNS = [
    "design",
    "pdk_name",
    "config_id",
    "net_name",
    "fanin",
    "fanout",
    "fanin_area",
    "fanout_area",
    "hpwl",
    "cell_density",
    "pin_cap_ff",
    "gcell_area",
    "gcell_ar",
]


def find_run_dirs(dataset_path: Path) -> list[Path]:
    """
    A run directory is detected by config/config.tcl.
    """
    config_files = sorted(dataset_path.rglob("config/config.tcl"))
    return [cfg.parent.parent for cfg in config_files]


def find_postcts_def(run_dir: Path) -> Optional[Path]:
    candidates = [
        run_dir / "postcts" / "def" / "def.def",
        run_dir / "postcts" / "def" / "postcts.def",
        run_dir / "postcts" / "def" / f"{run_dir.parent.name}.def",
    ]

    for cand in candidates:
        if cand.exists():
            return cand

    postcts_dir = run_dir / "postcts"
    if not postcts_dir.exists():
        return None

    defs = sorted(postcts_dir.rglob("*.def"))
    if not defs:
        return None

    return max(defs, key=lambda p: p.stat().st_size)


def find_postcts_sdc(run_dir: Path) -> Optional[Path]:
    candidates = [
        run_dir / "postcts" / "sdc" / "sdc.sdc",
        run_dir / "postcts" / "sdc" / "postcts.sdc",
        run_dir / "postcts" / "sdc" / f"{run_dir.parent.name}.sdc",
    ]

    for cand in candidates:
        if cand.exists():
            return cand

    postcts_dir = run_dir / "postcts"
    if not postcts_dir.exists():
        return None

    sdcs = sorted(postcts_dir.rglob("*.sdc"))
    if not sdcs:
        return None

    return sdcs[0]


def make_output_run_dir(dataset_path: Path, out_dir: Path, run_dir: Path) -> Path:
    rel = run_dir.relative_to(dataset_path)
    return out_dir / rel


def output_is_complete(out_file: Path) -> bool:
    return out_file.exists() and out_file.stat().st_size > 0


def make_tcl_script(
    config_file: Path,
    def_file: Path,
    sdc_file: Path,
    feat_tcl: Path,
    csv_file: Path,
    tcl_file: Path,
    density_bin_um: float,
) -> None:
    """
    Generate OpenROAD Tcl script for one postCTS feature extraction run.
    """

    tcl = f"""
# ============================================================
# Auto-generated postCTS net feature extraction script
# ============================================================

source "{config_file}"

foreach lef $lef_list {{
    read_lef $lef
}}

read_def "{def_file}"

# Create timing corner
define_corners view

# Read Liberty
foreach lib $liberty {{
    read_liberty -corner view $lib
}}

# Set units from run config
set_cmd_units \\
    -time $liberty_time_unit \\
    -capacitance $liberty_cap_unit \\
    -current $liberty_current_unit \\
    -voltage $liberty_voltage_unit \\
    -resistance $liberty_res_unit \\
    -distance um

read_sdc "{sdc_file}"

# Path groups are not strictly required for feature extraction,
# but they initialize the timing environment similarly to the main flow.
catch {{group_path -name reg2reg -from [all_registers] -to [all_registers]}} group_msg
catch {{group_path -name in2reg  -from [all_inputs]    -to [all_registers]}} group_msg
catch {{group_path -name reg2out -from [all_registers] -to [all_outputs]}} group_msg
catch {{group_path -name in2out  -from [all_inputs]    -to [all_outputs]}} group_msg

# User feature extraction script
source "{feat_tcl}"

# Optional feature extractor parameter
set DENSITY_BIN_UM {density_bin_um}

# Extract features
extract_net_features_prects "{csv_file}"

exit
"""

    tcl_file.write_text(tcl)


def convert_csv_to_parquet(csv_file: Path, parquet_file: Path, keep_csv: bool) -> int:
    df = pd.read_csv(csv_file)

    missing_cols = [c for c in REQUIRED_OUTPUT_COLUMNS if c not in df.columns]
    if missing_cols:
        raise RuntimeError(f"Missing columns in CSV: {missing_cols}")

    df = df[REQUIRED_OUTPUT_COLUMNS]

    # Basic numeric coercion. This avoids object dtype for numeric columns.
    numeric_cols = [
        "fanin",
        "fanout",
        "fanin_area",
        "fanout_area",
        "hpwl",
        "cell_density",
        "pin_cap_ff",
        "gcell_area",
        "gcell_ar",
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    tmp_file = parquet_file.with_name(parquet_file.name + ".tmp")

    df.to_parquet(
        tmp_file,
        index=False,
        engine="pyarrow",
        compression="zstd",
    )

    # Validate written parquet.
    _ = pd.read_parquet(tmp_file, engine="pyarrow", columns=["design", "pdk_name", "config_id", "net_name", "pin_cap_ff"])

    tmp_file.replace(parquet_file)

    if not keep_csv:
        csv_file.unlink(missing_ok=True)

    return len(df)


def run_one(
    dataset_path: Path,
    out_dir: Path,
    run_dir: Path,
    feat_tcl: Path,
    openroad_bin: str,
    openroad_threads: int,
    overwrite: bool,
    keep_csv: bool,
    density_bin_um: float,
    timeout_sec: Optional[int],
) -> dict:
    rel = run_dir.relative_to(dataset_path)
    out_run_dir = out_dir / rel
    out_run_dir.mkdir(parents=True, exist_ok=True)

    config_file = run_dir / "config" / "config.tcl"
    def_file = find_postcts_def(run_dir)
    sdc_file = find_postcts_sdc(run_dir)

    tcl_file = out_run_dir / "extract_net_features_postcts.tcl"
    csv_file = out_run_dir / "net_features_postcts.csv"
    parquet_file = out_run_dir / "net_features_postcts.parquet"

    stdout_log = out_run_dir / "openroad_stdout.log"
    stderr_log = out_run_dir / "openroad_stderr.log"

    row = {
        "run_dir": str(run_dir),
        "out_dir": str(out_run_dir),
        "parquet_file": str(parquet_file),
        "status": "",
        "reason": "",
        "num_nets": 0,
        "config_file": str(config_file),
        "def_file": str(def_file) if def_file else "",
        "sdc_file": str(sdc_file) if sdc_file else "",
    }

    if output_is_complete(parquet_file) and not overwrite:
        row["status"] = "skipped"
        row["reason"] = "output already exists"
        return row

    if not config_file.exists():
        row["status"] = "failed"
        row["reason"] = "config/config.tcl not found"
        return row

    if def_file is None:
        row["status"] = "failed"
        row["reason"] = "postCTS DEF not found"
        return row

    if sdc_file is None:
        row["status"] = "failed"
        row["reason"] = "postCTS SDC not found"
        return row

    if not feat_tcl.exists():
        row["status"] = "failed"
        row["reason"] = f"feat.tcl not found: {feat_tcl}"
        return row

    make_tcl_script(
        config_file=config_file,
        def_file=def_file,
        sdc_file=sdc_file,
        feat_tcl=feat_tcl,
        csv_file=csv_file,
        tcl_file=tcl_file,
        density_bin_um=density_bin_um,
    )

    cmd = [
        openroad_bin,
        "-threads",
        str(openroad_threads),
        "-no_init",
        str(tcl_file),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
        )

        stdout_log.write_text(proc.stdout)
        stderr_log.write_text(proc.stderr)

        if proc.returncode != 0:
            row["status"] = "failed"
            row["reason"] = f"OpenROAD return code {proc.returncode}"
            return row

        if not csv_file.exists() or csv_file.stat().st_size == 0:
            row["status"] = "failed"
            row["reason"] = "CSV file was not generated"
            return row

        num_nets = convert_csv_to_parquet(
            csv_file=csv_file,
            parquet_file=parquet_file,
            keep_csv=keep_csv,
        )

        row["status"] = "ok"
        row["reason"] = ""
        row["num_nets"] = int(num_nets)
        return row

    except subprocess.TimeoutExpired:
        row["status"] = "failed"
        row["reason"] = f"timeout after {timeout_sec} seconds"
        return row

    except Exception as e:
        row["status"] = "failed"
        row["reason"] = repr(e)
        return row


def write_report(report_rows: list[dict], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "run_dir",
        "out_dir",
        "parquet_file",
        "status",
        "reason",
        "num_nets",
        "config_file",
        "def_file",
        "sdc_file",
    ]

    with report_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract postCTS net-level features from raw OpenROAD dataset and save one parquet file per run."
    )

    parser.add_argument(
        "--dataset_path",
        required=True,
        type=Path,
        help="Path to raw dataset root.",
    )

    parser.add_argument(
        "--out_dir",
        required=True,
        type=Path,
        help="Output root directory. Raw dataset structure will be mirrored.",
    )

    parser.add_argument(
        "--feat_tcl",
        required=True,
        type=Path,
        help="Path to feat.tcl containing extract_net_features_prects procedure.",
    )

    parser.add_argument(
        "--openroad_bin",
        default="openroad",
        help="OpenROAD executable. Default: openroad",
    )

    parser.add_argument(
        "--openroad_threads",
        type=int,
        default=4,
        help="Number of OpenROAD threads per run. Default: 4.",
    )

    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of parallel OpenROAD jobs. Default: 1.",
    )

    parser.add_argument(
        "--density_bin_um",
        type=float,
        default=10.0,
        help="Density bin size in microns used by feat.tcl. Default: 10.0.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing parquet files.",
    )

    parser.add_argument(
        "--keep_csv",
        action="store_true",
        help="Keep intermediate CSV files.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Timeout per OpenROAD run in seconds. Default: no timeout.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only first N runs. Useful for debugging.",
    )

    parser.add_argument(
        "--start_index",
        type=int,
        default=0,
        help="Start from run index. Useful for debugging or resuming.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    dataset_path = args.dataset_path.resolve()
    out_dir = args.out_dir.resolve()
    feat_tcl = args.feat_tcl.resolve()

    if not dataset_path.exists():
        print(f"ERROR: dataset_path does not exist: {dataset_path}", file=sys.stderr)
        sys.exit(1)

    if not feat_tcl.exists():
        print(f"ERROR: feat_tcl does not exist: {feat_tcl}", file=sys.stderr)
        sys.exit(1)

    run_dirs = find_run_dirs(dataset_path)

    run_dirs = run_dirs[args.start_index:]

    if args.limit is not None:
        run_dirs = run_dirs[:args.limit]

    print(f"Found runs: {len(run_dirs)}")
    print(f"Dataset path: {dataset_path}")
    print(f"Output dir: {out_dir}")
    print(f"Feature Tcl: {feat_tcl}")
    print(f"Parallel jobs: {args.jobs}")
    print(f"OpenROAD threads per job: {args.openroad_threads}")

    report_rows: list[dict] = []

    if args.jobs == 1:
        for idx, run_dir in enumerate(run_dirs, 1):
            print(f"[{idx}/{len(run_dirs)}] {run_dir}")

            row = run_one(
                dataset_path=dataset_path,
                out_dir=out_dir,
                run_dir=run_dir,
                feat_tcl=feat_tcl,
                openroad_bin=args.openroad_bin,
                openroad_threads=args.openroad_threads,
                overwrite=args.overwrite,
                keep_csv=args.keep_csv,
                density_bin_um=args.density_bin_um,
                timeout_sec=args.timeout,
            )

            report_rows.append(row)
            print(f"  -> {row['status']}: {row['reason']} nets={row['num_nets']}")

    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futures = {
                ex.submit(
                    run_one,
                    dataset_path,
                    out_dir,
                    run_dir,
                    feat_tcl,
                    args.openroad_bin,
                    args.openroad_threads,
                    args.overwrite,
                    args.keep_csv,
                    args.density_bin_um,
                    args.timeout,
                ): run_dir
                for run_dir in run_dirs
            }

            for idx, fut in enumerate(as_completed(futures), 1):
                row = fut.result()
                report_rows.append(row)
                print(
                    f"[{idx}/{len(run_dirs)}] "
                    f"{row['status']}: {row['run_dir']} "
                    f"nets={row['num_nets']} "
                    f"{row['reason']}"
                )

    out_dir.mkdir(parents=True, exist_ok=True)

    report_path = out_dir / "extraction_report.csv"
    write_report(report_rows, report_path)

    n_ok = sum(r["status"] == "ok" for r in report_rows)
    n_failed = sum(r["status"] == "failed" for r in report_rows)
    n_skipped = sum(r["status"] == "skipped" for r in report_rows)
    total_nets = sum(int(r["num_nets"]) for r in report_rows if str(r["num_nets"]).isdigit())

    summary = {
        "dataset_path": str(dataset_path),
        "out_dir": str(out_dir),
        "feat_tcl": str(feat_tcl),
        "runs_total": len(run_dirs),
        "runs_ok": n_ok,
        "runs_failed": n_failed,
        "runs_skipped": n_skipped,
        "total_nets": total_nets,
    }

    summary_path = out_dir / "summary.json"
    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)

    print()
    print("Summary")
    print("-------")
    print(json.dumps(summary, indent=2))
    print(f"Report: {report_path}")
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()