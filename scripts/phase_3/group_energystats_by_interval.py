#!/usr/bin/env python3
"""
group_energystats_by_interval.py

Given a Sniper output directory containing files like:
    energystats-temp-560000000000.xml

this script:
1. Finds all energystats XML files
2. Sorts them by numeric timestamp from the filename
3. Reads the per-file instruction count from:
       //component[@id="system.core0"]//stat[@name="total_instructions"]
4. Accumulates files into approximate instruction intervals
   (for example, 20 million instructions per interval)

The intervals are approximate:
if a file crosses an interval boundary, the whole file is placed in the
current interval and then a new interval begins afterward.

Example:
    python3 group_energystats_by_interval.py \
        /data/jake_m/HotGauge/snipersim/output/himenobmtxpa/7nm/4.5GHz \
        20

Optional CSV output:
    python3 group_energystats_by_interval.py \
        /data/jake_m/HotGauge/snipersim/output/himenobmtxpa/7nm/4.5GHz \
        20 \
        --csv intervals.csv

In the Mayfew workflow, this CSV is typically written under:
    Mayfew/Outputs/<executable_dir>/intervals.csv
"""

import argparse
import csv
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


ENERGYSTATS_PATTERN = re.compile(r"energystats-temp-(\d+)\.xml$")


@dataclass
class EnergystatsFile:
    path: Path
    timestamp: int
    instructions: int


@dataclass
class IntervalGroup:
    interval_index: int
    target_start_instruction: int
    target_end_instruction: int
    actual_start_instruction: int
    actual_end_instruction: int
    total_instructions: int
    num_files: int
    files: List[EnergystatsFile]


def extract_timestamp(path: Path) -> Optional[int]:
    """
    Extract numeric timestamp from filenames like:
        energystats-temp-560000000000.xml
    """
    match = ENERGYSTATS_PATTERN.match(path.name)
    if not match:
        return None
    return int(match.group(1))


def read_total_instructions(xml_path: Path) -> int:
    """
    Read total_instructions from the XML.

    Primary target:
        component id="system.core0"
        stat name="total_instructions"

    If that exact component is not found, fall back to the first
    stat named total_instructions anywhere in the document.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Failed to parse XML: {xml_path} ({exc})") from exc

    # Preferred: system.core0 total_instructions
    for comp in root.iter("component"):
        if comp.attrib.get("id") == "system.core0":
            for stat in comp.iter("stat"):
                if stat.attrib.get("name") == "total_instructions":
                    value = stat.attrib.get("value")
                    if value is None:
                        raise ValueError(
                            f"Missing value attribute for total_instructions in {xml_path}"
                        )
                    return int(value)

    # Fallback: first total_instructions anywhere
    for stat in root.iter("stat"):
        if stat.attrib.get("name") == "total_instructions":
            value = stat.attrib.get("value")
            if value is None:
                raise ValueError(
                    f"Missing value attribute for total_instructions in {xml_path}"
                )
            return int(value)

    raise ValueError(f"Could not find total_instructions in {xml_path}")


def discover_energystats_files(output_dir: Path) -> List[EnergystatsFile]:
    """
    Find and load all energystats-temp-*.xml files from the given directory.
    """
    if not output_dir.is_dir():
        raise ValueError(f"Not a directory: {output_dir}")

    discovered: List[EnergystatsFile] = []

    for path in output_dir.iterdir():
        if not path.is_file():
            continue

        timestamp = extract_timestamp(path)
        if timestamp is None:
            continue

        instructions = read_total_instructions(path)
        discovered.append(
            EnergystatsFile(
                path=path,
                timestamp=timestamp,
                instructions=instructions,
            )
        )

    discovered.sort(key=lambda item: item.timestamp)
    return discovered


def group_into_intervals(
    files: List[EnergystatsFile],
    interval_size_instructions: int,
) -> List[IntervalGroup]:
    """
    Group files into approximate instruction intervals.

    Rule:
    - Keep adding files to the current interval.
    - If adding a file causes us to reach or exceed the target interval size,
      include that whole file in the current interval, close the interval,
      and start a new one for subsequent files.

    This matches your "good enough" approximation requirement.
    """
    intervals: List[IntervalGroup] = []

    if not files:
        return intervals

    current_files: List[EnergystatsFile] = []
    current_sum = 0
    cumulative_before_interval = 0
    interval_index = 0

    for file_info in files:
        current_files.append(file_info)
        current_sum += file_info.instructions

        if current_sum >= interval_size_instructions:
            interval_start = cumulative_before_interval
            interval_end = cumulative_before_interval + current_sum

            intervals.append(
                IntervalGroup(
                    interval_index=interval_index,
                    target_start_instruction=interval_index * interval_size_instructions,
                    target_end_instruction=(interval_index + 1) * interval_size_instructions,
                    actual_start_instruction=interval_start,
                    actual_end_instruction=interval_end,
                    total_instructions=current_sum,
                    num_files=len(current_files),
                    files=list(current_files),
                )
            )

            cumulative_before_interval += current_sum
            interval_index += 1
            current_files = []
            current_sum = 0

    # Handle leftover files that do not fill a full interval
    if current_files:
        interval_start = cumulative_before_interval
        interval_end = cumulative_before_interval + current_sum

        intervals.append(
            IntervalGroup(
                interval_index=interval_index,
                target_start_instruction=interval_index * interval_size_instructions,
                target_end_instruction=(interval_index + 1) * interval_size_instructions,
                actual_start_instruction=interval_start,
                actual_end_instruction=interval_end,
                total_instructions=current_sum,
                num_files=len(current_files),
                files=list(current_files),
            )
        )

    return intervals


def print_summary(files: List[EnergystatsFile], intervals: List[IntervalGroup], interval_millions: float) -> None:
    """
    Print a readable text summary.
    """
    total_instructions = sum(f.instructions for f in files)

    print(f"Discovered energystats files: {len(files)}")
    print(f"Requested interval size: {interval_millions:g} million instructions")
    print(f"Requested interval size (raw): {int(interval_millions * 1_000_000):,} instructions")
    print(f"Total instructions across all files: {total_instructions:,}")
    print(f"Produced intervals: {len(intervals)}")
    print()

    for interval in intervals:
        first_file = interval.files[0].path.name
        last_file = interval.files[-1].path.name

        print(
            f"Interval {interval.interval_index}: "
            f"target [{interval.target_start_instruction:,}, {interval.target_end_instruction:,}) | "
            f"actual [{interval.actual_start_instruction:,}, {interval.actual_end_instruction:,}) | "
            f"files={interval.num_files} | "
            f"instructions={interval.total_instructions:,}"
        )
        print(f"  first file: {first_file}")
        print(f"  last file : {last_file}")
        print("  member files:")
        for f in interval.files:
            print(f"    {f.path.name}  instructions={f.instructions:,}")
        print()


def write_csv(intervals: List[IntervalGroup], csv_path: Path, timestamp_order_map) -> None:
    """
    Write interval membership to a CSV file.

    One row per file, including interval metadata.
    """
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "interval_index",
            "target_start_instruction",
            "target_end_instruction",
            "actual_start_instruction",
            "actual_end_instruction",
            "interval_total_instructions",
            "num_files_in_interval",
            "file_timestamp",
            "timestamp_order",
            "file_instructions",
            "file_name",
            "file_path",
        ])

        for interval in intervals:
            for f in interval.files:
                writer.writerow([
                    interval.interval_index,
                    interval.target_start_instruction,
                    interval.target_end_instruction,
                    interval.actual_start_instruction,
                    interval.actual_end_instruction,
                    interval.total_instructions,
                    interval.num_files,
                    f.timestamp,
                    timestamp_order_map[f.path],
                    f.instructions,
                    f.path.name,
                    str(f.path),
                ])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Group Sniper energystats XML files into approximate instruction intervals."
    )
    parser.add_argument(
        "output_dir",
        help="Absolute path to the Sniper output directory containing energystats-temp-*.xml files",
    )
    parser.add_argument(
        "interval_millions",
        type=float,
        help="Interval size in millions of instructions (e.g. 20 means 20,000,000)",
    )
    parser.add_argument(
        "--csv",
        dest="csv_path",
        help="Optional output CSV path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    interval_millions = args.interval_millions

    if interval_millions <= 0:
        print("ERROR: interval_millions must be > 0", file=sys.stderr)
        return 1

    interval_size_instructions = int(interval_millions * 1_000_000)

    try:
        files = discover_energystats_files(output_dir)
        timestamp_order_map = {
            f.path: idx for idx, f in enumerate(files)
        }
    except Exception as exc:
        print(f"ERROR while discovering energystats files: {exc}", file=sys.stderr)
        return 1

    if not files:
        print(
            f"ERROR: No files matching energystats-temp-*.xml found in {output_dir}",
            file=sys.stderr,
        )
        return 1

    intervals = group_into_intervals(files, interval_size_instructions)

    print_summary(files, intervals, interval_millions)

    if args.csv_path:
        csv_path = Path(args.csv_path).expanduser().resolve()
        try:
            write_csv(intervals, csv_path, timestamp_order_map)
        except Exception as exc:
            print(f"ERROR while writing CSV: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote CSV: {csv_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
