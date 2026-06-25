#!/usr/bin/env python3
"""
extract_bbs.py

Extract replayable BB/TB units from a filtered trace and objdump index.

Broad execution steps:
1. Load the filtered trace CSV and group rows by basic-block index.
2. Load the parsed objdump index.
3. Reconstruct each BB's instruction stream from the objdump instruction table.
4. Emit one JSON catalog that combines the static BB instructions with the
   dynamic BB sequence seen in the filtered trace.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from bisect import bisect_left
from pathlib import Path
from typing import Any

from parse_objdump import format_addr

CONTROL_FLOW_PREFIXES = ("j", "call", "ret", "loop")


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Path to the filtered trace CSV.")
    parser.add_argument("--objdump-json", required=True, help="Path to the parsed objdump JSON.")
    parser.add_argument(
        "--icount-json",
        help="Optional JSON mapping from PC address string to trusted instruction count.",
    )
    parser.add_argument(
        "--out-json",
        default="bb_catalog.json",
        help="Output path for the extracted BB catalog JSON.",
    )
    return parser


def load_json(path: Path) -> Any:
    """Load JSON from disk."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_icount_mapping(raw: Any) -> dict[int, int]:
    """Normalize an external PC->instruction-count mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("icount JSON must be an object mapping PCs to counts")

    normalized: dict[int, int] = {}
    for key, value in raw.items():
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"invalid icount value for {key!r}: expected positive integer")
        try:
            pc = int(str(key), 16)
        except ValueError as exc:
            raise ValueError(f"invalid icount PC key {key!r}: expected hex string") from exc
        normalized[pc] = value
    return normalized


def is_terminator(asm_text: str) -> bool:
    """Return True when the instruction is treated as a control-flow terminator."""
    mnemonic = asm_text.split(None, 1)[0].lower()
    return mnemonic.startswith(CONTROL_FLOW_PREFIXES)


def extract_with_trusted_length(
    start_pc: int,
    length: int,
    sorted_addrs: list[int],
    instructions: dict[int, dict[str, str]],
) -> list[dict[str, str]]:
    """Extract exactly ``length`` instructions starting at ``start_pc``."""
    start_index = bisect_left(sorted_addrs, start_pc)
    if start_index >= len(sorted_addrs) or sorted_addrs[start_index] != start_pc:
        raise ValueError(f"start PC not found in objdump instruction table: {format_addr(start_pc)}")

    end_index = start_index + length
    if end_index > len(sorted_addrs):
        raise ValueError(
            f"trusted extraction overruns objdump instruction table: start={format_addr(start_pc)}, "
            f"length={length}"
        )

    return [instructions[addr] for addr in sorted_addrs[start_index:end_index]]


def extract_with_terminator_fallback(
    start_pc: int,
    sorted_addrs: list[int],
    instructions: dict[int, dict[str, str]],
) -> list[dict[str, str]]:
    """Extract forward until the first terminator instruction."""
    start_index = bisect_left(sorted_addrs, start_pc)
    if start_index >= len(sorted_addrs) or sorted_addrs[start_index] != start_pc:
        raise ValueError(f"start PC not found in objdump instruction table: {format_addr(start_pc)}")

    extracted: list[dict[str, str]] = []
    for addr in sorted_addrs[start_index:]:
        record = instructions[addr]
        extracted.append(record)
        if is_terminator(record["asm"]):
            break

    return extracted


def load_filtered_trace(csv_path: Path) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
    """Load and validate the filtered trace."""
    rows: list[dict[str, str]] = []
    bb_info: dict[str, dict[str, Any]] = {}

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"filtered CSV has no header: {csv_path}")

        for line_no, row in enumerate(reader, start=2):
            rows.append(row)

            bb_index = row["bb_index"]
            pc = int(row["pc"], 16)
            n_insns = int(row["n_insns"])
            info = bb_info.setdefault(
                bb_index,
                {
                    "pc": pc,
                    "csv_n_insns": set(),
                },
            )

            if info["pc"] != pc:
                raise ValueError(
                    "bb_index mapped to multiple PCs in filtered CSV: "
                    f"bb_index={bb_index}, first={format_addr(info['pc'])}, "
                    f"later={format_addr(pc)}, line={line_no}"
                )
            info["csv_n_insns"].add(n_insns)

    for bb_index, info in bb_info.items():
        counts = info["csv_n_insns"]
        if len(counts) > 1:
            raise ValueError(
                f"conflicting trusted n_insns values for bb_index={bb_index}: {sorted(counts)}"
            )
        info["trusted_csv_n_insns"] = next(iter(counts)) if counts else None

    return rows, bb_info


def build_catalog(
    rows: list[dict[str, str]],
    bb_info: dict[str, dict[str, Any]],
    objdump_index: dict[str, Any],
    icount_map: dict[int, int],
) -> dict[str, Any]:
    """Build the BB extraction catalog."""
    instructions_by_addr = {
        int(addr, 16): record for addr, record in objdump_index["instructions"].items()
    }
    sorted_addrs = [int(addr, 16) for addr in objdump_index["sorted_addrs"]]
    addr_to_symbol = {
        int(addr, 16): name for addr, name in objdump_index.get("addr_to_symbol", {}).items()
    }

    sequence_rows = sorted(rows, key=lambda row: int(row["seq_no"]))
    sequence = [int(row["bb_index"]) for row in sequence_rows]

    warnings: list[str] = []
    bb_catalog: dict[str, Any] = {}
    pc_to_bb_index: dict[str, int] = {}

    for bb_index_str, info in sorted(bb_info.items(), key=lambda item: int(item[0])):
        start_pc = info["pc"]
        if start_pc not in instructions_by_addr:
            raise ValueError(
                f"filtered trace references PC missing from objdump instruction table: {format_addr(start_pc)}"
            )

        trusted_count = icount_map.get(start_pc, info.get("trusted_csv_n_insns"))
        fallback = extract_with_terminator_fallback(
            start_pc=start_pc,
            sorted_addrs=sorted_addrs,
            instructions=instructions_by_addr,
        )

        if trusted_count is not None:
            trusted = extract_with_trusted_length(
                start_pc=start_pc,
                length=trusted_count,
                sorted_addrs=sorted_addrs,
                instructions=instructions_by_addr,
            )
            if len(trusted) != len(fallback):
                warnings.append(
                    "trusted length disagrees with terminator fallback for "
                    f"bb_index={bb_index_str}, pc={format_addr(start_pc)}, "
                    f"trusted={len(trusted)}, fallback={len(fallback)}"
                )
            extracted = trusted
            source_length_mode = "trusted_icount"
        else:
            extracted = fallback
            source_length_mode = "terminator_fallback"

        record = {
            "bb_index": int(bb_index_str),
            "start_pc": format_addr(start_pc),
            "symbol": addr_to_symbol.get(start_pc),
            "instructions": extracted,
            "n_instructions_extracted": len(extracted),
            "source_length_mode": source_length_mode,
        }
        bb_catalog[bb_index_str] = record
        pc_to_bb_index[format_addr(start_pc)] = int(bb_index_str)

    return {
        "bb_catalog": bb_catalog,
        "sequence": sequence,
        "pc_to_bb_index": pc_to_bb_index,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    objdump_json_path = Path(args.objdump_json)
    out_json_path = Path(args.out_json)

    if not csv_path.is_file():
        parser.error(f"filtered CSV does not exist: {csv_path}")
    if not objdump_json_path.is_file():
        parser.error(f"objdump JSON does not exist: {objdump_json_path}")

    rows, bb_info = load_filtered_trace(csv_path)
    objdump_index = load_json(objdump_json_path)
    icount_map = normalize_icount_mapping(load_json(Path(args.icount_json))) if args.icount_json else {}

    catalog = build_catalog(rows=rows, bb_info=bb_info, objdump_index=objdump_index, icount_map=icount_map)
    out_json_path.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "csv": str(csv_path),
                "objdump_json": str(objdump_json_path),
                "out_json": str(out_json_path),
                "bb_count": len(catalog["bb_catalog"]),
                "sequence_length": len(catalog["sequence"]),
                "warning_count": len(catalog["warnings"]),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
