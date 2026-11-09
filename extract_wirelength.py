#!/usr/bin/env python3
"""Extract post-route net wirelength and features into per-run Parquet files.

Required arguments (same interface as extract_spef.py):
    python3 extract_wirelength.py --dataset_path RAW --out_dir OUTPUT

For each run, read technology settings from config/config.tcl, restore routed
DEF in OpenROAD, execute the bundled Tcl feature extractor, and save Parquet.
OpenROAD and the corresponding PDK files must be installed locally.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from tqdm import tqdm


# The original user-provided extract_wirelength.tcl, embedded for standalone use.
EXTRACTOR_TCL = '##EXTRACT NETS WIRELENGTH and FEATURES\n\nproc add_wirelength_to_dict {dict_var report_file} {\n    upvar 1 $dict_var dict_nets\n\n    set fp [open $report_file r]\n    set count 0\n\n    while {[gets $fp line] >= 0} {\n        if {![regexp {^drt:\\s+(\\S+)\\s+(\\S+)\\s+\\d+} $line -> net wl]} {\n            continue\n        }\n\n        # Remove escaping from bus indices: \\[ -> [, \\] -> ]\n        set net [string map [list "\\\\\\[" "\\[" "\\\\\\]" "\\]"] $net]\n\n        if {[dict exists $dict_nets $net]} {\n            dict set dict_nets $net wirelength $wl\n            incr count\n        }\n    }\n\n    close $fp\n\n    puts "Wirelength added for $count nets"\n}\n\n# Gets full name list\nproc get_full_name_list {list} {\n\tset new_list ""\n\t  foreach value $list {\n\t    lappend new_list [get_full_name $value]\n\t  }\t\n\treturn $new_list\n}\n\nproc get_net_driver {net_name} {\n  set db    [ord::get_db]\n  set chip  [$db getChip]\n  set block [$chip getBlock]\n\n  # 0) escape [ ] so OpenDB can find the net\n  set esc_name [string map {"[" "\\\\[" "]" "\\\\]"} $net_name]\n\n  set net [$block findNet $esc_name]\n  if {$net eq "" || $net eq "NULL"} {\n    return ""\n  }\n\n  # 1) CELL driver: OUTPUT iterm\n  foreach it [$net getITerms] {\n    if {[catch { set t [$it getIoType] }]} { continue }\n    if {$t eq "OUTPUT"} {\n      set inst [$it getInst]\n      if {$inst ne ""} { return [$inst getName] }\n    }\n  }\n\n  # 2) PORT driver: INPUT bterm (net of input port)\n  foreach bt [$net getBTerms] {\n    set t "UNKNOWN"\n    catch { set t [$bt getIoType] }\n    if {$t eq "INPUT"} { return [$bt getName] }\n  }\n\n  # 3) fallback: any port on this net (если надо)\n  set bterms [$net getBTerms]\n  if {[llength $bterms] > 0} {\n    return [[lindex $bterms 0] getName]\n  }\n\n  return ""\n}\n\nproc save_nets_csv {filename} {\n    global dict_nets\n\n    if {[dict size $dict_nets] == 0} {\n        puts stderr "dict_nets пуст — нечего записывать"\n        return\n    }\n\n    # --- 1. Собираем все имена атрибутов (объединение ключей) ---\n    set attrSet [dict create]\n    dict for {name params} $dict_nets {\n        dict for {attr val} $params {\n            dict set attrSet $attr 1\n        }\n    }\n    # стабильный порядок колонок; при желании можно задать свой список\n    set attrs [lsort [dict keys $attrSet]]\n\n    # --- 2. Открываем файл и пишем заголовок ---\n    if {[catch {open $filename w} fp]} {\n        error "Не удалось открыть \'$filename\': $fp"\n    }\n    puts $fp "net,[join $attrs ,]"\n\n    # --- 3. Пишем данные ---\n    foreach name [lsort [dict keys $dict_nets]] {\n        set params [dict get $dict_nets $name]\n        set row [list $name]\n        foreach attr $attrs {\n            if {[dict exists $params $attr]} {\n                lappend row [dict get $params $attr]\n            } else {\n                lappend row ""        ;# отсутствующий атрибут -> пустая ячейка\n            }\n        }\n        puts $fp [join $row ,]\n    }\n\n    close $fp\n    puts "Записано [dict size $dict_nets] строк, колонок: [expr {[llength $attrs]+1}]"\n}\n\n#round\nproc tcl::mathfunc::roundto {value sigfigs} {\n\n    # Обработка нуля\n    if {$value == 0.0} {\n        return 0.0\n    }\n\n    # Работаем с модулем (для отрицательных)\n    set sign 1\n    if {$value < 0} {\n        set sign -1\n        set value [expr {abs($value)}]\n    }\n\n    set pow [expr {($sigfigs - 1) - floor(log10($value))}]\n    set result [expr {round(10.0**$pow * $value) / 10.0**$pow}]\n\n    return [expr {$sign * $result}]\n}\n\n\n##db_units_per_micron\nset db [::ord::get_db]\nset block [[$db getChips] getBlock]\nset db_units_per_micron [expr double([$block getDbUnitsPerMicron])]\n#set corner [lindex [sta::corners] 0]\nset corner "view"\n\nset block_llx [expr [[$block getBBox] xMin] / $db_units_per_micron]\nset block_lly [expr [[$block getBBox] yMin] / $db_units_per_micron]\nset block_urx [expr [[$block getBBox] xMax] / $db_units_per_micron]\nset block_ury [expr [[$block getBBox] yMax] / $db_units_per_micron]\n\n#create dictionary to conserve nets features\nset dict_nets {}\n\n#iterate by each net\nforeach net [ get_full_name_list [get_nets -filter "name != VDD && name != VSS"] ] {\n\n\t#get net odb name\n\tset net_odb_name [sta::sta_to_db_net [get_nets $net]]\n\t#get net db name\n\tset net_db_name [get_nets $net]\n\t#get dseign name and vars\n\tdict set dict_nets $net "design" $design\n\tdict set dict_nets $net "CLK_PERIOD" $CLK_PERIOD\n\tdict set dict_nets $net "PDN_HPITCH_TRACK" $PDN_HPITCH_TRACK\n\tdict set dict_nets $net "CU" $CU\n\tdict set dict_nets $net "AR" $AR\n\t#get net terms\n\tdict set dict_nets $net "terms" [$net_odb_name getTermCount]\n\n \t#net is port (1 - true; 0 - false)\n  \tif {[get_ports -quiet $net] ne ""} {\n    \tset is_port 1\n    \tdict set dict_nets $net is_port $is_port\n    \tset port_direction [get_property -object_type port $net direction]\n  \t} else {\n    \tset is_port 0\n    \tdict set dict_nets $net is_port $is_port\n  \t}\n\n\t#net term bbox xmin\n\tdict set dict_nets $net "term_bbox_xmin" [expr [[$net_odb_name getTermBBox] xMin] / $db_units_per_micron]\n\n\t#net term bbox ymin\n\tdict set dict_nets $net "term_bbox_ymin" [expr [[$net_odb_name getTermBBox] yMin] / $db_units_per_micron]\n\n\t#net term bbox xmax\n\tdict set dict_nets $net "term_bbox_xmax" [expr [[$net_odb_name getTermBBox] xMax] / $db_units_per_micron]\n\n\t#net term bbox ymax\n\tdict set dict_nets $net "term_bbox_ymax" [expr [[$net_odb_name getTermBBox] yMax] / $db_units_per_micron]\n\n\t#net bbox dx\n\tdict set dict_nets $net "term_bbox_dx" [expr [[$net_odb_name getTermBBox] dx] / $db_units_per_micron]\n\n\t#net bbox dy\n\tdict set dict_nets $net "term_bbox_dy" [expr [[$net_odb_name getTermBBox] dy] / $db_units_per_micron]\n\n\t#net bbox area\n\tdict set dict_nets $net "term_bbox_area" [expr roundto([[$net_odb_name getTermBBox] area] / (($db_units_per_micron)**2),4)]\n\n\t#net bbox hpwl\n\tdict set dict_nets $net "term_bbox_hpwl" [expr roundto(([[$net_odb_name getTermBBox] dx] + [[$net_odb_name getTermBBox] dy]) / $db_units_per_micron,3)]\n\n\t#net bbox aspect ration X/Y\n\tset dx [expr [[$net_odb_name getTermBBox] dx] / $db_units_per_micron + 1e-8]\n\tset dy [expr [[$net_odb_name getTermBBox] dy] / $db_units_per_micron + 1e-8]\n\tdict set dict_nets $net "term_bbox_ar" [expr roundto([expr $dx / $dy],3)]\n\t\n  \t#wire capacitance estimated by openroad (estimate_parasitics -placement)\n  \tset wire_capacitance_placement [expr roundto([$net_db_name wire_capacitance $corner max],3)]\n  \tdict set dict_nets $net "wire_capacitance_placement" $wire_capacitance_placement\n\n  \t#pins capacitance\n  \tif {$is_port && $port_direction eq "output"} {\n    \tset pin_capacitance 0.0\n  \t} else {\n    \tset pin_capacitance [expr {roundto([$net_db_name pin_capacitance $corner max], 3)}]\n  \t}\n  \tdict set dict_nets $net pin_capacitance $pin_capacitance\n\n\t#driving inst\n  \tif {$is_port eq "0"} {\n    \t#set driving_inst [get_full_name [get_fanin -to $net -levels 1 -only_cells]]\n    \tset driving_inst [get_net_driver $net]\n    \tset driving_inst_db [get_cells [get_net_driver $net]]\n    \tdict set dict_nets $net "driving_inst" $driving_inst\n\n    \t#driving cell\n    \tset driving_cell [[$driving_inst_db liberty_cell] name]\n    \tdict set dict_nets $net "driving_cell" $driving_cell\n\n    \t#driver cell is inverter\n    \tset is_inverter [[$driving_inst_db liberty_cell] is_inverter]\n    \tdict set dict_nets $net "is_inverter" $is_inverter\n\n    \t#driving cell drive strength\n    \tif {[regexp {_(\\d+)$} $driving_cell -> strength]} {\n      \tset driving_cell_strength $strength\n    \t}\n    \tdict set dict_nets $net "driving_cell_strength" $driving_cell_strength\n\n    \t#driver cell is buffer\n    \tset is_buffer [[$driving_inst_db liberty_cell] is_buffer]\n    \tdict set dict_nets $net "is_buffer" $is_buffer\n    \n    \t#fanin (number of driving cell pins)\n    \tset fanin [llength [get_pins -of [get_cells $driving_inst] -filter "direction == input"]]\n    \tdict set dict_nets $net "fanin" $fanin\n\n    \t#driving_cell_area\n    \tset driving_cell_area [expr roundto([get_property -object_type liberty_cell $driving_cell area],3)]\n    \tdict set dict_nets $net "driving_cell_area" $driving_cell_area\n\n    \t#fanout area\n    \tset fanout_area 0\n    \tforeach term [$net_odb_name getITerms] {\n      \tif {[[$term getInst] getName] ne $driving_inst} {\n        \tset inst_area [get_property -object_type liberty_cell [get_lib_cells -of [[$term getInst] getName]] area]\n        \tset fanout_area [expr roundto($fanout_area + $inst_area,4)]\n      \t}\n    \t}\n    \tdict set dict_nets $net "fanout_area" $fanout_area\n\n  \t} else {\n    \tdict set dict_nets $net "driving_inst" "port"\n    \tdict set dict_nets $net "driving_cell" "port"\n    \tdict set dict_nets $net "driving_cell_strength" "port"\n    \tdict set dict_nets $net "is_inverter" "0"\n    \tdict set dict_nets $net "is_buffer" "0"\n    \tdict set dict_nets $net "fanin" "0"\n    \tdict set dict_nets $net "driving_cell_area" "0"\n    \tdict set dict_nets $net "fanout_area" "0"\n  \t}\n\n}\n\n##write nets to csv\n\nsave_nets_csv "nets.csv" \n\n\n\n\n'


@dataclass(frozen=True)
class Job:
    dataset: Path
    run_dir: Path
    out_dir: Path
    extractor: Path
    openroad: str
    overwrite: bool
    parquet_name: str
    timeout: int


def tcl_path(value: Path | str) -> str:
    """Tcl path literal (for ordinary POSIX paths)."""
    s = str(value)
    if any(x in s for x in ('{', '}', '\n', '\r', '\\')):
        raise ValueError(f'Unsupported Tcl path: {s!r}')
    return '{' + s + '}'


def read_run_info(run_dir: Path) -> dict:
    try:
        with (run_dir / 'run_info.csv').open(newline='', encoding='utf-8-sig') as fp:
            return next(csv.DictReader(fp), {})
    except (OSError, UnicodeError):
        return {}


def infer_metadata(dataset: Path, run_dir: Path) -> dict:
    parts = run_dir.relative_to(dataset).parts
    info = read_run_info(run_dir)
    return {
        'design': info.get('design') or parts[1],
        'pdk_name': info.get('pdk_name') or parts[0],
        'config_id': parts[2],
    }


def find_run_dirs(root: Path) -> list[Path]:
    # Same discovery approach as extract_spef.py.
    runs = []
    for info in root.rglob('run_info.csv'):
        parts = info.parent.relative_to(root).parts
        if len(parts) == 3:
            runs.append(info.parent)
    return sorted(set(runs))


def parse_saved_config(path: Path) -> dict[str, str]:
    """Read literal `set name value` assignments, without executing the Tcl.

    Config generated by this RTL-to-GDS flow uses quoted absolute paths.
    Avoid Tcl evaluation because config may include unrelated design commands.
    """
    wanted = {'pdk_path', 'pdk_name', 'tech_lef', 'cells_lef',
              'liberty', 'wire_rc_metal'}
    result = {}
    pattern = re.compile(r'^\s*set\s+([A-Za-z_][A-Za-z_0-9]*)\s+(.*)$')
    with path.open('r', encoding='utf-8', errors='replace') as fp:
        for line in fp:
            m = pattern.match(line)
            if not m or m.group(1) not in wanted:
                continue
            value = m.group(2).strip()
            if value.startswith('"'):
                end = value.find('"', 1)
                if end < 0:
                    continue
                value = value[1:end]
            elif value.startswith('{'):
                end = value.find('}', 1)
                if end < 0:
                    continue
                value = value[1:end]
            else:
                value = value.split('#', 1)[0].strip()
            result[m.group(1)] = value
    return result


def pdk_roots(dataset: Path, run_dir: Path, settings: dict) -> list[Path]:
    """Try original PDK location, environment, and neighboring open_pdk repos."""
    pdk = run_dir.relative_to(dataset).parts[0]
    old_root = settings.get('pdk_path', '')
    roots = []
    if old_root:
        roots.append(Path(os.path.expanduser(os.path.expandvars(old_root))))

    # Optional env var works without adding any CLI parameters.
    env_root = os.environ.get('OPEN_PDK_ROOT', '')
    if env_root:
        x = Path(os.path.expanduser(os.path.expandvars(env_root)))
        roots.extend([x / pdk, x])

    for parent in (dataset, dataset.parent, dataset.parent.parent,
                   Path.cwd(), Path(__file__).resolve().parent):
        roots.extend([parent / 'open_pdk' / pdk, parent / 'open_pdk' / pdk.lower(),
                      parent / 'open_pdk', parent / pdk])

    # Remove duplicate candidates, preserving priority.
    unique = []
    seen = set()
    for path in roots:
        path = path.resolve()
        if str(path) not in seen:
            unique.append(path)
            seen.add(str(path))
    return unique


def resolve_pdk_files(dataset: Path, run_dir: Path) -> dict:
    config_path = run_dir / 'config' / 'config.tcl'
    if not config_path.is_file():
        raise FileNotFoundError(f'Missing {config_path}')
    cfg = parse_saved_config(config_path)
    required = ('tech_lef', 'cells_lef', 'liberty', 'wire_rc_metal')
    missing = [key for key in required if not cfg.get(key)]
    if missing:
        raise ValueError(f'Missing settings in {config_path}: {missing}')

    original_root = Path(cfg['pdk_path']) if cfg.get('pdk_path') else None
    roots = pdk_roots(dataset, run_dir, cfg)

    # Source run config stores three absolute file names under `pdk_path`.
    # If moved to a new workstation, remap the suffix onto discovered local PDK.
    for root in roots:
        result = {}
        for key in ('tech_lef', 'cells_lef', 'liberty'):
            path = Path(os.path.expanduser(os.path.expandvars(cfg[key])))
            if original_root is not None and path.is_absolute():
                try:
                    relative = path.relative_to(original_root)
                    path = root / relative
                except ValueError:
                    pass
            elif not path.is_absolute():
                path = root / path
            result[key] = path.resolve()
        if all(x.is_file() for x in result.values()):
            layer = cfg['wire_rc_metal']
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', layer):
                raise ValueError(f'Invalid wire_rc_metal: {layer!r}')
            return {**result, 'wire_rc_metal': layer, 'pdk_root': str(root)}

    old = original_root if original_root is not None else '(not specified)'
    raise FileNotFoundError(
        f'Cannot find PDK LEF/Liberty (saved pdk_path={old}). '
        f'Place open_pdk next to the dataset or set OPEN_PDK_ROOT to local PDK root.'
    )


def generate_restore_tcl(job: Job, dest: Path, pdk: dict) -> Path:
    def_file = job.run_dir / 'postcts/def/def.def'
    sdc_file = job.run_dir / 'postcts/sdc/sdc.sdc'
    if not def_file.is_file() or not sdc_file.is_file():
        raise FileNotFoundError(f'Missing postcts DEF or SDC: {job.run_dir}')

    config_file = job.run_dir / "config" / "config.tcl"

    # read_def restores detailed routing and the instance/net topology directly.
    # Read Liberty before DEF to bind timing arcs; use the existing 'view' corner.
    lines = [
        '# Auto-generated from run config and raw postcts design.',
        'define_corners view',
        f'read_liberty -corner view {tcl_path(pdk["liberty"])}',
        f'read_lef {tcl_path(pdk["tech_lef"])}',
        f'read_lef {tcl_path(pdk["cells_lef"])}',
        f'read_def {tcl_path(def_file)}',
        f'read_sdc {tcl_path(sdc_file)}',
        f'set_wire_rc -signal -layer {pdk["wire_rc_metal"]}',
        f'set_wire_rc -clock -layer {pdk["wire_rc_metal"]}',
        'estimate_parasitics -placement',
        f'source {tcl_path(config_file)}',
         f'cd {tcl_path(dest)}',
        f'source {tcl_path(job.extractor)}',
        f'cd {tcl_path(dest)}',
        f'source {tcl_path(job.extractor)}',
    ]
    path = dest / 'restore_and_extract.tcl'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def parse_extracted_csv(path: Path, meta: dict) -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(path, low_memory=False)
    if 'net' not in df.columns or 'wirelength' not in df.columns:
        raise ValueError('nets.csv missing required columns net, wirelength')
    num_nets_csv = len(df)
    if not num_nets_csv:
        raise ValueError('No nets extracted')
    df = df.rename(columns={'net': 'net_name'})
    df['net_name'] = df['net_name'].astype('string')
    df['wirelength'] = pd.to_numeric(df['wirelength'], errors='coerce')
    mask = df['wirelength'].notna() & df['wirelength'].ge(0)
    num_labeled = int(mask.sum())
    df = df.loc[mask].copy()
    if df.empty:
        raise ValueError('No nets with valid wirelength labels')
    # OpenSTA/OpenDB object handles are unstable between processes and runs.
    df.drop(columns=['net_db_name', 'net_odb_name'], errors='ignore', inplace=True)
    for key, value in meta.items():
        df[key] = value
    front = ['design', 'pdk_name', 'config_id', 'net_name']
    df = df[front + [c for c in df.columns if c not in front + ['wirelength']] + ['wirelength']]
    return df, {
        'num_nets_csv': num_nets_csv,
        'num_labeled': num_labeled,
        'num_missing_labels': num_nets_csv - num_labeled,
        'num_nets': len(df),
        'wirelength_min': float(df['wirelength'].min()),
        'wirelength_mean': float(df['wirelength'].mean()),
        'wirelength_max': float(df['wirelength'].max()),
    }


def save_parquet(df: pd.DataFrame, filename: Path) -> None:
    temp = filename.with_name(filename.name + '.tmp')
    try:
        df.to_parquet(temp, index=False, engine='pyarrow', compression='zstd')
        pd.read_parquet(temp, engine='pyarrow', columns=[
            'design', 'pdk_name', 'config_id', 'net_name', 'wirelength'])
        temp.replace(filename)
    finally:
        temp.unlink(missing_ok=True)


def process(job: Job) -> dict:
    meta = infer_metadata(job.dataset, job.run_dir)
    dest = job.out_dir / job.run_dir.relative_to(job.dataset)
    output = dest / job.parquet_name
    row = {**meta, 'run_dir': str(job.run_dir), 'out_file': str(output)}
    if output.is_file() and output.stat().st_size and not job.overwrite:
        return {**row, 'status': 'skipped', 'reason': 'output exists', 'num_nets': ''}
    try:
        pdk = resolve_pdk_files(job.dataset, job.run_dir)
        dest.mkdir(parents=True, exist_ok=True)
        runner = generate_restore_tcl(job, dest, pdk)
        raw_csv = dest / 'nets.csv'
        raw_csv.unlink(missing_ok=True)  # prevent consuming an old result after failure
        log_path = dest / 'openroad.log'
        t0 = time.monotonic()
        with log_path.open('w', encoding='utf-8') as log:
            proc = subprocess.run(
                [job.openroad, '-exit', '-no_init', str(runner)],
                cwd=dest, stdout=log, stderr=subprocess.STDOUT,
                timeout=job.timeout or None, check=False)
        elapsed = round(time.monotonic() - t0, 3)
        if proc.returncode:
            raise RuntimeError(f'OpenROAD exit code {proc.returncode}; see {log_path}')
        if not raw_csv.is_file() or not raw_csv.stat().st_size:
            raise RuntimeError(f'OpenROAD did not generate nets.csv; see {log_path}')
        df, stats = parse_extracted_csv(raw_csv, meta)
        save_parquet(df, output)
        return {**row, 'status': 'ok', 'reason': '', 'runtime_s': elapsed, **stats}
    except Exception as e:
        return {**row, 'status': 'failed', 'reason': f'{type(e).__name__}: {e}', 'num_nets': 0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dataset_path', type=Path, required=True)
    ap.add_argument('--out_dir', type=Path, required=True)
    ap.add_argument('--pdk', type=str.lower, choices=['asap7', 'freepdk45', 'sky130', 'gf180'],
                    default=None, help='Process only this PDK (default: all PDKs)')
    ap.add_argument('--parquet_name', default='net_wirelength.parquet')
    ap.add_argument('--overwrite', action='store_true')
    ap.add_argument('--jobs', type=int, default=1,
                    help='Number of simultaneous OpenROAD runs (default: 1)')
    ap.add_argument('--max_runs', type=int, default=0,
                    help='Limit to first N runs for testing (default: all)')
    ap.add_argument('--timeout', type=int, default=0, help='Seconds per run (0: no limit)')
    ap.add_argument('--openroad', default='openroad', help='OpenROAD executable')
    args = ap.parse_args()

    dataset = args.dataset_path.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()
    if not dataset.is_dir():
        print(f'ERROR: dataset_path not found: {dataset}', file=sys.stderr)
        return 2
    if dataset == out_dir or dataset in out_dir.parents:
        print('ERROR: out_dir must be outside dataset_path', file=sys.stderr)
        return 2
    if args.jobs < 1 or args.max_runs < 0:
        print('ERROR: --jobs must be >= 1; --max_runs must be >= 0', file=sys.stderr)
        return 2
    if shutil.which(args.openroad) is None:
        print(f'ERROR: OpenROAD executable not found: {args.openroad}', file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    extractor = out_dir / '_extract_wirelength.tcl'
    extractor.write_text(EXTRACTOR_TCL, encoding='utf-8')
    runs = find_run_dirs(dataset)
    if args.pdk is not None:
        runs = [run for run in runs
                if run.relative_to(dataset).parts[0].lower() == args.pdk]
    if args.max_runs:
        runs = runs[:args.max_runs]
    jobs = [Job(dataset, run, out_dir, extractor, args.openroad,
                args.overwrite, args.parquet_name, args.timeout) for run in runs]
    print(f'Found {len(runs)} runs. jobs={args.jobs}', flush=True)

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        for row in tqdm(executor.map(process, jobs), total=len(jobs),
                        desc='Extracting wirelength'):
            rows.append(row)
    report = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[
        'design', 'pdk_name', 'config_id', 'run_dir', 'out_file',
        'status', 'reason', 'num_nets'])
    report.to_csv(out_dir / 'extraction_report.csv', index=False)
    summary = {
        'dataset_path': str(dataset), 'out_dir': str(out_dir),
        'run_dirs_found': len(runs),
        'runs_ok': sum(r['status'] == 'ok' for r in rows),
        'runs_failed': sum(r['status'] == 'failed' for r in rows),
        'runs_skipped': sum(r['status'] == 'skipped' for r in rows),
        'total_nets': sum(int(r.get('num_nets', 0) or 0) for r in rows),
        'total_labeled_nets': sum(int(r.get('num_labeled', 0) or 0) for r in rows),
    }
    (out_dir / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f'Report: {out_dir / "extraction_report.csv"}')
    return 1 if summary['runs_failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
