# OpenROAD Flow Extractors

This repository contains task-specific data extraction scripts for raw RTL-to-GDS datasets generated with the OpenROAD-based physical design flow. The scripts read already generated raw design runs and create derived datasets for machine learning tasks in VLSI physical design.

Currently, two extractors are provided:

* `extract_spef.py` — extracts post-route net capacitance labels from SPEF files.
* `extract_congestion.py` — extracts OpenROAD GUI heatmaps for congestion and density prediction tasks.

The raw dataset is expected to contain one directory per design run, including `config/config.tcl`, final routed DEF files, SPEF files, and other flow outputs.

## Expected Raw Dataset Structure

The scripts assume the following directory layout:

```text
raw_dataset/
  <pdk_name>/
    <design_name>/
      <run_config_name>/
        config/
          config.tcl
        route/
          def/
            def.def
          *.spef
        run_info.csv
```

Example:

```text
dataset_v2/
  freepdk45/
    ac97_top/
      CLK_0.9_IO_0.00_CU_20_AR_1.0_HW_4_HS_4_HP_64_VW_4_VS_4_VP_64/
        config/
          config.tcl
        route/
          def/
            def.def
          ac97_top.spef
        run_info.csv
```

The output directory mirrors the raw dataset structure.

## Installation

Python 3.10+ is recommended.

Install Python dependencies:

```bash
pip install pandas pyarrow tqdm
```

For congestion extraction, OpenROAD must be available in `PATH`:

```bash
openroad -version
```

If the scripts are executed on a headless server, install `xvfb`:

```bash
sudo apt install xvfb
```

## Net Capacitance Extraction

The `extract_spef.py` script extracts total net capacitance from post-route SPEF files. For each run, it creates one Parquet file containing net-level labels.

The extracted columns are:

```text
design
pdk_name
config_id
net_name
cap_total_ff
```

where `cap_total_ff` is the total net capacitance normalized to femtofarads.

The extractor reads the SPEF header, parses `*C_UNIT`, resolves SPEF `*NAME_MAP` aliases, and extracts capacitance values from `*D_NET` records.

### Usage

```bash
python extract_spef.py \
  --dataset_path /path/to/raw_dataset \
  --out_dir /path/to/output/net_capacitance
```

Example:

```bash
python extract_spef.py \
  --dataset_path /home/nvgel/phd/dataset_v2 \
  --out_dir /home/nvgel/phd/OpenROAD-flow-Extract/net_capacitance
```

### Output Structure

```text
net_capacitance/
  freepdk45/
    ac97_top/
      CLK_0.9_IO_0.00_CU_20_AR_1.0_HW_4_HS_4_HP_64_VW_4_VS_4_VP_64/
        net_capacitance.parquet
  extraction_report.csv
  summary.json
```

Each `net_capacitance.parquet` file corresponds to one physical design run.

### Reading One Run

```python
import pandas as pd

df = pd.read_parquet(
    "/path/to/net_capacitance/freepdk45/ac97_top/<run_config>/net_capacitance.parquet"
)

print(df.head())
```

### Reading Multiple Runs with DuckDB

For large datasets, avoid loading all Parquet files into pandas at once. Use DuckDB instead:

```python
import duckdb

con = duckdb.connect()

df = con.execute("""
SELECT design, pdk_name, config_id, net_name, cap_total_ff
FROM '/path/to/net_capacitance/**/*.parquet'
WHERE pdk_name = 'freepdk45'
  AND design = 'ac97_top'
LIMIT 100000
""").df()
```

## Congestion and Density Map Extraction

The `extract_congestion.py` script restores each routed design in OpenROAD and dumps GUI heatmaps for congestion and density-related tasks.

For each run, the script:

1. sources `config/config.tcl`;
2. reads all LEF files from `$lef_list`;
3. reads the final routed DEF;
4. runs `global_route`;
5. dumps OpenROAD GUI heatmaps.

The following maps are generated:

```text
routing_congestion.txt
rudy_congestion.txt
pin_density.txt
placement_density.txt
```

### Usage

On a machine with GUI support:

```bash
python extract_congestion.py \
  --dataset_path /path/to/raw_dataset \
  --out_dir /path/to/output/congestion_maps
```

On a headless server:

```bash
python extract_congestion.py \
  --dataset_path /path/to/raw_dataset \
  --out_dir /path/to/output/congestion_maps \
  --xvfb
```

Example:

```bash
python extract_congestion.py \
  --dataset_path /home/nvgel/phd/dataset_v2 \
  --out_dir /home/nvgel/phd/OpenROAD-flow-Extract/congestion_maps \
  --xvfb
```

### Parallel Execution

The script supports parallel OpenROAD execution:

```bash
python extract_congestion.py \
  --dataset_path /home/nvgel/phd/dataset_v2 \
  --out_dir /home/nvgel/phd/OpenROAD-flow-Extract/congestion_maps \
  --xvfb \
  --jobs 4
```

Use this option carefully. Each OpenROAD GUI instance may consume significant RAM, especially for large designs. It is recommended to start with:

```bash
--jobs 2
```

and increase the number of jobs only after checking memory usage.

### Output Structure

```text
congestion_maps/
  freepdk45/
    ac97_top/
      CLK_0.9_IO_0.00_CU_20_AR_1.0_HW_4_HS_4_HP_64_VW_4_VS_4_VP_64/
        routing_congestion.txt
        rudy_congestion.txt
        pin_density.txt
        placement_density.txt
        dump_heatmaps.tcl
        openroad_stdout.log
        openroad_stderr.log
  extraction_report.csv
```

The `dump_heatmaps.tcl` file is generated automatically for each run and can be used for debugging.

## Recommended Workflow

1. Generate the raw RTL-to-GDS dataset using the OpenROAD flow.
2. Run SPEF extraction to create net capacitance labels.
3. Run congestion extraction to create routing and density maps.
4. Use the derived datasets to build task-specific ML features and benchmarks.

Example:

```bash
python extract_spef.py \
  --dataset_path /home/nvgel/phd/dataset_v2 \
  --out_dir /home/nvgel/phd/OpenROAD-flow-Extract/net_capacitance

python extract_congestion.py \
  --dataset_path /home/nvgel/phd/dataset_v2 \
  --out_dir /home/nvgel/phd/OpenROAD-flow-Extract/congestion_maps \
  --xvfb \
  --jobs 2
```

## Reports

Both scripts generate an `extraction_report.csv` file in the output directory. The report contains per-run extraction status and error messages, if any.

Typical statuses:

```text
ok
failed
skipped
```

A run may fail if the required input files are missing, if OpenROAD cannot restore the design, or if heatmap dumping is not supported for a particular run.

## Notes

* The raw dataset is not modified by these scripts.
* The output directory mirrors the raw dataset structure.
* Net capacitance is extracted from post-route SPEF files.
* Congestion maps are generated by restoring routed DEF files in OpenROAD and dumping GUI heatmaps.
* For large datasets, prefer per-run Parquet files and query them using DuckDB or PyArrow Dataset instead of loading everything into pandas at once.

## Citation

## License
