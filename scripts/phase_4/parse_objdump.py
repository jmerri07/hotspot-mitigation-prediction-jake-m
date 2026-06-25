#!/usr/bin/env python3
"""
parse_objdump.py

Parse an objdump text file into a structured JSON index.

Broad execution steps:
1. Parse symbol headers and instruction lines from objdump text.
2. Infer symbol ranges from the ordered symbol starts.
3. Build an address-indexed instruction table plus address-to-symbol mapping.
4. Write the structured JSON index used by the later Phase 4 scripts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SYMBOL_RE = re.compile(r"^\s*([0-9A-Fa-f]+)\s+<([^>]+)>:\s*$")
INSTRUCTION_RE = re.compile(
    r"^\s*([0-9A-Fa-f]+):\s*"
    r"((?:[0-9A-Fa-f]{2}(?:\s+|$))+)"
    r"(.*?)\s*$"
)


@dataclass(frozen=True)
class InstructionRecord:
    """A single instruction record parsed from objdump."""

    addr: int
    bytes_text: str
    asm: str

    def to_json(self) -> dict[str, str]:
        """Return a JSON-serializable representation."""
        return {
            "addr": format_addr(self.addr),
            "bytes": self.bytes_text,
            "asm": self.asm,
        }


@dataclass(frozen=True)
class SymbolRecord:
    """A symbol record with an inferred inclusive end address."""

    name: str
    start: int
    end: int

    def to_json(self) -> dict[str, str]:
        """Return a JSON-serializable representation."""
        return {
            "name": self.name,
            "start": format_addr(self.start),
            "end": format_addr(self.end),
        }


def format_addr(addr: int) -> str:
    """Format an address in canonical lower-case hexadecimal form."""
    return f"0x{addr:x}"


def parse_symbol_header(line: str) -> tuple[int, str] | None:
    """Parse a symbol header line.

    Example:
        00000000004014b6 <main>:
    """
    match = SYMBOL_RE.match(line)
    if not match:
        return None
    return int(match.group(1), 16), match.group(2)


def parse_instruction_line(line: str) -> InstructionRecord | None:
    """Parse an instruction line.

    Example:
        4017ae: 44 89 f7    mov %r14d,%edi

    Continuation lines that contain only trailing instruction bytes and no
    assembly text are ignored and return ``None``.
    """
    match = INSTRUCTION_RE.match(line)
    if not match:
        return None

    asm = match.group(3).strip()
    if not asm:
        return None

    addr = int(match.group(1), 16)
    byte_tokens = match.group(2).split()
    return InstructionRecord(addr=addr, bytes_text=" ".join(byte_tokens), asm=asm)


def parse_objdump_lines(lines: Iterable[str]) -> tuple[dict[int, InstructionRecord], list[tuple[int, str]]]:
    """Parse instructions and raw symbol starts from objdump lines."""
    instructions: dict[int, InstructionRecord] = {}
    raw_symbols: list[tuple[int, str]] = []

    for raw_line in lines:
        symbol = parse_symbol_header(raw_line)
        if symbol is not None:
            raw_symbols.append(symbol)
            continue

        instruction = parse_instruction_line(raw_line)
        if instruction is None:
            continue

        if instruction.addr in instructions:
            raise ValueError(
                f"duplicate instruction address encountered in objdump: {format_addr(instruction.addr)}"
            )
        instructions[instruction.addr] = instruction

    return instructions, raw_symbols


def infer_symbol_ranges(raw_symbols: list[tuple[int, str]]) -> list[SymbolRecord]:
    """Infer inclusive symbol end addresses from sorted symbol starts."""
    if not raw_symbols:
        return []

    sorted_symbols = sorted(raw_symbols, key=lambda item: item[0])
    records: list[SymbolRecord] = []
    for index, (start, name) in enumerate(sorted_symbols):
        if index + 1 < len(sorted_symbols):
            end = sorted_symbols[index + 1][0] - 1
        else:
            end = start
        records.append(SymbolRecord(name=name, start=start, end=end))
    return records


def build_addr_to_symbol(
    instruction_addrs: list[int], symbols: list[SymbolRecord]
) -> dict[int, str]:
    """Map instruction addresses to containing symbol names where possible."""
    if not instruction_addrs or not symbols:
        return {}

    symbol_starts = [symbol.start for symbol in symbols]
    addr_to_symbol: dict[int, str] = {}

    for addr in instruction_addrs:
        insert_index = bisect_right(symbol_starts, addr) - 1
        if insert_index < 0:
            continue
        symbol = symbols[insert_index]
        if symbol.start <= addr <= symbol.end:
            addr_to_symbol[addr] = symbol.name
    return addr_to_symbol


def build_index_from_objdump(path: Path) -> dict[str, object]:
    """Build the structured objdump index from a text file."""
    with path.open("r", encoding="utf-8") as handle:
        instructions, raw_symbols = parse_objdump_lines(handle)

    sorted_addrs = sorted(instructions)
    symbols = infer_symbol_ranges(raw_symbols)
    addr_to_symbol = build_addr_to_symbol(sorted_addrs, symbols)

    return {
        "instructions": {
            format_addr(addr): instructions[addr].to_json() for addr in sorted_addrs
        },
        "sorted_addrs": [format_addr(addr) for addr in sorted_addrs],
        "symbols": [symbol.to_json() for symbol in symbols],
        "addr_to_symbol": {
            format_addr(addr): name for addr, name in sorted(addr_to_symbol.items())
        },
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objdump", required=True, help="Path to the objdump text file.")
    parser.add_argument(
        "--out-json",
        default="objdump_index.json",
        help="Output path for the parsed JSON index.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    objdump_path = Path(args.objdump)
    out_path = Path(args.out_json)

    if not objdump_path.is_file():
        parser.error(f"objdump file does not exist: {objdump_path}")

    index = build_index_from_objdump(objdump_path)
    out_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "objdump": str(objdump_path),
                "out_json": str(out_path),
                "instruction_count": len(index["instructions"]),
                "symbol_count": len(index["symbols"]),
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
