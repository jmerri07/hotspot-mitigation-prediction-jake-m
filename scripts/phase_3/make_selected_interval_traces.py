#!/usr/bin/env python3
"""
make_selected_interval_traces.py

Create interval-specific metadata and trace files from:
1) interval CSV produced by group_energystats_by_interval.py
2) one original metadata JSON
3) one original trace JSON
4) a semicolon-separated list of interval IDs

Typical Mayfew usage:
    Run from either the executable output directory or from one interval-specific
    run directory and pass explicit --metadata-dir / --traces-dir paths.

Default subdirectories:
    Metadata/
    Traces/

Behavior:
- Parse --intervals like "5;10;13;22"
- For each requested interval:
    * verify it exists in the interval CSV
    * collect all timestamp_order values from the CSV for that interval
- Create:
    Metadata/<base>_interval_<N>.json
    Traces/<base>_interval_<N>.json
- Metadata edit:
    workload = "<original workload>_interval_<N>"
- Trace edit:
    for each field/list, keep only values whose indices are in timestamp_order,
    then repeat those kept values until the list is back to its original length

Example usage:
    python3 make_selected_interval_traces.py \
        --interval-csv himeno_intervals.csv \
        --intervals "5;10;13;22" \
        --og-metadata-file hot_himenobmtxpa_metadata.json \
        --og-trace-file hot_himenobmtxpa_pow_trace.json
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, List


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create interval-specific metadata and trace files."
    )
    parser.add_argument(
        "--interval-csv",
        required=True,
        help="CSV produced by group_energystats_by_interval.py",
    )
    parser.add_argument(
        "--intervals",
        required=True,
        help='Semicolon-separated interval list, e.g. "5;10;13;22"',
    )
    parser.add_argument(
        "--og-metadata-file",
        required=True,
        help="Original metadata JSON filename or path",
    )
    parser.add_argument(
        "--og-trace-file",
        required=True,
        help="Original trace JSON filename or path",
    )
    parser.add_argument(
        "--metadata-dir",
        default="Metadata",
        help="Directory containing/writing metadata files (default: Metadata)",
    )
    parser.add_argument(
        "--traces-dir",
        default="Traces",
        help="Directory containing/writing trace files (default: Traces)",
    )
    return parser.parse_args()


def resolve_input_file(path_or_name: str, base_dir: Path) -> Path:
    candidate = Path(path_or_name)

    if candidate.is_absolute():
        return candidate.resolve()

    direct = candidate.resolve()
    if direct.exists():
        return direct

    return (base_dir / candidate).resolve()


def parse_intervals(interval_text: str) -> List[int]:
    parts = [part.strip() for part in interval_text.split(";")]
    parts = [part for part in parts if part]

    if not parts:
        raise ValueError("No intervals were provided")

    intervals: List[int] = []
    for part in parts:
        try:
            interval_id = int(part)
        except ValueError as exc:
            raise ValueError(f"Invalid interval '{part}': must be an integer") from exc

        if interval_id < 0:
            raise ValueError(f"Invalid interval '{part}': must be >= 0")

        intervals.append(interval_id)

    # Remove duplicates while preserving order
    deduped: List[int] = []
    seen = set()
    for interval_id in intervals:
        if interval_id not in seen:
            deduped.append(interval_id)
            seen.add(interval_id)

    return deduped


def load_interval_csv(csv_path: Path) -> Dict[int, List[int]]:
    """
    Return:
        interval_to_timestamp_orders: {interval_index: [timestamp_order, ...]}
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Interval CSV not found: {csv_path}")

    interval_to_orders: Dict[int, List[int]] = {}

    with csv_path.open("r", newline="") as handle:
        reader = csv.DictReader(handle)

        required_columns = {"interval_index", "timestamp_order"}
        missing = required_columns - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Interval CSV missing required columns: {sorted(missing)}"
            )

        for row in reader:
            interval_index = int(row["interval_index"])
            timestamp_order = int(row["timestamp_order"])

            if interval_index not in interval_to_orders:
                interval_to_orders[interval_index] = []

            interval_to_orders[interval_index].append(timestamp_order)

    for interval_index in interval_to_orders:
        interval_to_orders[interval_index].sort()

    return interval_to_orders


def load_json_file(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with path.open("r") as handle:
        return json.load(handle)


def repeat_to_length(values: List[float], target_length: int) -> List[float]:
    if target_length < 0:
        raise ValueError("target_length must be >= 0")
    if target_length == 0:
        return []
    if not values:
        raise ValueError("Cannot repeat an empty list")

    repeated: List[float] = []
    i = 0
    while len(repeated) < target_length:
        repeated.append(values[i % len(values)])
        i += 1
    return repeated


def rewrite_trace(trace_data: Dict[str, List[float]], selected_orders: List[int]) -> Dict[str, List[float]]:
    if not isinstance(trace_data, dict):
        raise ValueError("Trace JSON must be a JSON object/dictionary")

    rewritten: Dict[str, List[float]] = {}

    for field_name, values in trace_data.items():
        if not isinstance(values, list):
            raise ValueError(
                f"Trace field '{field_name}' is not a list; all fields must be lists"
            )

        original_length = len(values)
        if original_length == 0:
            rewritten[field_name] = []
            continue

        kept_values: List[float] = []
        for order in selected_orders:
            if order < 0 or order >= original_length:
                raise ValueError(
                    f"timestamp_order {order} is out of range for field '{field_name}' "
                    f"(length {original_length})"
                )
            kept_values.append(values[order])

        if not kept_values:
            raise ValueError(
                f"No kept values for field '{field_name}'. "
                f"This usually means the chosen interval had no timestamp_order rows."
            )

        rewritten[field_name] = repeat_to_length(kept_values, original_length)

    return rewritten


def rewrite_metadata(metadata_data: Dict, interval_id: int) -> Dict:
    if not isinstance(metadata_data, dict):
        raise ValueError("Metadata JSON must be a JSON object/dictionary")

    if "workload" not in metadata_data:
        raise ValueError("Metadata JSON missing required key: 'workload'")

    new_metadata = dict(metadata_data)
    original_workload = str(metadata_data["workload"])
    new_metadata["workload"] = f"{original_workload}_interval_{interval_id}"
    return new_metadata


def make_output_name(original_name: str, interval_id: int) -> str:
    path = Path(original_name)
    stem = path.stem
    suffix = path.suffix if path.suffix else ".json"
    return f"{stem}_interval_{interval_id}{suffix}"


def main() -> int:
    args = parse_args()

    cwd = Path.cwd()

    metadata_dir = resolve_input_file(args.metadata_dir, cwd)
    traces_dir = resolve_input_file(args.traces_dir, cwd)

    interval_csv_path = resolve_input_file(args.interval_csv, cwd)
    metadata_path = resolve_input_file(args.og_metadata_file, metadata_dir)
    trace_path = resolve_input_file(args.og_trace_file, traces_dir)

    if not metadata_dir.exists():
        print(f"ERROR: Metadata directory does not exist: {metadata_dir}", file=sys.stderr)
        return 1

    if not traces_dir.exists():
        print(f"ERROR: Traces directory does not exist: {traces_dir}", file=sys.stderr)
        return 1

    try:
        requested_intervals = parse_intervals(args.intervals)
        interval_to_orders = load_interval_csv(interval_csv_path)
        metadata_template = load_json_file(metadata_path)
        trace_template = load_json_file(trace_path)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    missing_intervals = [
        interval_id for interval_id in requested_intervals
        if interval_id not in interval_to_orders
    ]
    if missing_intervals:
        print(
            "ERROR: The following requested intervals were not found in the CSV: "
            + ", ".join(str(x) for x in missing_intervals),
            file=sys.stderr,
        )
        return 1

    print(f"Loaded interval CSV       : {interval_csv_path}")
    print(f"Loaded metadata template : {metadata_path}")
    print(f"Loaded trace template    : {trace_path}")
    print(f"Requested intervals      : {requested_intervals}")
    print()

    generated_count = 0

    for interval_id in requested_intervals:
        selected_orders = interval_to_orders[interval_id]

        try:
            new_metadata = rewrite_metadata(metadata_template, interval_id)
            new_trace = rewrite_trace(trace_template, selected_orders)
        except Exception as exc:
            print(
                f"ERROR while generating files for interval {interval_id}: {exc}",
                file=sys.stderr,
            )
            return 1

        metadata_output_name = make_output_name(metadata_path.name, interval_id)
        trace_output_name = make_output_name(trace_path.name, interval_id)

        metadata_output_path = metadata_dir / metadata_output_name
        trace_output_path = traces_dir / trace_output_name

        try:
            with metadata_output_path.open("w") as handle:
                json.dump(new_metadata, handle, indent=2)
                handle.write("\n")

            with trace_output_path.open("w") as handle:
                json.dump(new_trace, handle, indent=2)
                handle.write("\n")
        except Exception as exc:
            print(
                f"ERROR while writing files for interval {interval_id}: {exc}",
                file=sys.stderr,
            )
            return 1

        print(f"Interval {interval_id}")
        print(f"  timestamp_orders: {selected_orders}")
        print(f"  wrote metadata : {metadata_output_path}")
        print(f"  wrote trace    : {trace_output_path}")
        print()

        generated_count += 1

    print(f"Done. Generated {generated_count} interval-specific metadata/trace pairs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
