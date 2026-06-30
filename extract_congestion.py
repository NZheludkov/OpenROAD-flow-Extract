#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional


REQUIRED_MAPS = [
    "routing_congestion.txt",
    "rudy_congestion.txt",
    "pin_density.txt",
    "placement_density.txt",
]


def find_run_dirs(dataset_path: Path) -> list[Path]:
    """
    A run directory is detected by config/config.tcl.
    """
    config_files = sorted(dataset_path.rglob("config/config.tcl"))
    return [cfg.parent.parent for cfg in config_files]


def find_def(run_dir: Path) -> Optional[Path]:
    """
    Prefer the expected final routed DEF location, then fall back to any DEF under route/.
    """
    candidates = [
        run_dir / "route" / "def" / "def.def",
        run_dir / "route" / "def" / f"{run_dir.parent.name}.def",
        run_dir / "route" / "def" / "route.def",
    ]

    for cand in candidates:
        if cand.exists():
            return cand

    route_dir = run_dir / "route"
    if not route_dir.exists():
        return None

    defs = sorted(route_dir.rglob("*.def"))
    if not defs:
        return None

    # Usually the final routed DEF is the largest DEF in route/.
    return max(defs, key=lambda p: p.stat().st_size)


def output_is_complete(out_run_dir: Path) -> bool:
    return all((out_run_dir / name).exists() and (out_run_dir / name).stat().st_size > 0 for name in REQUIRED_MAPS)


def make_tcl_script(
    run_dir: Path,
    config_file: Path,
    def_file: Path,
    out_run_dir: Path,
    tcl_file: Path,
) -> None:
    """
    Creates OpenROAD Tcl script for one run.
    """
    tcl = f"""
# Auto-generated congestion map extraction script

source "{config_file}"

foreach lef $lef_list {{
    read_lef $lef
}}

read_def "{def_file}"

# Some configs define routing layers, but this is safe to repeat when variables exist.
if {{[info exists bottom_routing_metal] && [info exists top_routing_metal]}} {{
    catch {{set_routing_layers -signal $bottom_routing_metal-$top_routing_metal}}
}}

# Run global routing to populate Routing congestion heatmap.
# If it fails, still try to dump RUDY/Pin/Placement maps.
set grt_status [catch {{global_route}} grt_msg]
if {{$grt_status != 0}} {{
    puts "WARNING: global_route failed: $grt_msg"
}}

file mkdir "{out_run_dir}"

# Dump GUI heatmaps.
# Heatmap names are OpenROAD GUI names.
set dump_status [catch {{
    gui::dump_heatmap Routing "{out_run_dir / "routing_congestion.txt"}"
}} dump_msg]
if {{$dump_status != 0}} {{
    puts "WARNING: failed to dump Routing heatmap: $dump_msg"
}}

set dump_status [catch {{
    gui::dump_heatmap RUDY "{out_run_dir / "rudy_congestion.txt"}"
}} dump_msg]
if {{$dump_status != 0}} {{
    puts "WARNING: failed to dump RUDY heatmap: $dump_msg"
}}

set dump_status [catch {{
    gui::dump_heatmap Pin "{out_run_dir / "pin_density.txt"}"
}} dump_msg]
if {{$dump_status != 0}} {{
    puts "WARNING: failed to dump Pin heatmap: $dump_msg"
}}

set dump_status [catch {{
    gui::dump_heatmap Placement "{out_run_dir / "placement_density.txt"}"
}} dump_msg]
if {{$dump_status != 0}} {{
    puts "WARNING: failed to dump Placement heatmap: $dump_msg"
}}

exit
"""
    tcl_file.write_text(tcl)


def run_one(
    dataset_path: Path,
    out_dir: Path,
    run_dir: Path,
    openroad_bin: str,
    use_xvfb: bool,
    overwrite: bool,
    timeout_sec: Optional[int],
) -> dict:
    rel = run_dir.relative_to(dataset_path)
    out_run_dir = out_dir / rel
    out_run_dir.mkdir(parents=True, exist_ok=True)

    config_file = run_dir / "config" / "config.tcl"
    def_file = find_def(run_dir)

    row = {
        "run_dir": str(run_dir),
        "out_dir": str(out_run_dir),
        "status": "",
        "reason": "",
        "def_file": str(def_file) if def_file else "",
    }

    if output_is_complete(out_run_dir) and not overwrite:
        row["status"] = "skipped"
        row["reason"] = "output already exists"
        return row

    if not config_file.exists():
        row["status"] = "failed"
        row["reason"] = "config/config.tcl not found"
        return row

    if def_file is None:
        row["status"] = "failed"
        row["reason"] = "route DEF not found"
        return row

    tcl_file = out_run_dir / "dump_heatmaps.tcl"
    stdout_log = out_run_dir / "openroad_stdout.log"
    stderr_log = out_run_dir / "openroad_stderr.log"

    make_tcl_script(
        run_dir=run_dir,
        config_file=config_file,
        def_file=def_file,
        out_run_dir=out_run_dir,
        tcl_file=tcl_file,
    )

    cmd = [
        openroad_bin,
        "-gui",
        "-no_init",
        str(tcl_file),
    ]

    if use_xvfb:
        cmd = ["xvfb-run", "-a"] + cmd

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

        if not output_is_complete(out_run_dir):
            missing = [name for name in REQUIRED_MAPS if not (out_run_dir / name).exists()]
            row["status"] = "failed"
            row["reason"] = f"missing maps: {missing}"
            return row

        row["status"] = "ok"
        row["reason"] = ""
        return row

    except subprocess.TimeoutExpired:
        row["status"] = "failed"
        row["reason"] = f"timeout after {timeout_sec} sec"
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
        "status",
        "reason",
        "def_file",
    ]

    with report_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract OpenROAD GUI heatmaps for congestion/pin/placement density from raw RTL-to-GDS dataset."
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
        "--openroad_bin",
        default="openroad",
        help="OpenROAD executable. Default: openroad",
    )

    parser.add_argument(
        "--xvfb",
        action="store_true",
        help="Run OpenROAD GUI through xvfb-run. Use this on headless servers.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing heatmap files.",
    )

    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of parallel OpenROAD jobs. Default: 1. Use carefully.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Timeout per run in seconds. Default: no timeout.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    dataset_path = args.dataset_path.resolve()
    out_dir = args.out_dir.resolve()

    if not dataset_path.exists():
        print(f"ERROR: dataset_path does not exist: {dataset_path}", file=sys.stderr)
        sys.exit(1)

    run_dirs = find_run_dirs(dataset_path)

    print(f"Found run dirs: {len(run_dirs)}")
    print(f"Output dir: {out_dir}")

    report_rows: list[dict] = []

    if args.jobs == 1:
        for idx, run_dir in enumerate(run_dirs, 1):
            print(f"[{idx}/{len(run_dirs)}] {run_dir}")
            row = run_one(
                dataset_path=dataset_path,
                out_dir=out_dir,
                run_dir=run_dir,
                openroad_bin=args.openroad_bin,
                use_xvfb=args.xvfb,
                overwrite=args.overwrite,
                timeout_sec=args.timeout,
            )
            report_rows.append(row)
            print(f"  -> {row['status']}: {row['reason']}")
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futures = {
                ex.submit(
                    run_one,
                    dataset_path,
                    out_dir,
                    run_dir,
                    args.openroad_bin,
                    args.xvfb,
                    args.overwrite,
                    args.timeout,
                ): run_dir
                for run_dir in run_dirs
            }

            for idx, fut in enumerate(as_completed(futures), 1):
                row = fut.result()
                report_rows.append(row)
                print(f"[{idx}/{len(run_dirs)}] {row['status']}: {row['run_dir']} {row['reason']}")

    report_path = out_dir / "extraction_report.csv"
    write_report(report_rows, report_path)

    n_ok = sum(r["status"] == "ok" for r in report_rows)
    n_failed = sum(r["status"] == "failed" for r in report_rows)
    n_skipped = sum(r["status"] == "skipped" for r in report_rows)

    print()
    print("Summary")
    print("-------")
    print(f"ok      : {n_ok}")
    print(f"failed  : {n_failed}")
    print(f"skipped : {n_skipped}")
    print(f"report  : {report_path}")


if __name__ == "__main__":
    main()