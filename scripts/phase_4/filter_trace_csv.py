#!/usr/bin/env python3
"""
filter_trace_csv.py

Filter a translated-block trace CSV against main-binary objdump addresses.

Broad execution steps:
1. Parse the objdump text and collect the valid instruction addresses from the
   main binary.
2. Walk the BB-sequence CSV row by row.
3. Keep only rows whose PC belongs to the parsed objdump address set.
4. Emit both a filtered CSV and a JSON summary of what was kept and dropped.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from parse_objdump import build_index_from_objdump, format_addr


def parse_trace_pc(value: str) -> int | None:
    """Parse a trace PC field conservatively."""
    try:
        return int(value, 16)
    except ValueError:
        return None


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Path to the input trace CSV.")
    parser.add_argument("--objdump", required=True, help="Path to the objdump text file.")
    parser.add_argument(
        "--out-csv",
        default="filtered_bb_seq.csv",
        help="Output path for the filtered CSV.",
    )
    parser.add_argument(
        "--summary-json",
        default="filtered_trace_summary.json",
        help="Output path for the filter summary JSON.",
    )
    return parser


def filter_trace(
    csv_path: Path,
    valid_addrs: set[int],
    out_csv_path: Path,
) -> dict[str, Any]:
    """Filter the trace and return a summary dictionary."""
    bb_to_pc: dict[str, int] = {}
    kept_rows = 0
    dropped_rows = 0
    total_rows = 0
    kept_pcs: set[int] = set()
    dropped_pcs: set[int] = set()
    first_kept_seq_no: int | None = None
    last_kept_seq_no: int | None = None

    with csv_path.open("r", encoding="utf-8", newline="") as infile:
        reader = csv.DictReader(infile)
        if reader.fieldnames is None:
            raise ValueError(f"trace CSV has no header: {csv_path}")

        required = [
            "vcpu",
            "interval_id",
            "seq_no",
            "pc",
            "bb_index",
            "n_insns",
            "n_compute",
            "n_memory",
            "n_branch",
        ]
        missing = [name for name in required if name not in reader.fieldnames]
        if missing:
            raise ValueError(f"trace CSV is missing required columns: {', '.join(missing)}")

        with out_csv_path.open("w", encoding="utf-8", newline="") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
            writer.writeheader()

            for line_no, row in enumerate(reader, start=2):
                total_rows += 1
                bb_index = row["bb_index"]
                pc_raw = row["pc"]
                pc = parse_trace_pc(pc_raw)
                if pc is None:
                    dropped_rows += 1
                    continue

                previous_pc = bb_to_pc.setdefault(bb_index, pc)
                if previous_pc != pc:
                    raise ValueError(
                        "bb_index mapped to multiple PCs: "
                        f"bb_index={bb_index}, first={format_addr(previous_pc)}, "
                        f"later={format_addr(pc)}, line={line_no}"
                    )

                if pc not in valid_addrs:
                    dropped_rows += 1
                    dropped_pcs.add(pc)
                    continue

                writer.writerow(row)
                kept_rows += 1
                kept_pcs.add(pc)

                seq_no = int(row["seq_no"])
                if first_kept_seq_no is None or seq_no < first_kept_seq_no:
                    first_kept_seq_no = seq_no
                if last_kept_seq_no is None or seq_no > last_kept_seq_no:
                    last_kept_seq_no = seq_no

    return {
        "total_rows": total_rows,
        "kept_rows": kept_rows,
        "dropped_rows": dropped_rows,
        "unique_pcs_kept": len(kept_pcs),
        "unique_pcs_dropped": len(dropped_pcs),
        "first_kept_seq_no": first_kept_seq_no,
        "last_kept_seq_no": last_kept_seq_no,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    objdump_path = Path(args.objdump)
    out_csv_path = Path(args.out_csv)
    summary_path = Path(args.summary_json)

    if not csv_path.is_file():
        parser.error(f"trace CSV does not exist: {csv_path}")
    if not objdump_path.is_file():
        parser.error(f"objdump file does not exist: {objdump_path}")

    objdump_index = build_index_from_objdump(objdump_path)
    valid_addrs = {int(addr, 16) for addr in objdump_index["instructions"]}
    if not valid_addrs:
        raise ValueError(f"no instruction addresses were parsed from objdump: {objdump_path}")

    summary = filter_trace(csv_path=csv_path, valid_addrs=valid_addrs, out_csv_path=out_csv_path)
    summary.update(
        {
            "csv": str(csv_path),
            "objdump": str(objdump_path),
            "out_csv": str(out_csv_path),
            "valid_instruction_min": format_addr(min(valid_addrs)),
            "valid_instruction_max": format_addr(max(valid_addrs)),
            "valid_instruction_count": len(valid_addrs),
        }
    )

    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
