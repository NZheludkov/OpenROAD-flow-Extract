#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional

import pandas as pd
from tqdm import tqdm


CAP_TO_FF = {
    "FF": 1.0,
    "PF": 1e3,
    "NF": 1e6,
}

@dataclass
class SpefUnits:
    c_unit_value: float = 1.0
    c_unit_name: str = "PF"

    @property
    def cap_to_ff(self) -> float:
        unit = self.c_unit_name.upper()
        if unit not in CAP_TO_FF:
            raise ValueError(f"Unsupported SPEF capacitance unit: {self.c_unit_name}")
        return self.c_unit_value * CAP_TO_FF[unit]


def resolve_name(token: str, name_map: Dict[str, str]) -> str:
    """
    Resolve SPEF aliases.

    Examples:
      *5250    -> _01234_
      *5250:14 -> _01234_:14
    """
    if not token.startswith("*"):
        return token

    if ":" in token:
        base, suffix = token.split(":", 1)
        return f"{name_map.get(base, base)}:{suffix}"

    return name_map.get(token, token)


def parse_spef_header_and_namemap(spef_path: Path) -> tuple[SpefUnits, Dict[str, str]]:
    units = SpefUnits()
    name_map: Dict[str, str] = {}
    in_name_map = False

    with spef_path.open("r", errors="ignore") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            if line.startswith("*C_UNIT"):
                parts = line.split()
                if len(parts) >= 3:
                    units.c_unit_value = float(parts[1])
                    units.c_unit_name = parts[2].upper()

            elif line.startswith("*NAME_MAP"):
                in_name_map = True
                continue

            elif line.startswith("*D_NET"):
                break

            elif in_name_map:
                parts = line.split(maxsplit=1)
                if len(parts) == 2 and parts[0].startswith("*"):
                    name_map[parts[0]] = parts[1]

    return units, name_map


def parse_spef_total_caps(spef_path: Path) -> Iterator[dict]:
    """
    Extract only total net capacitance from *D_NET lines.

    SPEF example:
      *D_NET *5250 6.94318e-05
    """
    units, name_map = parse_spef_header_and_namemap(spef_path)

    with spef_path.open("r", errors="ignore") as f:
        for line in f:
            line = line.strip()

            if not line.startswith("*D_NET"):
                continue

            parts = line.split()
            if len(parts) < 3:
                continue

            net_token = parts[1]
            cap_total_raw = float(parts[2])

            yield {
                "net_name": resolve_name(net_token, name_map),
                "cap_total_ff": cap_total_raw * units.cap_to_ff,
            }


def extract_spef_to_dataframe(spef_path: Path) -> pd.DataFrame:
    rows = list(parse_spef_total_caps(spef_path))
    return pd.DataFrame(rows)


def read_run_info(run_info_path: Path) -> dict:
    if not run_info_path.exists():
        return {}

    try:
        with run_info_path.open("r", newline="", errors="ignore") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            return rows[0] if rows else {}
    except Exception:
        return {}


def find_run_dirs(dataset_path: Path) -> list[Path]:
    return sorted(run_info.parent for run_info in dataset_path.rglob("run_info.csv"))


def find_spef(run_dir: Path) -> Optional[Path]:
    route_dir = run_dir / "route"
    if not route_dir.exists():
        return None

    spefs = sorted(route_dir.glob("*.spef"))
    if not spefs:
        spefs = sorted(route_dir.rglob("*.spef"))

    if not spefs:
        return None

    # If several SPEF files exist, use the largest one.
    return max(spefs, key=lambda p: p.stat().st_size)


def infer_metadata(dataset_path: Path, run_dir: Path) -> dict:
    """
    Expected raw dataset structure:
      dataset_path / pdk_name / design / config_id / run_info.csv

    If run_info.csv contains design/pdk_name, use these values.
    """
    rel = run_dir.relative_to(dataset_path)
    parts = rel.parts

    pdk_name = parts[0] if len(parts) >= 1 else "unknown_pdk"
    design = parts[1] if len(parts) >= 2 else "unknown_design"
    config_id = parts[2] if len(parts) >= 3 else run_dir.name

    run_info = read_run_info(run_dir / "run_info.csv")

    pdk_name = run_info.get("pdk_name", pdk_name)
    design = run_info.get("design", design)

    return {
        "design": design,
        "pdk_name": pdk_name,
        "config_id": config_id,
    }


def make_output_run_dir(dataset_path: Path, out_dir: Path, run_dir: Path) -> Path:
    """
    Mirror raw dataset directory structure in output directory.
    """
    rel = run_dir.relative_to(dataset_path)
    return out_dir / rel


def write_run_parquet(df: pd.DataFrame, out_file: Path, overwrite: bool) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if out_file.exists() and not overwrite:
        return

    tmp_file = out_file.with_name(out_file.name + ".tmp")

    df.to_parquet(
        tmp_file,
        index=False,
        engine="pyarrow",
        compression="zstd",
    )

    # Validate temporary parquet before replacing final file.
    pd.read_parquet(tmp_file, engine="pyarrow", columns=["design", "pdk_name", "config_id", "net_name", "cap_total_ff"])

    tmp_file.replace(out_file)


def extract_all_runs(
    dataset_path: Path,
    out_dir: Path,
    parquet_name: str,
    overwrite: bool,
) -> None:
    run_dirs = find_run_dirs(dataset_path)

    report_rows = []

    for run_dir in tqdm(run_dirs, desc="Extracting net capacitance per run"):
        meta = infer_metadata(dataset_path, run_dir)
        spef_path = find_spef(run_dir)

        out_run_dir = make_output_run_dir(dataset_path, out_dir, run_dir)
        out_file = out_run_dir / parquet_name

        if out_file.exists() and not overwrite:
            report_rows.append({
                **meta,
                "run_dir": str(run_dir),
                "out_file": str(out_file),
                "status": "skipped",
                "reason": "output exists",
                "num_nets": "",
                "spef_path": str(spef_path) if spef_path else "",
            })
            continue

        if spef_path is None:
            report_rows.append({
                **meta,
                "run_dir": str(run_dir),
                "out_file": str(out_file),
                "status": "failed",
                "reason": "SPEF not found",
                "num_nets": 0,
                "spef_path": "",
            })
            continue

        try:
            df = extract_spef_to_dataframe(spef_path)

            if df.empty:
                report_rows.append({
                    **meta,
                    "run_dir": str(run_dir),
                    "out_file": str(out_file),
                    "status": "failed",
                    "reason": "no nets extracted",
                    "num_nets": 0,
                    "spef_path": str(spef_path),
                })
                continue

            df["design"] = meta["design"]
            df["pdk_name"] = meta["pdk_name"]
            df["config_id"] = meta["config_id"]

            df = df[
                [
                    "design",
                    "pdk_name",
                    "config_id",
                    "net_name",
                    "cap_total_ff",
                ]
            ]

            write_run_parquet(df, out_file, overwrite=overwrite)

            report_rows.append({
                **meta,
                "run_dir": str(run_dir),
                "out_file": str(out_file),
                "status": "ok",
                "reason": "",
                "num_nets": int(len(df)),
                "cap_total_ff_min": float(df["cap_total_ff"].min()),
                "cap_total_ff_mean": float(df["cap_total_ff"].mean()),
                "cap_total_ff_max": float(df["cap_total_ff"].max()),
                "spef_path": str(spef_path),
            })

        except Exception as e:
            report_rows.append({
                **meta,
                "run_dir": str(run_dir),
                "out_file": str(out_file),
                "status": "failed",
                "reason": repr(e),
                "num_nets": 0,
                "spef_path": str(spef_path),
            })

    out_dir.mkdir(parents=True, exist_ok=True)

    report_df = pd.DataFrame(report_rows)
    report_path = out_dir / "extraction_report.csv"
    report_df.to_csv(report_path, index=False)

    summary = {
        "dataset_path": str(dataset_path),
        "out_dir": str(out_dir),
        "parquet_name": parquet_name,
        "run_dirs_found": len(run_dirs),
        "runs_ok": int((report_df["status"] == "ok").sum()) if not report_df.empty else 0,
        "runs_failed": int((report_df["status"] == "failed").sum()) if not report_df.empty else 0,
        "runs_skipped": int((report_df["status"] == "skipped").sum()) if not report_df.empty else 0,
        "total_nets": int(pd.to_numeric(report_df.get("num_nets", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
        if not report_df.empty else 0,
    }

    summary_path = out_dir / "summary.json"
    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    print(f"Report: {report_path}")
    print(f"Summary: {summary_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract post-route total net capacitance from SPEF files and save one parquet file per run."
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
        help="Output root directory. The raw dataset directory structure will be mirrored here.",
    )

    parser.add_argument(
        "--parquet_name",
        default="net_capacitance.parquet",
        help="Output parquet filename inside each mirrored run directory.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing parquet files.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    dataset_path = args.dataset_path.resolve()
    out_dir = args.out_dir.resolve()

    if not dataset_path.exists():
        print(f"ERROR: dataset_path does not exist: {dataset_path}", file=sys.stderr)
        sys.exit(1)

    extract_all_runs(
        dataset_path=dataset_path,
        out_dir=out_dir,
        parquet_name=args.parquet_name,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    main()