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


FEATURE_TCL = r'''
# ============================================================
# Embedded Tcl feature extractor
#
# Extracts net-level features from loaded OpenROAD design.
#
# Required design state:
#   - LEF loaded
#   - postCTS DEF loaded
#   - Liberty loaded with corner "view"
#   - SDC loaded
#
# Output columns:
#   design,pdk_name,config_id,net_name,
#   fanin,fanout,fanin_area,fanout_area,
#   hpwl,cell_density,pin_cap_ff,gcell_area,gcell_ar
# ============================================================

if {![info exists DENSITY_BIN_UM]} {
    set DENSITY_BIN_UM 10.0
}

if {![info exists SKIP_MULTI_DRIVER_NETS]} {
    set SKIP_MULTI_DRIVER_NETS 1
}

if {![info exists SKIP_NON_CELL_NETS]} {
    set SKIP_NON_CELL_NETS 1
}


proc is_null {obj} {
    return [expr {$obj eq "" || $obj eq "NULL" || $obj eq "0"}]
}


proc csv_escape {s} {
    set s [string map {\" \"\"} $s]
    return "\"$s\""
}


proc inst_box {inst} {
    set box [$inst getBBox]
    return [list [$box xMin] [$box yMin] [$box xMax] [$box yMax]]
}


proc inst_area_um2 {inst dbu} {
    lassign [inst_box $inst] xmin ymin xmax ymax

    set w_um [expr {double($xmax - $xmin) / double($dbu)}]
    set h_um [expr {double($ymax - $ymin) / double($dbu)}]

    if {$w_um <= 0.0 || $h_um <= 0.0} {
        return 0.0
    }

    return [expr {$w_um * $h_um}]
}


proc mterm_direction {iterm} {
    set mterm [$iterm getMTerm]

    if {[is_null $mterm]} {
        return ""
    }

    return [$mterm getIoType]
}


proc is_signal_net {net} {
    if {[is_null $net]} {
        return 0
    }

    if {[catch {set sig_type [$net getSigType]}]} {
        return 1
    }

    if {$sig_type eq "POWER" || $sig_type eq "GROUND"} {
        return 0
    }

    return 1
}


proc get_net_cell_drivers_and_loads {net} {
    set drivers {}
    set loads {}

    foreach iterm [$net getITerms] {
        set inst [$iterm getInst]

        if {[is_null $inst]} {
            continue
        }

        set dir [mterm_direction $iterm]

        if {$dir eq "OUTPUT"} {
            lappend drivers $iterm
        } elseif {$dir eq "INPUT"} {
            lappend loads $iterm
        } elseif {$dir eq "INOUT"} {
            lappend loads $iterm
        }
    }

    return [list $drivers $loads]
}


proc get_driver_input_nets {driver_inst driven_net} {
    set input_nets [dict create]
    set driven_net_name [$driven_net getName]

    foreach iterm [$driver_inst getITerms] {
        set dir [mterm_direction $iterm]

        if {$dir ne "INPUT"} {
            continue
        }

        set net [$iterm getNet]

        if {[is_null $net]} {
            continue
        }

        if {![is_signal_net $net]} {
            continue
        }

        set net_name [$net getName]

        if {$net_name eq $driven_net_name} {
            continue
        }

        dict set input_nets $net_name $net
    }

    set result {}

    dict for {net_name net_obj} $input_nets {
        lappend result $net_obj
    }

    return $result
}


proc get_upstream_driver_insts {input_nets} {
    set upstream_insts [dict create]

    foreach in_net $input_nets {
        lassign [get_net_cell_drivers_and_loads $in_net] drivers loads

        foreach driver_iterm $drivers {
            set inst [$driver_iterm getInst]

            if {[is_null $inst]} {
                continue
            }

            dict set upstream_insts [$inst getName] $inst
        }
    }

    set result {}

    dict for {inst_name inst_obj} $upstream_insts {
        lappend result $inst_obj
    }

    return $result
}


proc unique_insts_from_iterms {iterms} {
    set inst_dict [dict create]

    foreach iterm $iterms {
        set inst [$iterm getInst]

        if {[is_null $inst]} {
            continue
        }

        dict set inst_dict [$inst getName] $inst
    }

    set result {}

    dict for {name inst} $inst_dict {
        lappend result $inst
    }

    return $result
}


proc sum_unique_inst_area_um2 {insts dbu} {
    set seen [dict create]
    set area 0.0

    foreach inst $insts {
        if {[is_null $inst]} {
            continue
        }

        set inst_name [$inst getName]

        if {[dict exists $seen $inst_name]} {
            continue
        }

        dict set seen $inst_name 1

        set area [expr {$area + [inst_area_um2 $inst $dbu]}]
    }

    return $area
}


proc bbox_from_insts {insts} {
    set first 1

    set xmin 0
    set ymin 0
    set xmax 0
    set ymax 0

    foreach inst $insts {
        if {[is_null $inst]} {
            continue
        }

        lassign [inst_box $inst] ixmin iymin ixmax iymax

        if {$first} {
            set xmin $ixmin
            set ymin $iymin
            set xmax $ixmax
            set ymax $iymax
            set first 0
        } else {
            if {$ixmin < $xmin} {
                set xmin $ixmin
            }
            if {$iymin < $ymin} {
                set ymin $iymin
            }
            if {$ixmax > $xmax} {
                set xmax $ixmax
            }
            if {$iymax > $ymax} {
                set ymax $iymax
            }
        }
    }

    if {$first} {
        return ""
    }

    return [list $xmin $ymin $xmax $ymax]
}


proc hpwl_from_bbox_um {bbox dbu} {
    lassign $bbox xmin ymin xmax ymax

    set w_um [expr {double($xmax - $xmin) / double($dbu)}]
    set h_um [expr {double($ymax - $ymin) / double($dbu)}]

    return [expr {$w_um + $h_um}]
}


proc area_from_bbox_um2 {bbox dbu} {
    lassign $bbox xmin ymin xmax ymax

    set w_um [expr {double($xmax - $xmin) / double($dbu)}]
    set h_um [expr {double($ymax - $ymin) / double($dbu)}]

    if {$w_um <= 0.0 || $h_um <= 0.0} {
        return 0.0
    }

    return [expr {$w_um * $h_um}]
}


proc ar_from_bbox {bbox} {
    lassign $bbox xmin ymin xmax ymax

    set w [expr {double($xmax - $xmin)}]
    set h [expr {double($ymax - $ymin)}]

    if {$h <= 0.0} {
        return 0.0
    }

    return [expr {$w / $h}]
}


proc build_density_bins {block dbu bin_um} {
    global DENSITY_BIN_DBU
    global CELL_DENSITY_BIN_AREA

    catch {array unset CELL_DENSITY_BIN_AREA}

    set DENSITY_BIN_DBU [expr {int(double($bin_um) * double($dbu))}]

    if {$DENSITY_BIN_DBU < 1} {
        set DENSITY_BIN_DBU 1
    }

    foreach inst [$block getInsts] {
        if {[is_null $inst]} {
            continue
        }

        if {[catch {lassign [inst_box $inst] xmin ymin xmax ymax}]} {
            continue
        }

        set cx [expr {int(($xmin + $xmax) / 2)}]
        set cy [expr {int(($ymin + $ymax) / 2)}]

        set bx [expr {int(floor(double($cx) / double($DENSITY_BIN_DBU)))}]
        set by [expr {int(floor(double($cy) / double($DENSITY_BIN_DBU)))}]

        set key "$bx,$by"

        set area_um2 [inst_area_um2 $inst $dbu]

        if {![info exists CELL_DENSITY_BIN_AREA($key)]} {
            set CELL_DENSITY_BIN_AREA($key) 0.0
        }

        set CELL_DENSITY_BIN_AREA($key) [expr {$CELL_DENSITY_BIN_AREA($key) + $area_um2}]
    }
}


proc cell_density_in_bbox {bbox dbu} {
    global DENSITY_BIN_DBU
    global CELL_DENSITY_BIN_AREA

    lassign $bbox xmin ymin xmax ymax

    set bbox_area_um2 [area_from_bbox_um2 $bbox $dbu]

    if {$bbox_area_um2 <= 0.0} {
        return 0.0
    }

    set bx0 [expr {int(floor(double($xmin) / double($DENSITY_BIN_DBU)))}]
    set by0 [expr {int(floor(double($ymin) / double($DENSITY_BIN_DBU)))}]
    set bx1 [expr {int(floor(double($xmax) / double($DENSITY_BIN_DBU)))}]
    set by1 [expr {int(floor(double($ymax) / double($DENSITY_BIN_DBU)))}]

    set cell_area_sum_um2 0.0

    for {set bx $bx0} {$bx <= $bx1} {incr bx} {
        for {set by $by0} {$by <= $by1} {incr by} {
            set key "$bx,$by"

            if {[info exists CELL_DENSITY_BIN_AREA($key)]} {
                set cell_area_sum_um2 [expr {$cell_area_sum_um2 + $CELL_DENSITY_BIN_AREA($key)}]
            }
        }
    }

    return [expr {$cell_area_sum_um2 / $bbox_area_um2}]
}


proc get_net_pin_cap_ff {net_name} {
    # Correct OpenROAD/OpenSTA method found experimentally:
    #
    #   set sta_net [get_nets _1925_]
    #   $sta_net pin_capacitance view max
    #
    # Returned value is in farads.
    # Convert to femtofarads: 1 F = 1e15 fF.

    set sta_nets ""

    if {[catch {set sta_nets [get_nets -quiet $net_name]}]} {
        if {[catch {set sta_nets [get_nets $net_name]}]} {
            return 0.0
        }
    }

    if {[llength $sta_nets] == 0} {
        return 0.0
    }

    set sta_net [lindex $sta_nets 0]

    if {![catch {set cap_f [$sta_net pin_capacitance view max]}]} {
        if {$cap_f ne ""} {
            return [expr {double($cap_f) * 1.0e15}]
        }
    }

    return 0.0
}


proc extract_net_features_postcts {out_csv} {
    global DENSITY_BIN_UM
    global SKIP_MULTI_DRIVER_NETS
    global SKIP_NON_CELL_NETS

    set block [ord::get_db_block]
    set dbu [$block getDbUnitsPerMicron]

    puts "Building density bins..."
    build_density_bins $block $dbu $DENSITY_BIN_UM
    puts "Density bins are ready."

    if {[info exists ::design]} {
        set design_name $::design
    } else {
        set design_name "unknown_design"
    }

    if {[info exists ::pdk_name]} {
        set pdk_name $::pdk_name
    } else {
        set pdk_name "unknown_pdk"
    }

    if {[info exists ::run_dir]} {
        set config_id [file tail $::run_dir]
    } else {
        set config_id "unknown_config"
    }

    set fh [open $out_csv "w"]

    puts $fh "design,pdk_name,config_id,net_name,fanin,fanout,fanin_area,fanout_area,hpwl,cell_density,pin_cap_ff,gcell_area,gcell_ar"

    set total_nets 0
    set written_nets 0
    set skipped_nets 0

    foreach net [$block getNets] {
        incr total_nets

        if {![is_signal_net $net]} {
            incr skipped_nets
            continue
        }

        set net_name [$net getName]

        lassign [get_net_cell_drivers_and_loads $net] driver_iterms load_iterms

        set num_drivers [llength $driver_iterms]
        set num_loads [llength $load_iterms]

        if {$SKIP_NON_CELL_NETS && ($num_drivers == 0 || $num_loads == 0)} {
            incr skipped_nets
            continue
        }

        if {$SKIP_MULTI_DRIVER_NETS && $num_drivers != 1} {
            incr skipped_nets
            continue
        }

        if {$num_drivers == 0} {
            incr skipped_nets
            continue
        }

        set driver_iterm [lindex $driver_iterms 0]
        set driver_inst [$driver_iterm getInst]

        if {[is_null $driver_inst]} {
            incr skipped_nets
            continue
        }

        # fanin: number of unique signal input nets of driver cell
        set input_nets [get_driver_input_nets $driver_inst $net]
        set fanin [llength $input_nets]

        # fanout: number of cell load pins
        set fanout $num_loads

        # fanin_area: area of upstream driver cells
        set upstream_driver_insts [get_upstream_driver_insts $input_nets]
        set fanin_area [sum_unique_inst_area_um2 $upstream_driver_insts $dbu]

        # fanout_area: area of unique load cells
        set load_insts [unique_insts_from_iterms $load_iterms]
        set fanout_area [sum_unique_inst_area_um2 $load_insts $dbu]

        # HPWL bbox based on driver cell and load cells
        set hpwl_insts [concat [list $driver_inst] $load_insts]
        set bbox [bbox_from_insts $hpwl_insts]

        if {$bbox eq ""} {
            incr skipped_nets
            continue
        }

        set hpwl [hpwl_from_bbox_um $bbox $dbu]
        set gcell_area [area_from_bbox_um2 $bbox $dbu]
        set gcell_ar [ar_from_bbox $bbox]

        set cell_density [cell_density_in_bbox $bbox $dbu]

        # Correct net pin capacitance from OpenSTA/OpenROAD, in fF.
        set pin_cap_ff [get_net_pin_cap_ff $net_name]

        puts $fh "[csv_escape $design_name],[csv_escape $pdk_name],[csv_escape $config_id],[csv_escape $net_name],$fanin,$fanout,$fanin_area,$fanout_area,$hpwl,$cell_density,$pin_cap_ff,$gcell_area,$gcell_ar"

        incr written_nets
    }

    close $fh

    puts "Feature extraction complete."
    puts "Total nets   : $total_nets"
    puts "Written nets : $written_nets"
    puts "Skipped nets : $skipped_nets"
    puts "Output CSV   : $out_csv"
}
'''


def tcl_quote(path: Path | str) -> str:
    s = str(path)
    return s.replace("\\", "\\\\").replace('"', '\\"')


def find_run_dirs(dataset_path: Path) -> list[Path]:
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
    return out_dir / run_dir.relative_to(dataset_path)


def output_is_complete(parquet_file: Path) -> bool:
    return parquet_file.exists() and parquet_file.stat().st_size > 0


def make_tcl_script(
    config_file: Path,
    def_file: Path,
    sdc_file: Path,
    csv_file: Path,
    tcl_file: Path,
    density_bin_um: float,
) -> None:
    tcl = f'''
# ============================================================
# Auto-generated OpenROAD postCTS feature extraction script
# ============================================================

source "{tcl_quote(config_file)}"

foreach lef $lef_list {{
    read_lef $lef
}}

read_def "{tcl_quote(def_file)}"

define_corners view

foreach lib $liberty {{
    read_liberty -corner view $lib
}}

set_cmd_units \\
    -time $liberty_time_unit \\
    -capacitance $liberty_cap_unit \\
    -current $liberty_current_unit \\
    -voltage $liberty_voltage_unit \\
    -resistance $liberty_res_unit \\
    -distance um

read_sdc "{tcl_quote(sdc_file)}"

catch {{group_path -name reg2reg -from [all_registers] -to [all_registers]}} group_msg
catch {{group_path -name in2reg  -from [all_inputs]    -to [all_registers]}} group_msg
catch {{group_path -name reg2out -from [all_registers] -to [all_outputs]}} group_msg
catch {{group_path -name in2out  -from [all_inputs]    -to [all_outputs]}} group_msg

set DENSITY_BIN_UM {density_bin_um}

{FEATURE_TCL}

extract_net_features_postcts "{tcl_quote(csv_file)}"

exit
'''

    tcl_file.write_text(tcl)


def convert_csv_to_parquet(csv_file: Path, parquet_file: Path, keep_csv: bool) -> int:
    df = pd.read_csv(csv_file)

    missing_cols = [c for c in REQUIRED_OUTPUT_COLUMNS if c not in df.columns]
    if missing_cols:
        raise RuntimeError(f"Missing columns in CSV: {missing_cols}")

    df = df[REQUIRED_OUTPUT_COLUMNS]

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

    # Validate temporary parquet before replacing.
    pd.read_parquet(
        tmp_file,
        engine="pyarrow",
        columns=["design", "pdk_name", "config_id", "net_name", "pin_cap_ff"],
    )

    tmp_file.replace(parquet_file)

    if not keep_csv:
        csv_file.unlink(missing_ok=True)

    return len(df)


def run_one(
    dataset_path: Path,
    out_dir: Path,
    run_dir: Path,
    openroad_bin: str,
    openroad_threads: int,
    overwrite: bool,
    keep_csv: bool,
    keep_tcl: bool,
    density_bin_um: float,
    timeout_sec: Optional[int],
) -> dict:
    out_run_dir = make_output_run_dir(dataset_path, out_dir, run_dir)
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

    make_tcl_script(
        config_file=config_file,
        def_file=def_file,
        sdc_file=sdc_file,
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

        if not keep_tcl:
            tcl_file.unlink(missing_ok=True)

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
        description="Standalone postCTS net-level feature extraction from raw OpenROAD dataset."
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
        help="OpenROAD executable. Default: openroad.",
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
        help="Density bin size in microns. Default: 10.0.",
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
        "--keep_tcl",
        action="store_true",
        help="Keep generated Tcl files.",
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

    if not dataset_path.exists():
        print(f"ERROR: dataset_path does not exist: {dataset_path}", file=sys.stderr)
        sys.exit(1)

    run_dirs = find_run_dirs(dataset_path)

    run_dirs = run_dirs[args.start_index:]

    if args.limit is not None:
        run_dirs = run_dirs[:args.limit]

    print(f"Found runs: {len(run_dirs)}")
    print(f"Dataset path: {dataset_path}")
    print(f"Output dir: {out_dir}")
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
                openroad_bin=args.openroad_bin,
                openroad_threads=args.openroad_threads,
                overwrite=args.overwrite,
                keep_csv=args.keep_csv,
                keep_tcl=args.keep_tcl,
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
                    args.openroad_bin,
                    args.openroad_threads,
                    args.overwrite,
                    args.keep_csv,
                    args.keep_tcl,
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

    total_nets = 0
    for r in report_rows:
        try:
            total_nets += int(r["num_nets"])
        except Exception:
            pass

    summary = {
        "dataset_path": str(dataset_path),
        "out_dir": str(out_dir),
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