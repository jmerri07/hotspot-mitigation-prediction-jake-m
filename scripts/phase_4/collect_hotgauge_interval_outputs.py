#!/usr/bin/env python3
"""
collect_hotgauge_interval_outputs.py

Phase 4 Step 2 transfer script.

This script pulls interval-local artifacts back out of HotGauge and stores them
under one Mayfew experiment output directory.

Broad execution steps:
1. Discover which intervals exist by scanning HotGauge metadata files.
2. Create one Mayfew interval directory for each discovered interval.
3. Copy the energystats XML files that belong to each interval.
4. Copy the requested HotGauge thermal outputs into each interval directory.
5. Build the interval-local `BBV_info` view from the Phase 4 Step 1 outputs.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from phase4_common import (
    ensure_directory,
    parse_metadata_interval_filename,
    read_csv_rows,
    read_json,
    write_json,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intervals-csv", required=True, help="Path to intervals.csv")
    parser.add_argument(
        "--sniper-output-dir",
        required=True,
        help="Sniper output directory that contains the energystats XML files",
    )
    parser.add_argument(
        "--hotgauge-experiment-dir",
        required=True,
        help="Path to the HotGauge experiment directory",
    )
    parser.add_argument(
        "--mayfew-experiment-output-dir",
        required=True,
        help="Path to the Mayfew experiment output directory",
    )
    return parser


def require_path(path: Path, description: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Missing {description}: {path}")
    return path


def copy_file(src: Path, dest: Path) -> None:
    ensure_directory(dest.parent)
    shutil.copy2(src, dest)


def discover_interval_metadata(metadata_dir: Path) -> dict[int, Path]:
    discovered: dict[int, Path] = {}
    for path in sorted(metadata_dir.glob("*_metadata_interval_*.json")):
        interval_id = parse_metadata_interval_filename(path)
        if interval_id is None:
            continue
        discovered[interval_id] = path
    return discovered


def build_interval_file_map(intervals_csv_path: Path) -> dict[int, list[Path]]:
    """
    Read `intervals.csv` and group energystats file paths by interval index.
    """
    interval_to_files: dict[int, list[Path]] = defaultdict(list)
    for row in read_csv_rows(intervals_csv_path):
        interval_id = int(row["interval_index"])
        interval_to_files[interval_id].append(Path(row["file_path"]).expanduser().resolve())
    return interval_to_files


def build_interval_trace_rows(filtered_trace_path: Path) -> dict[int, list[dict[str, str]]]:
    interval_rows: dict[int, list[dict[str, str]]] = defaultdict(list)
    for row in read_csv_rows(filtered_trace_path):
        interval_rows[int(row["interval_id"])].append(row)

    for interval_id in interval_rows:
        interval_rows[interval_id].sort(key=lambda row: int(row["seq_no"]))
    return interval_rows


def build_interval_bb_catalog(
    interval_id: int,
    rows: list[dict[str, str]],
    root_bb_catalog: dict[str, object],
) -> tuple[dict[str, object], list[int]]:
    """
    Construct the interval-local BB catalog and ordered BB sequence.

    Step 1:
      Count how many times each BB executes in this interval.
    Step 2:
      Filter the global BB catalog down to those BBs only.
    Step 3:
      Annotate each retained BB with its interval-local execution count.
    Step 4:
      Emit the dynamic BB order as one ordered `bb_index` list.
    """
    execution_counts: Counter[str] = Counter(row["bb_index"] for row in rows)
    global_catalog = root_bb_catalog["bb_catalog"]
    ordered_bb_indices = [int(row["bb_index"]) for row in rows]

    interval_catalog: dict[str, object] = {
        "interval_id": interval_id,
        "bb_catalog": {},
    }

    for bb_index, execution_count in sorted(
        execution_counts.items(),
        key=lambda item: int(item[0]),
    ):
        if bb_index not in global_catalog:
            continue
        record = dict(global_catalog[bb_index])
        instruction_count = int(record.get("n_instructions_extracted", 0))
        record["interval_execution_count"] = execution_count
        record["interval_dynamic_instruction_count"] = execution_count * instruction_count
        interval_catalog["bb_catalog"][bb_index] = record

    return interval_catalog, ordered_bb_indices


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    intervals_csv_path = Path(args.intervals_csv).expanduser().resolve()
    sniper_output_dir = Path(args.sniper_output_dir).expanduser().resolve()
    hotgauge_experiment_dir = Path(args.hotgauge_experiment_dir).expanduser().resolve()
    mayfew_experiment_output_dir = Path(args.mayfew_experiment_output_dir).expanduser().resolve()

    require_path(intervals_csv_path, "intervals.csv")
    require_path(sniper_output_dir, "sniper output directory")
    require_path(hotgauge_experiment_dir, "HotGauge experiment directory")
    ensure_directory(mayfew_experiment_output_dir)

    metadata_dir = require_path(hotgauge_experiment_dir / "Metadata", "HotGauge Metadata directory")
    root_bb_catalog_path = require_path(mayfew_experiment_output_dir / "bb_catalog.json", "Mayfew bb_catalog.json")
    filtered_trace_path = require_path(
        mayfew_experiment_output_dir / "filtered_bb_seq.csv",
        "Mayfew filtered_bb_seq.csv",
    )

    interval_metadata_files = discover_interval_metadata(metadata_dir)
    interval_to_energystats = build_interval_file_map(intervals_csv_path)
    interval_to_trace_rows = build_interval_trace_rows(filtered_trace_path)
    root_bb_catalog = read_json(root_bb_catalog_path)

    for interval_id, metadata_path in sorted(interval_metadata_files.items()):
        interval_dir = mayfew_experiment_output_dir / f"interval_{interval_id}"
        sniper_files_dir = interval_dir / "sniper_files"
        thermal_output_dir = interval_dir / "thermal_output"
        bbv_info_dir = interval_dir / "BBV_info"
        ensure_directory(sniper_files_dir)
        ensure_directory(thermal_output_dir / "viz")
        ensure_directory(bbv_info_dir)

        # Step 1: copy the interval-local energystats XML files.
        for energystats_path in interval_to_energystats.get(interval_id, []):
            copy_file(energystats_path, sniper_files_dir / energystats_path.name)

        # Step 2: use the interval metadata JSON to reconstruct the HotGauge
        # thermal-output location exactly as HotGauge named it.
        metadata = read_json(metadata_path)
        workload = str(metadata["workload"])
        interval_ns = str(metadata["interval_ns"])
        tech_node = str(metadata["tech_node"])
        frequency = str(metadata["frequency"])

        sim_output_dir = (
            hotgauge_experiment_dir
            / "outputs"
            / "sims"
            / interval_ns
            / workload
            / tech_node
            / frequency
            / "core_0"
            / "idle_00"
        )

        thermal_files = [
            ("die_grid.temps", thermal_output_dir / "die_grid.temps"),
            ("die_grid.temps.2dmaxima.csv", thermal_output_dir / "die_grid.temps.2dmaxima.csv"),
            ("viz/temps.mp4", thermal_output_dir / "viz" / "temps.mp4"),
        ]
        for relative_src, dest in thermal_files:
            src = sim_output_dir / relative_src
            if src.exists():
                copy_file(src, dest)

        # Step 3: build the interval-local BBV view.
        rows = interval_to_trace_rows.get(interval_id, [])
        interval_bb_catalog, ordered_bb_indices = build_interval_bb_catalog(
            interval_id=interval_id,
            rows=rows,
            root_bb_catalog=root_bb_catalog,
        )

        write_json(bbv_info_dir / "bb_catalog.json", interval_bb_catalog)
        with (bbv_info_dir / "BB_order.txt").open("w", encoding="utf-8") as handle:
            for bb_index in ordered_bb_indices:
                handle.write(f"{bb_index}\n")

        print(f"[phase4] Collected interval_{interval_id} into {interval_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
