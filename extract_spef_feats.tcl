
# Auto-generated congestion map extraction script

source "/home/nvgel/phd/dataset/sky130/ac97_top/CLK_7.8_IO_0.00_CU_20_AR_0.5_HW_4_HS_4_HP_32_VW_4_VS_4_VP_32/config/config.tcl"

foreach lef $lef_list {
    read_lef $lef
}

read_def "/home/nvgel/phd/dataset/sky130/ac97_top/CLK_7.8_IO_0.00_CU_20_AR_0.5_HW_4_HS_4_HP_32_VW_4_VS_4_VP_32/postcts/def/def.def"

##CREATE TIMING CORNER
define_corners view

##READ LIBERTY FILE
foreach lib $liberty {
    read_liberty -corner view $lib
}

##UNITS
set_cmd_units -time $liberty_time_unit -capacitance $liberty_cap_unit -current $liberty_current_unit -voltage $liberty_voltage_unit -resistance $liberty_res_unit -distance um

read_sdc "/home/nvgel/phd/dataset/sky130/ac97_top/CLK_7.8_IO_0.00_CU_20_AR_0.5_HW_4_HS_4_HP_32_VW_4_VS_4_VP_32/postcts/sdc/sdc.sdc"

##CREATE PATH GROUP
group_path -name reg2reg -from [all_registers] -to [all_registers]
group_path -name in2reg -from [all_inputs] -to [all_registers]
group_path -name reg2out -from [all_registers] -to [all_outputs]
group_path -name in2out -from [all_inputs] -to [all_outputs]

source ./feat.tcl

extract_net_features_prects "net_features_prects.csv"

#'fanin' -- число цепей у элемента, который драйверит нашу цепь
#'fanout' -- число нагрузок на выходе нашей цепи 
#'fanin_area' -- площадь ячеек, на входе у у элемента, который драйверит нашу цепь
#'fanout_area -- площадь ячеек в нагрзуке цепи 
#'hpwl' -- полупериметр прямоугольника в который попадают наш drive и ее fanout ячейки
#'cell_density' -- плотность ячеек в hpwl прямоугольнике 
#'pin_cap' -- емкость пинов в нагрузке (из liberty можно взять)
#'gcell_area' -- площадь Hpwl прямоугольника
#'gcell_ar' -- соотношение сторон Hpwl прямоугольника