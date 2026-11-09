# OpenROAD Flow Extractors

How py-script work:

```
extract_wirelength_postcts_route.py [-h] 
--dataset_path DATASET_PATH 
--out_dir OUT_DIR [--pdk {asap7,freepdk45,sky130,gf180}] 
[--openroad OPENROAD] 
[--parquet_name PARQUET_NAME]
[--jobs JOBS]
[--max_runs MAX_RUNS]
[--timeout TIMEOUT]
[--overwrite]
[--labels_only]
[--csv_only]
[--keep_unmatched]
[--min_match_fraction MIN_MATCH_FRACTION]
[--postcts_def POSTCTS_DEF]
[--postcts_sdc POSTCTS_SDC]
[--route_def ROUTE_DEF]
[--route_sdc ROUTE_SDC]
```

How to select dataset (example):

```python
python3 extract_wirelength_postcts_route.py --dataset_path /home/nvgel/phd/dataset --pdk sky130 --out_dir /home/nvgel/phd/dataset_wirelength_v2/ --overwrite --jobs 6
```