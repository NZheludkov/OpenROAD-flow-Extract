# ============================================================
# extract_net_features_prects.tcl
#
# Net-level feature extraction for post-route capacitance
# prediction using preCTS design state.
#
# Expected to be run inside OpenROAD after the preCTS design
# is loaded.
#
# Required loaded data:
#   - LEF
#   - Liberty
#   - Verilog / linked design, recommended for pin_cap
#   - preCTS DEF
#
# Output features:
#   design
#   pdk_name
#   config_id
#   net_name
#   fanin
#   fanout
#   fanin_area
#   fanout_area
#   hpwl
#   cell_density
#   pin_cap
#   gcell_area
#   gcell_ar
# ============================================================


# ----------------------------
# User-tunable parameters
# ----------------------------

# Density is computed approximately using cell-center binning.
# Larger bin -> faster, smoother density.
# Smaller bin -> more local, slower.
if {![info exists DENSITY_BIN_UM]} {
    set DENSITY_BIN_UM 10.0
}

# Skip nets with more than one cell driver.
# For first ML dataset, this is usually cleaner.
if {![info exists SKIP_MULTI_DRIVER_NETS]} {
    set SKIP_MULTI_DRIVER_NETS 1
}

# Skip nets without cell driver or without cell loads.
if {![info exists SKIP_NON_CELL_NETS]} {
    set SKIP_NON_CELL_NETS 1
}


# ----------------------------
# Basic helpers
# ----------------------------

proc is_null {obj} {
    return [expr {$obj eq "" || $obj eq "NULL" || $obj eq "0"}]
}


proc csv_escape {s} {
    set s [string map {\" \"\"} $s]
    return "\"$s\""
}


proc get_dbu {} {
    set block [ord::get_db_block]
    return [$block getDbUnitsPerMicron]
}


proc dbu_to_um {x dbu} {
    return [expr {double($x) / double($dbu)}]
}


proc get_box_coords {box} {
    set xmin [$box xMin]
    set ymin [$box yMin]
    set xmax [$box xMax]
    set ymax [$box yMax]
    return [list $xmin $ymin $xmax $ymax]
}


proc inst_box {inst} {
    set box [$inst getBBox]
    return [get_box_coords $box]
}


proc inst_area_um2 {inst dbu} {
    lassign [inst_box $inst] xmin ymin xmax ymax

    set w_um [expr {double($xmax - $xmin) / double($dbu)}]
    set h_um [expr {double($ymax - $ymin) / double($dbu)}]

    if {$w_um < 0.0 || $h_um < 0.0} {
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


proc mterm_name {iterm} {
    set mterm [$iterm getMTerm]
    if {[is_null $mterm]} {
        return ""
    }
    return [$mterm getName]
}


proc is_signal_net {net} {
    if {[is_null $net]} {
        return 0
    }

    if {[catch {set sig_type [$net getSigType]}]} {
        return 1
    }

    # Usually signal nets have type SIGNAL.
    # Clock nets may also be represented as SIGNAL depending on the flow.
    if {$sig_type eq "POWER" || $sig_type eq "GROUND"} {
        return 0
    }

    return 1
}


proc unique_insts_from_iterms {iterms} {
    set inst_dict [dict create]

    foreach iterm $iterms {
        set inst [$iterm getInst]
        if {[is_null $inst]} {
            continue
        }

        set inst_name [$inst getName]
        dict set inst_dict $inst_name $inst
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


# ----------------------------
# Net driver/load extraction
# ----------------------------

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
            # Treat INOUT conservatively as load for now.
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


# ----------------------------
# HPWL / bbox helpers
# ----------------------------

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
            if {$ixmin < $xmin} { set xmin $ixmin }
            if {$iymin < $ymin} { set ymin $iymin }
            if {$ixmax > $xmax} { set xmax $ixmax }
            if {$iymax > $ymax} { set ymax $iymax }
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


# ----------------------------
# Approximate local cell density
# ----------------------------

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


# ----------------------------
# Pin capacitance
# ----------------------------

proc get_net_pin_cap_ff {net_name} {
    # OpenSTA/OpenROAD method.
    #
    # Example manually checked by user:
    #   set sta_net [get_nets _1925_]
    #   $sta_net pin_capacitance view max
    #
    # The returned value is in farads, for example:
    #   1.5449999542531054e-15
    #
    # We convert it to femtofarads:
    #   1 F = 1e15 fF

    if {[catch {set sta_nets [get_nets -quiet $net_name]} err]} {
        return 0.0
    }

    if {[llength $sta_nets] == 0} {
        return 0.0
    }

    set sta_net [lindex $sta_nets 0]

    # Main variant based on the working command:
    #   $sta_net pin_capacitance view max
    if {![catch {set cap_f [$sta_net pin_capacitance view max]} err]} {
        if {$cap_f ne ""} {
            return [expr {double($cap_f) * 1.0e15}]
        }
    }

    # Fallback: if "view" is stored as a global Tcl variable in some setups.
    if {[info exists ::view]} {
        if {![catch {set cap_f [$sta_net pin_capacitance $::view max]} err]} {
            if {$cap_f ne ""} {
                return [expr {double($cap_f) * 1.0e15}]
            }
        }
    }

    # If both variants fail, return zero but do not break extraction.
    return 0.0
}

# ----------------------------
# Main extraction procedure
# ----------------------------

proc extract_net_features_prects {out_csv} {
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

        # fanin: number of unique signal input nets of the driver cell
        set input_nets [get_driver_input_nets $driver_inst $net]
        set fanin [llength $input_nets]

        # fanout: number of load pins on this net
        set fanout $num_loads

        # fanin_area: area of cells driving the input nets of the driver cell
        set upstream_driver_insts [get_upstream_driver_insts $input_nets]
        set fanin_area [sum_unique_inst_area_um2 $upstream_driver_insts $dbu]

        # fanout_area: area of unique load cells
        set load_insts [unique_insts_from_iterms $load_iterms]
        set fanout_area [sum_unique_inst_area_um2 $load_insts $dbu]

        # HPWL bbox: bbox covering driver cell and all load cells
        set hpwl_insts [concat [list $driver_inst] $load_insts]
        set bbox [bbox_from_insts $hpwl_insts]

        if {$bbox eq ""} {
            incr skipped_nets
            continue
        }

        set hpwl [hpwl_from_bbox_um $bbox $dbu]
        set gcell_area [area_from_bbox_um2 $bbox $dbu]
        set gcell_ar [ar_from_bbox $bbox]

        # approximate cell density inside HPWL bbox
        set cell_density [cell_density_in_bbox $bbox $dbu]

        # Total load pin capacitance of the current net from OpenSTA/OpenROAD.
        # Returned by OpenROAD in farads, converted to femtofarads inside the function.
        set pin_cap [get_net_pin_cap_ff $net_name]

        puts $fh "[csv_escape $design_name],[csv_escape $pdk_name],[csv_escape $config_id],[csv_escape $net_name],$fanin,$fanout,$fanin_area,$fanout_area,$hpwl,$cell_density,$pin_cap,$gcell_area,$gcell_ar"

        incr written_nets
    }

    close $fh

    puts "Feature extraction complete."
    puts "Total nets   : $total_nets"
    puts "Written nets : $written_nets"
    puts "Skipped nets : $skipped_nets"
    puts "Output CSV   : $out_csv"
}