# Phase 4 Overview

Phase 4 has three scripts:

1. `run_phase4_bbv_pipeline.py`
2. `collect_hotgauge_interval_outputs.py`
3. `build_interval_dataset.py`

Together they do this:

1. Recreate the legacy BBV / SimPoint / BB-sequence workflow and store the durable outputs in `Mayfew/Outputs/<experiment-dir-name>`.
2. Pull interval-local sniper and thermal outputs out of HotGauge and place them under per-interval directories in Mayfew.
3. Build one CSV dataset containing the interval feature vectors and labels.

## Directory Shape

After Phase 4 Step 2, one experiment output directory will look like this:

```text
Mayfew/Outputs/<experiment-dir-name>/
  results.simpts
  results.weights
  results.labels
  results.cluster_members
  bb_seq.0.csv
  objdump_index.json
  filtered_bb_seq.csv
  filtered_trace_summary.json
  bb_catalog.json
  <prefix>_hotblocks.txt
  <prefix>.0.bb
  <prefix>_simpoint.0.bb
  <executable>.objdump.txt
  intervals.csv
  interval_5/
    sniper_files/
    thermal_output/
    BBV_info/
  interval_6/
    ...
```

## Script 1

### `run_phase4_bbv_pipeline.py`

What it does:

1. Runs `run_profile.sh --option 0`
2. Runs `run_profile.sh --option 2`
3. Runs `strip_bbv.sh`
4. Runs `new_simpoint.py` with fixed `k`
5. Parses the representative interval ids from `results.simpts`
6. Runs `objdump -d`
7. Runs the `libbb_sequence_intervals.so` plugin for those representative intervals
8. Runs:
   - `parse_objdump.py`
   - `filter_trace_csv.py`
   - `extract_bbs.py`

Run from:

```bash
cd /data/jake_m/Mayfew/scripts/phase_4
```

Example:

```bash
python3 run_phase4_bbv_pipeline.py \
  --executable /data/jake_m/HotGauge/examples/mod_linpack \
  --interval-size 10000000 \
  --k-clusters 15 \
  --bb-file-prefix n_linpack \
  --experiment-dir-name linpack_phase4
```

Inputs:

- `--executable`: target binary
- `--interval-size`: instruction interval size
- `--k-clusters`: fixed SimPoint `k`
- `--bb-file-prefix`: stable prefix for BB files
- `--experiment-dir-name`: Mayfew output directory name
- optional profiler-path overrides
- optional executable arguments after `--`

Outputs in `Mayfew/Outputs/<experiment-dir-name>`:

- `results.simpts`
- `results.weights`
- `results.labels`
- `results.cluster_members`
- `bb_seq.0.csv`
- `objdump_index.json`
- `filtered_bb_seq.csv`
- `filtered_trace_summary.json`
- `bb_catalog.json`
- hotblocks output
- extended BBV
- stripped BBV
- objdump text

Profiler-side commands it runs:

- `run_profile.sh --option 0`
  - input: executable, interval size, BB prefix
  - output: hotblocks file

- `run_profile.sh --option 2`
  - input: executable, interval size, BB prefix
  - output: extended BBV

- `strip_bbv.sh`
  - input: extended BBV
  - output: stripped BBV

- `new_simpoint.py`
  - input: stripped BBV, fixed `k`
  - output: `results.simpts`, `results.weights`, `results.labels`, `results.cluster_members`

- `qemu-x86_64` with `libbb_sequence_intervals.so`
  - input: executable, interval size, SimPoint representative interval ids
  - output: `bb_seq.0.csv`

- `objdump -d`
  - input: executable
  - output: objdump text file

- `parse_objdump.py`
  - input: objdump text file
  - output: `objdump_index.json`

- `filter_trace_csv.py`
  - input: `bb_seq.0.csv`, objdump text file
  - output: `filtered_bb_seq.csv`, `filtered_trace_summary.json`

- `extract_bbs.py`
  - input: `filtered_bb_seq.csv`, `objdump_index.json`
  - output: `bb_catalog.json`

## Script 2

### `collect_hotgauge_interval_outputs.py`

What it does:

1. Scans HotGauge `Metadata/` to discover interval ids
2. Creates one Mayfew interval directory per interval
3. Copies matching energystats XML files into `sniper_files/`
4. Copies selected thermal outputs into `thermal_output/`
5. Builds interval-local `BBV_info/bb_catalog.json`
6. Builds interval-local `BBV_info/BB_order.txt`

Run from:

```bash
cd /data/jake_m/Mayfew/scripts/phase_4
```

Example:

```bash
python3 collect_hotgauge_interval_outputs.py \
  --intervals-csv /data/jake_m/Mayfew/Outputs/libq/intervals.csv \
  --sniper-output-dir /data/jake_m/HotGauge/snipersim/output/libq_intervals/7nm/4.0GHz \
  --hotgauge-experiment-dir /data/jake_m/HotGauge/examples/libq_intervals \
  --mayfew-experiment-output-dir /data/jake_m/Mayfew/Outputs/libq
```

Inputs:

- `--intervals-csv`: output from `group_energystats_by_interval.py`
- `--sniper-output-dir`: sniper energystats directory
- `--hotgauge-experiment-dir`: HotGauge experiment directory
- `--mayfew-experiment-output-dir`: one Mayfew experiment output directory

Outputs:

- `Mayfew/Outputs/<experiment-dir-name>/interval_<n>/sniper_files/*`
- `Mayfew/Outputs/<experiment-dir-name>/interval_<n>/thermal_output/die_grid.temps`
- `Mayfew/Outputs/<experiment-dir-name>/interval_<n>/thermal_output/die_grid.temps.2dmaxima.csv`
- `Mayfew/Outputs/<experiment-dir-name>/interval_<n>/thermal_output/viz/temps.mp4`
- `Mayfew/Outputs/<experiment-dir-name>/interval_<n>/BBV_info/bb_catalog.json`
- `Mayfew/Outputs/<experiment-dir-name>/interval_<n>/BBV_info/BB_order.txt`

How `BBV_info` is created:

- The script reads the experiment-level:
  - `bb_catalog.json`
  - `filtered_bb_seq.csv`
- It filters the dynamic trace rows by `interval_id`
- It keeps only the BBs that execute in that interval
- It adds `interval_execution_count` to each retained BB
- It writes the ordered `bb_index` sequence, one per line, to `BB_order.txt`

## Script 3

### `build_interval_dataset.py`

What it does:

1. Reads one experiment output directory or all of them
2. Walks each `interval_<n>` directory
3. Aggregates energystats features from `sniper_files/`
4. Aggregates BB-derived features from `BBV_info/bb_catalog.json`
5. Extracts labels from `thermal_output/`
6. Writes one CSV row per interval

Run from:

```bash
cd /data/jake_m/Mayfew/scripts/phase_4
```

One experiment:

```bash
python3 build_interval_dataset.py \
  --experiment-dir-name libq \
  --csv-name libq_dataset
```

All experiments:

```bash
python3 build_interval_dataset.py \
  --all-experiments \
  --csv-name dataset
```

Inputs:

- either `--experiment-dir-name <name>` or `--all-experiments`
- optional `--csv-name`
- optional `--strict`

Outputs:

- `Mayfew/Outputs/dataset/<csv-name>.csv`

Per-interval inputs it reads:

- `interval_<n>/sniper_files/energystats-temp-*.xml`
- `interval_<n>/thermal_output/die_grid.temps`
- `interval_<n>/thermal_output/die_grid.temps.2dmaxima.csv`
- `interval_<n>/BBV_info/bb_catalog.json`

Per-interval outputs it contributes:

- one labeled dataset row with:
  - experiment name
  - interval id
  - energystats-derived features
  - BB-derived features
  - intent-vs-execution mismatch features
  - thermal labels

## Suggested Order

1. Run `run_phase4_bbv_pipeline.py`
2. Run `collect_hotgauge_interval_outputs.py`
3. Run `build_interval_dataset.py`
