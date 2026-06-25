#!/usr/bin/env python3
"""
build_interval_dataset.py

Phase 4 Step 3 dataset builder.

Broad execution steps:
1. Choose either one experiment output directory or all of them.
2. Walk each `interval_<n>` directory under the chosen experiment outputs.
3. Aggregate energystats XML features from `sniper_files/`.
4. Aggregate BB-derived features from `BBV_info/bb_catalog.json`.
5. Extract thermal labels from `thermal_output/`.
6. Write one labeled CSV row per `(experiment_dir_name, interval_id)`.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

from phase4_common import (
    aggregate_energystats_interval,
    build_interval_bbv_features,
    compute_intent_execution_mismatch,
    default_outputs_root,
    discover_interval_dirs,
    ensure_directory,
    load_interval_thermal_labels,
    read_json,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--experiment-dir-name",
        help="Build a dataset for just one experiment directory under Mayfew/Outputs",
    )
    group.add_argument(
        "--all-experiments",
        action="store_true",
        help="Build a dataset from every experiment directory under Mayfew/Outputs",
    )
    parser.add_argument(
        "--outputs-root",
        default=str(default_outputs_root()),
        help="Root directory for Mayfew outputs",
    )
    parser.add_argument(
        "--csv-name",
        default="dataset",
        help="Dataset CSV basename. '.csv' is added automatically if omitted.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail immediately if any interval is missing required inputs",
    )
    return parser


def normalize_csv_name(name: str) -> str:
    return name if name.endswith(".csv") else f"{name}.csv"


def discover_experiment_output_dirs(outputs_root: Path) -> list[Path]:
    discovered: list[Path] = []
    for path in sorted(outputs_root.iterdir()):
        if not path.is_dir():
            continue
        if path.name == "dataset":
            continue
        discovered.append(path)
    return discovered


LABEL_COLUMNS = [
    "peak_temperature",
    "average_temperature",
    "max_positive_mltd",
    "worst_hotspot_severity",
]
IDENTIFIER_COLUMNS = [
    "experiment_dir_name",
    "interval_id",
]


def build_field_order(rows: list[dict[str, object]]) -> list[str]:
    """
    Build a stable CSV column order for the final dataset.

    Broad steps:
    1. Keep the identifier columns at the far left.
    2. Place `real_ipc` as the first true feature column immediately after the identifiers.
    3. Keep the remaining non-label feature columns in stable alphabetical order.
    4. Move the label columns to the far right in a fixed order.
    """
    seen_columns = {key for row in rows for key in row.keys()}
    left_columns = [column for column in IDENTIFIER_COLUMNS if column in seen_columns]

    middle_columns: list[str] = []
    if "real_ipc" in seen_columns and "real_ipc" not in left_columns:
        middle_columns.append("real_ipc")

    reserved = set(left_columns) | set(middle_columns) | set(LABEL_COLUMNS)
    middle_columns.extend(sorted(column for column in seen_columns if column not in reserved))

    right_columns = [column for column in LABEL_COLUMNS if column in seen_columns]
    return [*left_columns, *middle_columns, *right_columns]


def build_interval_row(experiment_output_dir: Path, interval_dir: Path) -> dict[str, object]:
    """
    Build one full feature row from one interval directory.

    Step 1:
      Load and aggregate all energystats XML files for the interval.
    Step 2:
      Load the interval-local BB catalog and derive the BB / dependency /
      entropy features.
    Step 3:
      Load the thermal files and derive the interval labels.
    Step 4:
      Merge the feature families into one flat CSV row.
    """
    interval_id = int(interval_dir.name.split("_", 1)[1])

    sniper_files_dir = interval_dir / "sniper_files"
    thermal_output_dir = interval_dir / "thermal_output"
    bbv_info_dir = interval_dir / "BBV_info"

    xml_paths = sorted(sniper_files_dir.glob("energystats-temp-*.xml"))
    if not xml_paths:
        raise FileNotFoundError(f"No energystats XML files found in {sniper_files_dir}")

    energystats_features = aggregate_energystats_interval(xml_paths)

    bb_catalog_path = bbv_info_dir / "bb_catalog.json"
    if not bb_catalog_path.exists():
        raise FileNotFoundError(f"Missing interval BB catalog: {bb_catalog_path}")
    interval_bb_catalog = read_json(bb_catalog_path)
    bbv_features = build_interval_bbv_features(interval_bb_catalog)

    die_grid_path = thermal_output_dir / "die_grid.temps"
    maxima_csv_path = thermal_output_dir / "die_grid.temps.2dmaxima.csv"
    if not die_grid_path.exists():
        raise FileNotFoundError(f"Missing die_grid.temps: {die_grid_path}")
    if not maxima_csv_path.exists():
        raise FileNotFoundError(f"Missing die_grid.temps.2dmaxima.csv: {maxima_csv_path}")
    thermal_labels = load_interval_thermal_labels(die_grid_path, maxima_csv_path)

    mismatch_features = compute_intent_execution_mismatch(bbv_features, energystats_features)

    row: dict[str, object] = {
        "experiment_dir_name": experiment_output_dir.name,
        "interval_id": interval_id,
    }
    row.update(energystats_features)
    row.update(bbv_features)
    row.update(mismatch_features)
    row.update(thermal_labels)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    outputs_root = Path(args.outputs_root).expanduser().resolve()
    dataset_dir = outputs_root / "dataset"
    ensure_directory(dataset_dir)

    if args.experiment_dir_name:
        experiment_output_dirs = [outputs_root / args.experiment_dir_name]
    else:
        experiment_output_dirs = discover_experiment_output_dirs(outputs_root)

    rows: list[dict[str, object]] = []
    warnings: list[str] = []

    for experiment_output_dir in experiment_output_dirs:
        if not experiment_output_dir.exists():
            message = f"Experiment output directory does not exist: {experiment_output_dir}"
            if args.strict:
                raise FileNotFoundError(message)
            warnings.append(message)
            continue

        for interval_dir in discover_interval_dirs(experiment_output_dir):
            try:
                row = build_interval_row(experiment_output_dir, interval_dir)
            except Exception as exc:
                message = f"Skipping {interval_dir}: {exc}"
                if args.strict:
                    raise
                warnings.append(message)
                continue
            rows.append(row)

    if not rows:
        raise RuntimeError("No dataset rows were produced")

    # Step 5: write a stable CSV with explicit feature/label ordering.
    fieldnames = build_field_order(rows)
    dataset_csv_path = dataset_dir / normalize_csv_name(args.csv_name)
    with dataset_csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(
            rows,
            key=lambda record: (str(record["experiment_dir_name"]), int(record["interval_id"])),
        ):
            normalized_row = {
                key: ("" if isinstance(value, float) and math.isnan(value) else value)
                for key, value in row.items()
            }
            writer.writerow(normalized_row)

    print(f"[phase4] Wrote dataset CSV: {dataset_csv_path}")
    if warnings:
        print("[phase4] Warnings:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
