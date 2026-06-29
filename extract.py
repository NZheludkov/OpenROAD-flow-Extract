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

RES_TO_OHM = {
    "OHM": 1.0,
    "KOHM": 1e3,
}


@dataclass
class SpefUnits:
    c_unit_value: float = 1.0
    c_unit_name: str = "PF"
    r_unit_value: float = 1.0
    r_unit_name: str = "OHM"

    @property
    def cap_to_ff(self) -> float:
        unit = self.c_unit_name.upper()
        if unit not in CAP_TO_FF:
            raise ValueError(f"Unsupported SPEF capacitance unit: {self.c_unit_name}")
        return self.c_unit_value * CAP_TO_FF[unit]

    @property
    def res_to_ohm(self) -> float:
        unit = self.r_unit_name.upper()
        if unit not in RES_TO_OHM:
            raise ValueError(f"Unsupported SPEF resistance unit: {self.r_unit_name}")
        return self.r_unit_value * RES_TO_OHM[unit]


def resolve_name(token: str, name_map: Dict[str, str]) -> str:
    """
    Resolve SPEF name aliases.

    Examples:
      *5250       -> _01234_
      *5250:14    -> _01234_:14
      plain_name  -> plain_name
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

            elif line.startswith("*R_UNIT"):
                parts = line.split()
                if len(parts) >= 3:
                    units.r_unit_value = float(parts[1])
                    units.r_unit_name = parts[2].upper()

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


def parse_cap_line(line: str, current: dict) -> None:
    """
    SPEF CAP line variants:
      index node cap
      index node1 node2 cap

    Single-node form is ground capacitance.
    Two-node form is coupling capacitance.
    """
    parts = line.split()

    if len(parts) == 3:
        _, _node, cap = parts
        cap_value = float(cap)
        current["ground_cap_raw"] += cap_value
        current["num_cap_entries"] += 1
        current["num_ground_cap_entries"] += 1

    elif len(parts) == 4:
        _, _node1, _node2, cap = parts
        cap_value = float(cap)
        current["coupling_cap_raw"] += cap_value
        current["num_cap_entries"] += 1
        current["num_coupling_cap_entries"] += 1


def parse_res_line(line: str, current: dict) -> None:
    """
    SPEF RES line:
      index node1 node2 resistance
    """
    parts = line.split()

    if len(parts) == 4:
        _, _node1, _node2, res = parts
        res_value = float(res)
        current["res_total_raw"] += res_value
        current["num_res_entries"] += 1


def finalize_net(current: dict, units: SpefUnits) -> dict:
    row = dict(current)

    row["cap_total_ff"] = row["cap_total_raw"] * units.cap_to_ff
    row["ground_cap_ff"] = row["ground_cap_raw"] * units.cap_to_ff
    row["coupling_cap_ff"] = row["coupling_cap_raw"] * units.cap_to_ff
    row["res_total_ohm"] = row["res_total_raw"] * units.res_to_ohm

    row["spef_c_unit"] = f"{units.c_unit_value:g} {units.c_unit_name}"
    row["spef_r_unit"] = f"{units.r_unit_value:g} {units.r_unit_name}"

    cap_sum = row["ground_cap_ff"] + row["coupling_cap_ff"]
    row["cap_sum_ff"] = cap_sum
    row["cap_total_minus_sum_ff"] = row["cap_total_ff"] - cap_sum

    return row


def parse_spef_total_caps(spef_path: Path) -> Iterator[dict]:
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

    if len(spefs) > 1:
        # Usually there should be one SPEF per run.
        # Taking the largest one is safer if temporary/small SPEF files exist.
        spefs = sorted(spefs, key=lambda p: p.stat().st_size, reverse=True)

    return spefs[0]


def read_run_info(run_info_path: Path) -> dict:
    if not run_info_path.exists():
        return {}

    try:
        with run_info_path.open("r", newline="", errors="ignore") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if not rows:
                return {}
            return rows[0]
    except Exception:
        return {}


def infer_metadata(dataset_path: Path, run_dir: Path) -> dict:
    """
    Expected structure:
      dataset_path / pdk_name / design / config_id / run_info.csv

    If run_info.csv has design/pdk_name columns, they override path-based inference.
    """
    rel = run_dir.relative_to(dataset_path)
    parts = rel.parts

    pdk_name = parts[0] if len(parts) >= 1 else "unknown_pdk"
    design = parts[1] if len(parts) >= 2 else "unknown_design"
    config_id = parts[2] if len(parts) >= 3 else run_dir.name

    run_info = read_run_info(run_dir / "run_info.csv")

    pdk_name = run_info.get("pdk_name", pdk_name)
    design = run_info.get("design", design)

    run_id = f"{pdk_name}__{design}__{config_id}"

    meta = {
        "run_id": run_id,
        "pdk_name": pdk_name,
        "design": design,
        "config_id": config_id,
        "run_dir": str(run_dir),
    }

    # Useful run-level metrics from run_info.csv if present.
    optional_cols = [
        "CLK_PERIOD",
        "IO_DELAY",
        "CU",
        "AR",
        "PDN_HWIDTH_TRACK",
        "PDN_HSPACING_TRACK",
        "PDN_HPITCH_TRACK",
        "PDN_VWIDTH_TRACK",
        "PDN_VSPACING_TRACK",
        "PDN_VPITCH_TRACK",
        "cells_number",
        "nets_number",
        "regs_number",
        "wns",
        "tns",
        "total_power",
        "design_area",
        "drc_errors",
    ]

    for col in optional_cols:
        if col in run_info:
            meta[col] = run_info[col]

    return meta


def reorder_columns(df: pd.DataFrame) -> pd.DataFrame:
    first_cols = [
        "run_id",
        "design",
        "pdk_name",
        "config_id",
        "net_name",
        "net_name_spef",
        "cap_total_ff",
        "ground_cap_ff",
        "coupling_cap_ff",
        "cap_sum_ff",
        "cap_total_minus_sum_ff",
        "res_total_ohm",
        "num_cap_entries",
        "num_ground_cap_entries",
        "num_coupling_cap_entries",
        "num_res_entries",
        "spef_c_unit",
        "spef_r_unit",
        "source_spef",
        "run_dir",
    ]

    existing_first_cols = [c for c in first_cols if c in df.columns]
    other_cols = [c for c in df.columns if c not in existing_first_cols]

    return df[existing_first_cols + other_cols]


def write_output(df: pd.DataFrame, out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)

    suffix = out_file.suffix.lower()
    tmp_file = out_file.with_name(out_file.name + ".tmp")

    if suffix == ".parquet":
        df.to_parquet(tmp_file, index=False, engine="pyarrow")
    elif suffix == ".csv":
        df.to_csv(tmp_file, index=False)
    else:
        raise ValueError("Unsupported output format. Use .parquet or .csv")

    # Проверяем, что файл реально читается
    if suffix == ".parquet":
        pd.read_parquet(tmp_file, engine="pyarrow")

    tmp_file.replace(out_file)


def extract_dataset(dataset_path: Path, out_file: Path, report_file: Optional[Path]) -> None:
    run_dirs = find_run_dirs(dataset_path)

    all_parts: list[pd.DataFrame] = []
    report_rows: list[dict] = []

    for run_dir in tqdm(run_dirs, desc="Extracting net capacitance labels"):
        meta = infer_metadata(dataset_path, run_dir)
        spef_path = find_spef(run_dir)

        if spef_path is None:
            report_rows.append({
                **meta,
                "status": "failed",
                "reason": "SPEF not found",
                "spef_path": "",
                "num_nets_extracted": 0,
            })
            continue

        try:
            df = extract_spef_to_dataframe(spef_path)

            if df.empty:
                report_rows.append({
                    **meta,
                    "status": "failed",
                    "reason": "No nets extracted",
                    "spef_path": str(spef_path),
                    "num_nets_extracted": 0,
                })
                continue

            for key, value in meta.items():
                df[key] = value

            df["source_spef"] = str(spef_path)
            df = reorder_columns(df)

            all_parts.append(df)

            report_rows.append({
                **meta,
                "status": "ok",
                "reason": "",
                "spef_path": str(spef_path),
                "num_nets_extracted": int(len(df)),
                "cap_total_ff_min": float(df["cap_total_ff"].min()),
                "cap_total_ff_mean": float(df["cap_total_ff"].mean()),
                "cap_total_ff_max": float(df["cap_total_ff"].max()),
                "cap_total_minus_sum_abs_max_ff": float(df["cap_total_minus_sum_ff"].abs().max()),
            })

        except Exception as e:
            report_rows.append({
                **meta,
                "status": "failed",
                "reason": repr(e),
                "spef_path": str(spef_path),
                "num_nets_extracted": 0,
            })

    if not all_parts:
        raise RuntimeError("No SPEF data was extracted. Check dataset_path and route/*.spef files.")

    result = pd.concat(all_parts, ignore_index=True)
    write_output(result, out_file)

    if report_file is None:
        report_file = out_file.with_suffix(".report.csv")

    pd.DataFrame(report_rows).to_csv(report_file, index=False)

    summary = {
        "dataset_path": str(dataset_path),
        "out_file": str(out_file),
        "report_file": str(report_file),
        "run_dirs_found": len(run_dirs),
        "runs_extracted": sum(1 for r in report_rows if r["status"] == "ok"),
        "runs_failed": sum(1 for r in report_rows if r["status"] != "ok"),
        "total_net_labels": int(len(result)),
        "unique_designs": int(result["design"].nunique()),
        "unique_pdks": int(result["pdk_name"].nunique()),
    }

    summary_file = out_file.with_suffix(".summary.json")
    with summary_file.open("w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract post-route net capacitance labels from SPEF files for all runs in an RTL-to-GDS raw dataset."
    )

    parser.add_argument(
        "--dataset_path",
        required=True,
        type=Path,
        help="Path to raw dataset root directory.",
    )

    parser.add_argument(
        "--out_file",
        required=True,
        type=Path,
        help="Output file. Supported formats: .parquet, .csv",
    )

    parser.add_argument(
        "--report_file",
        required=False,
        type=Path,
        default=None,
        help="Optional extraction report CSV path. Default: <out_file>.report.csv",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    dataset_path = args.dataset_path.resolve()
    out_file = args.out_file.resolve()

    if not dataset_path.exists():
        print(f"ERROR: dataset_path does not exist: {dataset_path}", file=sys.stderr)
        sys.exit(1)

    extract_dataset(
        dataset_path=dataset_path,
        out_file=out_file,
        report_file=args.report_file,
    )


if __name__ == "__main__":
    main()