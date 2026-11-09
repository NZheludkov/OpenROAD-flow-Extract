#!/usr/bin/env python3
"""Postcts features + detailed-route wirelength labels, per physical run.

python3 extract_wirelength_postcts_route.py --dataset_path RAW --out_dir OUTPUT
Requires OpenROAD. Parquet requires pandas + pyarrow; --csv_only is stdlib-only.
Inputs default to postcts/def/def.def and route/def/def.def under each run.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import csv
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback

FEATURES_TCL = r'''
# Gets full name list
proc get_full_name_list {list} {
	set new_list ""
	  foreach value $list {
	    lappend new_list [get_full_name $value]
	  }	
	return $new_list
}

proc get_net_driver {net_name} {
  set db    [ord::get_db]
  set chip  [$db getChip]
  set block [$chip getBlock]

  # 0) escape [ ] so OpenDB can find the net
  set esc_name [string map {"[" "\\[" "]" "\\]"} $net_name]

  set net [$block findNet $esc_name]
  if {$net eq "" || $net eq "NULL"} {
    return ""
  }

  # 1) CELL driver: OUTPUT iterm
  foreach it [$net getITerms] {
    if {[catch { set t [$it getIoType] }]} { continue }
    if {$t eq "OUTPUT"} {
      set inst [$it getInst]
      if {$inst ne ""} { return [$inst getName] }
    }
  }

  # 2) PORT driver: INPUT bterm (net of input port)
  foreach bt [$net getBTerms] {
    set t "UNKNOWN"
    catch { set t [$bt getIoType] }
    if {$t eq "INPUT"} { return [$bt getName] }
  }

  # 3) fallback: any port on this net (если надо)
  set bterms [$net getBTerms]
  if {[llength $bterms] > 0} {
    return [[lindex $bterms 0] getName]
  }

  return ""
}

proc csv_field {value} {
    if {[regexp {[,"\r\n]} $value]} {
        return "\"[string map [list \" \"\"] $value]\""
    }
    return $value
}
proc save_nets_csv {filename} {
    global dict_nets
    if {[dict size $dict_nets] == 0} {error "No postcts nets extracted"}
    set attrSet [dict create]
    dict for {name params} $dict_nets {
        dict for {attr value} $params {dict set attrSet $attr 1}
    }
    set attrs [lsort [dict keys $attrSet]]
    set fp [open $filename w]
    puts $fp [join [linsert $attrs 0 net] ,]
    foreach name [lsort [dict keys $dict_nets]] {
        set params [dict get $dict_nets $name]
        set row [list [csv_field $name]]
        foreach attr $attrs {
            set value ""
            if {[dict exists $params $attr]} {set value [dict get $params $attr]}
            lappend row [csv_field $value]
        }
        puts $fp [join $row ,]
    }
    close $fp
}

#round
proc tcl::mathfunc::roundto {value sigfigs} {

    # Обработка нуля
    if {$value == 0.0} {
        return 0.0
    }

    # Работаем с модулем (для отрицательных)
    set sign 1
    if {$value < 0} {
        set sign -1
        set value [expr {abs($value)}]
    }

    set pow [expr {($sigfigs - 1) - floor(log10($value))}]
    set result [expr {round(10.0**$pow * $value) / 10.0**$pow}]

    return [expr {$sign * $result}]
}


##db_units_per_micron
set db [::ord::get_db]
set block [[$db getChip] getBlock]
set db_units_per_micron [expr double([$block getDbUnitsPerMicron])]
#set corner [lindex [sta::corners] 0]
set corner "view"

set block_llx [expr [[$block getBBox] xMin] / $db_units_per_micron]
set block_lly [expr [[$block getBBox] yMin] / $db_units_per_micron]
set block_urx [expr [[$block getBBox] xMax] / $db_units_per_micron]
set block_ury [expr [[$block getBBox] yMax] / $db_units_per_micron]

#create dictionary to conserve nets features
set dict_nets {}

#iterate by each net
foreach net [ get_full_name_list [get_nets -filter "name != VDD && name != VSS"] ] {

	#get net odb name
	set net_odb_name [sta::sta_to_db_net [get_nets $net]]
	#get net db name
	set net_db_name [get_nets $net]
	#get dseign name and vars
	dict set dict_nets $net "design" $design
	dict set dict_nets $net "CLK_PERIOD" $CLK_PERIOD
	dict set dict_nets $net "PDN_HPITCH_TRACK" $PDN_HPITCH_TRACK
	dict set dict_nets $net "CU" $CU
	dict set dict_nets $net "AR" $AR
	#get net terms
	dict set dict_nets $net "terms" [$net_odb_name getTermCount]

 	#net is port (1 - true; 0 - false)
  	if {[get_ports -quiet $net] ne ""} {
    	set is_port 1
    	dict set dict_nets $net is_port $is_port
    	set port_direction [get_property -object_type port $net direction]
  	} else {
    	set is_port 0
    	dict set dict_nets $net is_port $is_port
  	}

	#net term bbox xmin
	dict set dict_nets $net "term_bbox_xmin" [expr [[$net_odb_name getTermBBox] xMin] / $db_units_per_micron]

	#net term bbox ymin
	dict set dict_nets $net "term_bbox_ymin" [expr [[$net_odb_name getTermBBox] yMin] / $db_units_per_micron]

	#net term bbox xmax
	dict set dict_nets $net "term_bbox_xmax" [expr [[$net_odb_name getTermBBox] xMax] / $db_units_per_micron]

	#net term bbox ymax
	dict set dict_nets $net "term_bbox_ymax" [expr [[$net_odb_name getTermBBox] yMax] / $db_units_per_micron]

	#net bbox dx
	dict set dict_nets $net "term_bbox_dx" [expr [[$net_odb_name getTermBBox] dx] / $db_units_per_micron]

	#net bbox dy
	dict set dict_nets $net "term_bbox_dy" [expr [[$net_odb_name getTermBBox] dy] / $db_units_per_micron]

	#net bbox area
	dict set dict_nets $net "term_bbox_area" [expr roundto([[$net_odb_name getTermBBox] area] / (($db_units_per_micron)**2),4)]

	#net bbox hpwl
	dict set dict_nets $net "term_bbox_hpwl" [expr roundto(([[$net_odb_name getTermBBox] dx] + [[$net_odb_name getTermBBox] dy]) / $db_units_per_micron,3)]

	#net bbox aspect ration X/Y
	set dx [expr [[$net_odb_name getTermBBox] dx] / $db_units_per_micron + 1e-8]
	set dy [expr [[$net_odb_name getTermBBox] dy] / $db_units_per_micron + 1e-8]
	dict set dict_nets $net "term_bbox_ar" [expr roundto([expr $dx / $dy],3)]
	
  	#wire capacitance estimated by openroad (estimate_parasitics -placement)
  	set wire_capacitance_placement [expr roundto([$net_db_name wire_capacitance $corner max],3)]
  	dict set dict_nets $net "wire_capacitance_placement" $wire_capacitance_placement

  	#pins capacitance
  	if {$is_port && $port_direction eq "output"} {
    	set pin_capacitance 0.0
  	} else {
    	set pin_capacitance [expr {roundto([$net_db_name pin_capacitance $corner max], 3)}]
  	}
  	dict set dict_nets $net pin_capacitance $pin_capacitance

	#driving inst
  	if {$is_port eq "0"} {
    	#set driving_inst [get_full_name [get_fanin -to $net -levels 1 -only_cells]]
    	set driving_inst [get_net_driver $net]
    	set driving_inst_db [get_cells [get_net_driver $net]]
    	dict set dict_nets $net "driving_inst" $driving_inst

    	#driving cell
    	set driving_cell [[$driving_inst_db liberty_cell] name]
    	dict set dict_nets $net "driving_cell" $driving_cell

    	#driver cell is inverter
    	set is_inverter [[$driving_inst_db liberty_cell] is_inverter]
    	dict set dict_nets $net "is_inverter" $is_inverter

    	#driving cell drive strength
    	set driving_cell_strength ""
    	if {[regexp {_(\d+)$} $driving_cell -> strength]} {
      	set driving_cell_strength $strength
    	}
    	dict set dict_nets $net "driving_cell_strength" $driving_cell_strength

    	#driver cell is buffer
    	set is_buffer [[$driving_inst_db liberty_cell] is_buffer]
    	dict set dict_nets $net "is_buffer" $is_buffer
    
    	#fanin (number of driving cell pins)
    	set fanin [llength [get_pins -of [get_cells $driving_inst] -filter "direction == input"]]
    	dict set dict_nets $net "fanin" $fanin

    	#driving_cell_area
    	set driving_cell_area [expr roundto([get_property -object_type liberty_cell $driving_cell area],3)]
    	dict set dict_nets $net "driving_cell_area" $driving_cell_area

    	#fanout area
    	set fanout_area 0
    	foreach term [$net_odb_name getITerms] {
      	if {[[$term getInst] getName] ne $driving_inst} {
        	set inst_area [get_property -object_type liberty_cell [get_lib_cells -of [[$term getInst] getName]] area]
        	set fanout_area [expr roundto($fanout_area + $inst_area,4)]
      	}
    	}
    	dict set dict_nets $net "fanout_area" $fanout_area

  	} else {
    	dict set dict_nets $net "driving_inst" "port"
    	dict set dict_nets $net "driving_cell" "port"
    	dict set dict_nets $net "driving_cell_strength" "port"
    	dict set dict_nets $net "is_inverter" "0"
    	dict set dict_nets $net "is_buffer" "0"
    	dict set dict_nets $net "fanin" "0"
    	dict set dict_nets $net "driving_cell_area" "0"
    	dict set dict_nets $net "fanout_area" "0"
  	}

}

##write nets to csv

save_nets_csv "nets_postcts.csv" 




'''


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



# Use -file alone: on OpenROAD 26Q2 it writes the drt: table directly.
# Shell-style > inside a sourced Tcl script is not needed.
LABELS_TCL = r'''
proc get_full_name_list {objects} {
    set names {}
    foreach object $objects {lappend names [get_full_name $object]}
    return $names
}
proc csv_field {value} {
    if {[regexp {[,"\r\n]} $value]} {
        return "\"[string map [list \" \"\"] $value]\""
    }
    return $value
}
proc add_wirelength_to_dict {dict_var report_file} {
    upvar 1 $dict_var dict_nets
    set fp [open $report_file r]
    set count 0
    set unknown 0
    set seen {}
    while {[gets $fp line] >= 0} {
        if {![regexp {^\s*drt:\s+(\S+)\s+(\S+)\s+\d+} $line -> net wl]} {
            continue
        }
        # Use exact names first; unescape bus brackets only as fallback.
        if {![dict exists $dict_nets $net]} {
            set net [string map [list {\[} {[} {\]} {]}] $net]
        }
        if {![dict exists $dict_nets $net]} {incr unknown; continue}
        if {![string is double -strict $wl]} {error "Invalid wirelength: $line"}
        if {[catch {expr {$wl >= 0.0 && $wl < Inf}} valid] || !$valid} {
            error "Nonfinite or negative wirelength: $line"
        }
        if {[dict exists $seen $net]} {error "Duplicate detailed-route entry for $net"}
        dict set seen $net 1
        dict set dict_nets $net wirelength $wl
        incr count
    }
    close $fp
    puts "Wirelength added for $count route nets; unmatched report rows: $unknown"
    if {$unknown} {error "Some route report names cannot be matched to route nets"}
    return $count
}
set names [get_full_name_list [get_nets -filter "name != VDD && name != VSS"]]
if {![llength $names]} {error "No route nets available"}
set dict_nets {}
foreach net $names {
    set key [string map [list {\[} {[} {\]} {]}] $net]
    if {[dict exists $dict_nets $key]} {error "Ambiguous route net name after unescaping: $net"}
    dict set dict_nets $key [dict create]
}
report_wire_length -detailed_route -net $names -file wirelength_route_report.txt
set count [add_wirelength_to_dict dict_nets wirelength_route_report.txt]
if {$count == 0} {
    error "No drt: entries parsed; inspect wirelength_route_report.txt"
}
set fp [open wirelength_route.csv w]
puts $fp "net,wirelength"
foreach net [lsort [dict keys $dict_nets]] {
    if {[dict exists $dict_nets $net wirelength]} {
        puts $fp "[csv_field $net],[dict get $dict_nets $net wirelength]"
    }
}
close $fp
'''


def canonical_net(name: str) -> str:
    """Only the bus escapes handled in the supplied Tcl; never strip other '\\'."""
    return name.replace('\\[', '[').replace('\\]', ']')


def merge_csv(features: Path, labels: Path, output: Path, meta: dict,
              keep_unmatched: bool, min_match_fraction: float) -> dict:
    lengths = {}
    route_names = {}
    with labels.open(newline='', encoding='utf-8') as fp:
        reader = csv.DictReader(fp)
        if not {'net', 'wirelength'} <= set(reader.fieldnames or []):
            raise ValueError('Route CSV must contain net,wirelength')
        for row in reader:
            key = canonical_net(row['net'])
            if key in lengths:
                raise ValueError(f'Ambiguous/duplicate route net: {row["net"]!r}, {route_names[key]!r}')
            value = float(row['wirelength'])
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'Invalid route length for {key!r}')
            lengths[key] = row['wirelength']
            route_names[key] = row['net']
    if not lengths:
        raise ValueError('No route labels')
    tmp = output.with_name(output.name+'.tmp')
    missing_file = output.with_name('unmatched_postcts_nets.csv')
    extra_file = output.with_name('route_only_nets.csv')
    seen = set()
    num_features = matched = 0
    total_length = 0.0
    wl_min = math.inf
    wl_max = -math.inf
    try:
        with features.open(newline='', encoding='utf-8-sig') as src, \
             tmp.open('w', newline='', encoding='utf-8') as dst, \
             missing_file.open('w', newline='', encoding='utf-8') as unmatched:
            reader = csv.DictReader(src)
            fields = reader.fieldnames or []
            name_col = 'net' if 'net' in fields else 'net_name' if 'net_name' in fields else None
            if name_col is None:
                raise ValueError('Feature CSV has neither net nor net_name')
            if len(fields) != len(set(fields)):
                raise ValueError('Duplicate feature CSV column names')
            # Drop an old label if this is an update of an existing feature CSV.
            fields = [c for c in fields if c != 'wirelength']
            fields += [k for k in meta if k not in fields]
            fields += ['wirelength']
            writer = csv.DictWriter(dst, fieldnames=fields)
            missing_writer = csv.writer(unmatched)
            writer.writeheader()
            missing_writer.writerow(['net'])
            for row in reader:
                if None in row:
                    raise ValueError('Malformed feature CSV row (unquoted comma or excess fields)')
                name = row[name_col]
                key = canonical_net(name)
                if key in seen:
                    raise ValueError(f'Ambiguous/duplicate postcts net: {name!r}')
                seen.add(key)
                num_features += 1
                row.update(meta)
                row['wirelength'] = lengths.get(key, '')
                if key in lengths:
                    value = float(lengths[key])
                    total_length += value
                    wl_min = min(wl_min, value)
                    wl_max = max(wl_max, value)
                    matched += 1
                else:
                    missing_writer.writerow([name])
                # CSV always preserves every postcts row. Parquet filtering is separate.
                writer.writerow(row)
        if not num_features or not matched:
            raise ValueError('No matching postcts/route nets; refusing an unlabeled dataset')
        match_fraction = matched/num_features
        if match_fraction < min_match_fraction:
            raise ValueError(f'Label match fraction {match_fraction:.4f} < {min_match_fraction:.4f}')
        with extra_file.open('w', newline='', encoding='utf-8') as fp:
            writer = csv.writer(fp)
            writer.writerow(['net','wirelength'])
            for key, value in lengths.items():
                if key not in seen:
                    writer.writerow([route_names[key],value])
        tmp.replace(output)
    finally:
        tmp.unlink(missing_ok=True)
    return dict(num_nets_csv=num_features, num_labeled=matched,
                num_missing_labels=num_features-matched,
                num_route_labels=len(lengths), num_route_only=len(set(lengths)-seen),
                label_match_fraction=match_fraction,
                num_nets=num_features if keep_unmatched else matched,
                wirelength_min=wl_min,wirelength_mean=total_length/matched,wirelength_max=wl_max)


def save_parquet(csv_path: Path, parquet_path: Path, keep_unmatched: bool) -> None:
    import pandas as pd
    # Preserve text identifiers such as NA, numeric-looking names, leading zeros.
    with csv_path.open(newline='', encoding='utf-8') as fp:
        fields = next(csv.reader(fp))
    names = {c:'string' for c in ['net','net_name','design','pdk_name','config_id',
                                 'driving_cell','driving_inst'] if c in fields}
    df = pd.read_csv(csv_path, dtype=names, keep_default_na=False, low_memory=False)
    if 'net' in df:
        df = df.rename(columns={'net':'net_name'})
    df['wirelength'] = pd.to_numeric(df['wirelength'], errors='coerce')
    if not keep_unmatched:
        df = df.loc[df['wirelength'].notna()].copy()
    # Keep names textual, convert numeric/boolean features when all present tokens are valid.
    text_cols = set(names)-{'net'} | {'net_name'}
    for c in df:
        if c in text_cols or c=='wirelength':
            continue
        values = df[c].replace({'true':'1','false':'0','True':'1','False':'0','':None})
        numeric = pd.to_numeric(values, errors='coerce')
        if int(numeric.notna().sum()) == int(values.notna().sum()):
            df[c] = numeric
    tmp = parquet_path.with_name(parquet_path.name+'.tmp')
    try:
        df.to_parquet(tmp,index=False,engine='pyarrow',compression='zstd')
        tmp.replace(parquet_path)
    finally:
        tmp.unlink(missing_ok=True)


def generate_restore_tcl(run_dir: Path, work: Path, pdk: dict, stage: str,
                         extractor: Path, args) -> Path:
    rel_def = args.postcts_def if stage=='postcts' else args.route_def
    rel_sdc = args.postcts_sdc if stage=='postcts' else args.route_sdc
    def_file = run_dir/rel_def
    sdc_file = run_dir/rel_sdc
    if not def_file.is_file():
        raise FileNotFoundError(f'Missing {stage} DEF: {def_file}')
    if stage=='postcts' and not sdc_file.is_file():
        raise FileNotFoundError(f'Missing postcts SDC: {sdc_file}')
    lines = [
        '# Independent OpenROAD process; no state from the other stage.',
        'define_corners view',
        f'read_liberty -corner view {tcl_path(pdk["liberty"])}',
        f'read_lef {tcl_path(pdk["tech_lef"])}',
        f'read_lef {tcl_path(pdk["cells_lef"])}',
        f'read_def {tcl_path(def_file)}',
    ]
    if sdc_file.is_file():
        lines.append(f'read_sdc {tcl_path(sdc_file)}')
    if stage=='postcts':
        lines += [f'set_wire_rc -signal -layer {pdk["wire_rc_metal"]}',
                  f'set_wire_rc -clock -layer {pdk["wire_rc_metal"]}',
                  'estimate_parasitics -placement',
                  # Preserve the existing feature extraction configuration workflow.
                  f'source {tcl_path(run_dir/"config"/"config.tcl")}']
    # Geometric route labels do not require SPEF or estimate_parasitics.
    lines += [f'cd {tcl_path(work)}', f'source {tcl_path(extractor)}']
    # Explicit catch makes a Tcl failure produce a nonzero process status.
    script = '\n'.join(lines)
    path = work/'restore_and_extract.tcl'
    path.write_text('if {[catch {\n'+script+'\n} message options]} {\n'
                    '    puts stderr $message\n'
                    '    if {[dict exists $options -errorinfo]} {puts stderr [dict get $options -errorinfo]}\n'
                    '    exit 1\n}\nexit 0\n',encoding='utf-8')
    return path


def run_openroad(run_dir: Path, work: Path, pdk: dict, stage: str,
                 extractor: Path, args, expected: str) -> float:
    work.mkdir(parents=True,exist_ok=True)
    runner = generate_restore_tcl(run_dir,work,pdk,stage,extractor,args)
    (work/expected).unlink(missing_ok=True)
    if stage=='route':
        for name in ('wirelength_route_raw.csv','wirelength_route_report.txt'):
            (work/name).unlink(missing_ok=True)
    start = time.monotonic()
    log_path = work/'openroad.log'
    with log_path.open('w',encoding='utf-8') as log:
        result = subprocess.run([args.openroad,'-exit','-no_init',str(runner)],
                                cwd=work,stdout=log,stderr=subprocess.STDOUT,
                                timeout=args.timeout or None,check=False)
    if result.returncode:
        raise RuntimeError(f'OpenROAD {stage} exit={result.returncode}; see {log_path}')
    if not (work/expected).is_file() or not (work/expected).stat().st_size:
        raise RuntimeError(f'OpenROAD {stage} did not produce {expected}; see {log_path}')
    return round(time.monotonic()-start,3)


def process(run_dir: Path, args, feature_extractor: Path, label_extractor: Path) -> dict:
    meta = infer_metadata(args.dataset_path,run_dir)
    dest = args.out_dir/run_dir.relative_to(args.dataset_path)
    output = dest/args.parquet_name
    marker = dest/'extraction_info.json'
    row = {**meta,'run_dir':str(run_dir),'out_file':str(dest/'nets.csv') if args.csv_only else str(output)}
    if marker.is_file() and not args.overwrite:
        try:
            done = json.loads(marker.read_text())
            compatible = (done.get('status')=='ok'
                          and done.get('feature_stage')=='postcts'
                          and done.get('label_stage')=='route'
                          and done.get('keep_unmatched')==args.keep_unmatched
                          and done.get('postcts_def')==args.postcts_def
                          and done.get('route_def')==args.route_def)
            if compatible and (dest/'nets.csv').is_file() and (args.csv_only or output.is_file()):
                return {**row,'status':'skipped','reason':'completed output exists','num_nets':0}
        except (OSError,ValueError):
            pass
    start = time.monotonic()
    try:
        dest.mkdir(parents=True,exist_ok=True)
        pdk = resolve_pdk_files(args.dataset_path,run_dir)
        # Validate route input before spending time extracting postcts features.
        if not (run_dir/args.route_def).is_file():
            raise FileNotFoundError(f'Missing route DEF: {run_dir/args.route_def}')
        feature_file = dest/'postcts'/'nets_postcts.csv'
        post_sec = 0.0
        if args.labels_only:
            if not feature_file.is_file():
                candidates = [dest/'nets_postcts.csv',dest/'nets.csv']
                original = next((p for p in candidates if p.is_file()),None)
                if original is None:
                    raise FileNotFoundError(f'No existing postcts feature CSV in {dest}')
                feature_file.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(original,feature_file)
        else:
            post_sec = run_openroad(run_dir,dest/'postcts',pdk,'postcts',
                                   feature_extractor,args,'nets_postcts.csv')
        route_sec = run_openroad(run_dir,dest/'route',pdk,'route',label_extractor,args,'wirelength_route.csv')
        stats = merge_csv(feature_file,dest/'route'/'wirelength_route.csv',dest/'nets.csv',
                          meta,args.keep_unmatched,args.min_match_fraction)
        if not args.csv_only:
            save_parquet(dest/'nets.csv',output,args.keep_unmatched)
        result = {**row,'status':'ok','reason':'',**stats,
                  'postcts_runtime_s':post_sec,'route_runtime_s':route_sec,
                  'runtime_s':round(time.monotonic()-start,3),
                  'feature_stage':'postcts','label_stage':'route',
                  'wirelength_source':'report_wire_length -detailed_route',
                  'wirelength_unit':'um','keep_unmatched':args.keep_unmatched,
                  'postcts_def':args.postcts_def,'route_def':args.route_def}
        marker.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        return result
    except Exception as exc:
        result = {**row,'status':'failed','reason':f'{type(exc).__name__}: {exc}','num_nets':0}
        if dest.is_dir():
            (dest/'failure.log').write_text(traceback.format_exc(),encoding='utf-8')
            # An old success marker must not describe a failed overwrite.
            marker.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dataset_path',type=Path,required=True)
    ap.add_argument('--out_dir',type=Path,required=True)
    ap.add_argument('--pdk',type=str.lower,choices=['asap7','freepdk45','sky130','gf180'])
    ap.add_argument('--openroad',default='openroad')
    ap.add_argument('--parquet_name',default='net_wirelength.parquet')
    ap.add_argument('--jobs',type=int,default=1)
    ap.add_argument('--max_runs',type=int,default=0)
    ap.add_argument('--timeout',type=int,default=0,help='Seconds per OpenROAD stage; 0 = unlimited')
    ap.add_argument('--overwrite',action='store_true')
    ap.add_argument('--labels_only',action='store_true',help='Reuse existing postcts nets.csv under out_dir')
    ap.add_argument('--csv_only',action='store_true',help='Do not write Parquet; no Python packages required')
    ap.add_argument('--keep_unmatched',action='store_true',help='Keep rows without labels in Parquet (CSV always keeps them)')
    ap.add_argument('--min_match_fraction',type=float,default=0.0,help='Fail when matched/num_postcts_nets is below this threshold')
    ap.add_argument('--postcts_def',default='postcts/def/def.def')
    ap.add_argument('--postcts_sdc',default='postcts/sdc/sdc.sdc')
    ap.add_argument('--route_def',default='route/def/def.def')
    ap.add_argument('--route_sdc',default='route/sdc/sdc.sdc')
    args = ap.parse_args(argv)
    args.dataset_path = args.dataset_path.expanduser().resolve()
    args.out_dir = args.out_dir.expanduser().resolve()
    if not args.dataset_path.is_dir():
        ap.error('dataset_path does not exist')
    if args.out_dir==args.dataset_path or args.dataset_path in args.out_dir.parents:
        ap.error('out_dir must be outside dataset_path')
    if args.jobs<1 or args.max_runs<0 or args.timeout<0 or not 0<=args.min_match_fraction<=1:
        ap.error('Invalid jobs/max_runs/timeout/min_match_fraction')
    if Path(args.parquet_name).name!=args.parquet_name:
        ap.error('parquet_name must be a basename')
    if shutil.which(args.openroad) is None:
        ap.error(f'OpenROAD not found: {args.openroad}')
    if not args.csv_only:
        try:
            import pandas
            import pyarrow
        except ImportError:
            ap.error('Install pandas and pyarrow, or use --csv_only')
    args.out_dir.mkdir(parents=True,exist_ok=True)
    feature_extractor = args.out_dir/'_extract_features_postcts.tcl'
    label_extractor = args.out_dir/'_extract_labels_route.tcl'
    feature_extractor.write_text(FEATURES_TCL,encoding='utf-8')
    label_extractor.write_text(LABELS_TCL,encoding='utf-8')
    runs = find_run_dirs(args.dataset_path)
    if args.pdk:
        runs = [p for p in runs if p.relative_to(args.dataset_path).parts[0].lower()==args.pdk]
    if args.max_runs:
        runs = runs[:args.max_runs]
    if not runs:
        print('ERROR: no run_info.csv at dataset/PDK/design/config',file=sys.stderr)
        return 2
    print(f'Found {len(runs)} runs; jobs={args.jobs}; labels_only={args.labels_only}',flush=True)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(process,p,args,feature_extractor,label_extractor) for p in runs]
        for i, future in enumerate(concurrent.futures.as_completed(futures),1):
            row = future.result()
            rows.append(row)
            detail = (f"matched={row['num_labeled']}/{row['num_nets_csv']}, route_only={row['num_route_only']}"
                      if row['status']=='ok' else row['reason'])
            print(f'[{i}/{len(runs)}] {row["status"]}: {row["run_dir"]} {detail}',flush=True)
    rows.sort(key=lambda r:r['run_dir'])
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with (args.out_dir/'extraction_report.csv').open('w',newline='',encoding='utf-8') as fp:
        writer = csv.DictWriter(fp,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    summary = dict(dataset_path=str(args.dataset_path),out_dir=str(args.out_dir),
                   run_dirs_found=len(runs),runs_ok=sum(r['status']=='ok' for r in rows),
                   runs_failed=sum(r['status']=='failed' for r in rows),
                   runs_skipped=sum(r['status']=='skipped' for r in rows),
                   total_labeled_nets=sum(r.get('num_labeled',0) for r in rows),
                   total_missing_labels=sum(r.get('num_missing_labels',0) for r in rows))
    (args.out_dir/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2),flush=True)
    return int(summary['runs_failed']>0)


if __name__=='__main__':
    raise SystemExit(main())
