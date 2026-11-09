##EXTRACT NETS WIRELENGTH and FEATURES

proc add_wirelength_to_dict {dict_var report_file} {
    upvar 1 $dict_var dict_nets

    set fp [open $report_file r]
    set count 0

    while {[gets $fp line] >= 0} {
        if {![regexp {^drt:\s+(\S+)\s+(\S+)\s+\d+} $line -> net wl]} {
            continue
        }

        # Remove escaping from bus indices: \[ -> [, \] -> ]
        set net [string map [list "\\\[" "\[" "\\\]" "\]"] $net]

        if {[dict exists $dict_nets $net]} {
            dict set dict_nets $net wirelength $wl
            incr count
        }
    }

    close $fp

    puts "Wirelength added for $count nets"
}

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

proc save_nets_csv {filename} {
    global dict_nets

    if {[dict size $dict_nets] == 0} {
        puts stderr "dict_nets пуст — нечего записывать"
        return
    }

    # --- 1. Собираем все имена атрибутов (объединение ключей) ---
    set attrSet [dict create]
    dict for {name params} $dict_nets {
        dict for {attr val} $params {
            dict set attrSet $attr 1
        }
    }
    # стабильный порядок колонок; при желании можно задать свой список
    set attrs [lsort [dict keys $attrSet]]

    # --- 2. Открываем файл и пишем заголовок ---
    if {[catch {open $filename w} fp]} {
        error "Не удалось открыть '$filename': $fp"
    }
    puts $fp "net,[join $attrs ,]"

    # --- 3. Пишем данные ---
    foreach name [lsort [dict keys $dict_nets]] {
        set params [dict get $dict_nets $name]
        set row [list $name]
        foreach attr $attrs {
            if {[dict exists $params $attr]} {
                lappend row [dict get $params $attr]
            } else {
                lappend row ""        ;# отсутствующий атрибут -> пустая ячейка
            }
        }
        puts $fp [join $row ,]
    }

    close $fp
    puts "Записано [dict size $dict_nets] строк, колонок: [expr {[llength $attrs]+1}]"
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
set block [[$db getChips] getBlock]
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
	dict set dict_nets $net "net_odb_name" $net_odb_name

	#get net db name
	set net_db_name [get_nets $net]
	dict set dict_nets $net "net_db_name" $net_db_name

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

##report nets wirelnegth to file
report_wire_length -detailed_route -net [ get_full_name_list [get_nets -filter "name != VDD && name != VSS"] ] -file report.txt > ./report.txt

##add nets wirelength to dictionary
add_wirelength_to_dict dict_nets report.txt

##write nets to csv

save_nets_csv "nets.csv" 




