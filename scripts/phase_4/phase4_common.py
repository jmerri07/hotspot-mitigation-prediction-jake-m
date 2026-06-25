#!/usr/bin/env python3
"""
phase4_common.py

Shared helpers for the Mayfew Phase 4 scripts.

This module intentionally centralizes the reusable pieces of the Phase 4
pipeline so that:
1. The profiler wrapper, transfer script, and dataset builder all agree on the
   same Mayfew / HotGauge / profiler directory conventions.
2. XML parsing, CSV parsing, and thermal-label extraction stay consistent.
3. The conservative static x86 analysis lives in one place instead of being
   reimplemented differently by multiple scripts.
"""

from __future__ import annotations

import csv
import json
import math
import re
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ENERGYSTATS_COUNTER_SPECS = [
    {
        "feature_name": "cdb_alu_accesses",
        "component_id": "system.core0",
        "stat_name": "cdb_alu_accesses",
    },
    {
        "feature_name": "dcache_read_accesses",
        "component_id": "system.core0.dcache",
        "stat_name": "read_accesses",
    },
    {
        "feature_name": "rob_reads",
        "component_id": "system.core0",
        "stat_name": "ROB_reads",
    },
    {
        "feature_name": "busy_cycles",
        "component_id": "system.core0",
        "stat_name": "busy_cycles",
    },
    {
        "feature_name": "icache_read_accesses",
        "component_id": "system.core0.icache",
        "stat_name": "read_accesses",
    },
    {
        "feature_name": "committed_int_instructions",
        "component_id": "system.core0",
        "stat_name": "committed_int_instructions",
    },
    {
        "feature_name": "dtlb_total_accesses",
        "component_id": "system.core0.dtlb",
        "stat_name": "total_accesses",
    },
    {
        "feature_name": "itlb_total_misses",
        "component_id": "system.core0.itlb",
        "stat_name": "total_misses",
    },
    {
        "feature_name": "btb_read_accesses",
        "component_id": "system.core0.BTB",
        "stat_name": "read_accesses",
    },
    {
        "feature_name": "dcache_read_misses",
        "component_id": "system.core0.dcache",
        "stat_name": "read_misses",
    },
    {
        "feature_name": "cdb_fpu_accesses",
        "component_id": "system.core0",
        "stat_name": "cdb_fpu_accesses",
    },
    {
        "feature_name": "branch_mispredictions",
        "component_id": "system.core0",
        "stat_name": "branch_mispredictions",
    },
    {
        "feature_name": "dcache_write_accesses",
        "component_id": "system.core0.dcache",
        "stat_name": "write_accesses",
    },
]

ENERGYSTATS_DUTY_CYCLE_SPECS = [
    ("alu_duty_cycle", "system.core0", "ALU_duty_cycle"),
    ("mul_cdb_duty_cycle", "system.core0", "MUL_cdb_duty_cycle"),
    ("lsu_duty_cycle", "system.core0", "LSU_duty_cycle"),
    ("ifu_duty_cycle", "system.core0", "IFU_duty_cycle"),
    ("fpu_cdb_duty_cycle", "system.core0", "FPU_cdb_duty_cycle"),
]

SERIALIZING_MNEMONICS = {
    "cpuid",
    "mfence",
    "lfence",
    "sfence",
    "rdtsc",
    "wrmsr",
    "call",
    "ret",
}

BRANCH_PREFIXES = ("j", "call", "ret", "loop")
COMPARE_MNEMONICS = {"cmp", "test", "ucomisd", "ucomiss", "comisd", "comiss"}
READ_WRITE_MNEMONICS = {
    "add",
    "adc",
    "sub",
    "sbb",
    "and",
    "or",
    "xor",
    "imul",
    "mul",
    "idiv",
    "div",
    "shl",
    "shr",
    "sar",
    "sal",
    "rol",
    "ror",
    "rcl",
    "rcr",
    "inc",
    "dec",
    "neg",
    "not",
    "btc",
    "btr",
    "bts",
    "xadd",
    "cmpxchg",
}
FLAGS_READING_MNEMONICS = {
    "adc",
    "sbb",
    "cmova",
    "cmovae",
    "cmovb",
    "cmovbe",
    "cmove",
    "cmovg",
    "cmovge",
    "cmovl",
    "cmovle",
    "cmovne",
    "cmovno",
    "cmovnp",
    "cmovns",
    "cmovo",
    "cmovp",
    "cmovs",
    "seta",
    "setae",
    "setb",
    "setbe",
    "sete",
    "setg",
    "setge",
    "setl",
    "setle",
    "setne",
    "setno",
    "setnp",
    "setns",
    "seto",
    "setp",
    "sets",
}
FLAGS_WRITING_PREFIXES = (
    "add",
    "sub",
    "adc",
    "sbb",
    "and",
    "or",
    "xor",
    "imul",
    "mul",
    "idiv",
    "div",
    "cmp",
    "test",
    "sh",
    "ro",
    "inc",
    "dec",
    "neg",
    "not",
)

REGISTER_ALIASES = {
    "rax": {"rax", "eax", "ax", "al", "ah"},
    "rbx": {"rbx", "ebx", "bx", "bl", "bh"},
    "rcx": {"rcx", "ecx", "cx", "cl", "ch"},
    "rdx": {"rdx", "edx", "dx", "dl", "dh"},
    "rsi": {"rsi", "esi", "si", "sil"},
    "rdi": {"rdi", "edi", "di", "dil"},
    "rbp": {"rbp", "ebp", "bp", "bpl"},
    "rsp": {"rsp", "esp", "sp", "spl"},
    "r8": {"r8", "r8d", "r8w", "r8b"},
    "r9": {"r9", "r9d", "r9w", "r9b"},
    "r10": {"r10", "r10d", "r10w", "r10b"},
    "r11": {"r11", "r11d", "r11w", "r11b"},
    "r12": {"r12", "r12d", "r12w", "r12b"},
    "r13": {"r13", "r13d", "r13w", "r13b"},
    "r14": {"r14", "r14d", "r14w", "r14b"},
    "r15": {"r15", "r15d", "r15w", "r15b"},
    "rip": {"rip", "eip", "ip"},
}

NORMALIZED_REGISTER_LOOKUP: dict[str, str] = {}
for canonical_name, aliases in REGISTER_ALIASES.items():
    for alias in aliases:
        NORMALIZED_REGISTER_LOOKUP[alias] = canonical_name


@dataclass(frozen=True)
class ParsedOperand:
    """A small normalized view of one x86 AT&T operand."""

    text: str
    kind: str
    registers: tuple[str, ...]


@dataclass(frozen=True)
class InstructionAnalysis:
    """Static analysis record for one instruction inside a BB."""

    mnemonic: str
    category: str
    is_serializing: bool
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    cp_depth: int


def find_mayfew_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_outputs_root() -> Path:
    """
    Resolve the canonical Mayfew outputs root.

    We prefer `Outputs`, but we still honor a lowercase existing directory for
    compatibility with earlier experiments.
    """
    mayfew_root = find_mayfew_root()
    uppercase = mayfew_root / "Outputs"
    lowercase = mayfew_root / "outputs"
    if uppercase.exists():
        return uppercase
    if lowercase.exists():
        return lowercase
    return uppercase


def default_profiler_dir() -> Path:
    return find_mayfew_root().parent / "coop-precommit" / "profilers"


def default_hotgauge_root() -> Path:
    return find_mayfew_root().parent / "HotGauge"


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sanitize_name(text: str) -> str:
    sanitized = [
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in text
    ]
    return "".join(sanitized).strip("_") or "run"


def run_command(command: list[str], cwd: Path, verbose: bool = True, check: bool = True) -> int:
    """
    Run one subprocess with predictable logging.

    Broadly, every Phase 4 script follows this same execution pattern:
    1. Print the command and cwd so the workflow is inspectable.
    2. Run the command.
    3. Either fail fast or return the exit code for warning-and-continue logic.
    """
    if verbose:
        print("[phase4] Running command:")
        print("  " + " ".join(shlex.quote(part) for part in command))
        print(f"  cwd={cwd}")

    completed = subprocess.run(
        command,
        cwd=str(cwd),
        stdout=None if verbose else subprocess.DEVNULL,
        stderr=None if verbose else subprocess.DEVNULL,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {completed.returncode}: {' '.join(command)}"
        )
    return completed.returncode


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def load_representative_intervals(simpoints_path: Path) -> list[int]:
    """
    Parse `results.simpts` and return representative interval ids.

    File format:
      <interval_index> <cluster_id>
    """
    intervals: list[int] = []
    with simpoints_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 2:
                raise ValueError(f"unexpected simpoints line in {simpoints_path}: {line}")
            intervals.append(int(parts[0]))
    return sorted(dict.fromkeys(intervals))


def parse_metadata_interval_filename(path: Path) -> int | None:
    match = re.search(r"_interval_(\d+)\.json$", path.name)
    if not match:
        return None
    return int(match.group(1))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def safe_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0 if numerator == 0 else float("nan")
    return numerator / denominator


def log_safe_entropy(weights: list[float], normalize_by_categories: bool = False) -> float:
    """
    Compute entropy with natural logarithms.

    When `normalize_by_categories` is true, divide by ln(K) where K is the
    number of categories that were available to the calculation.
    """
    if not weights:
        return 0.0

    positive = [weight for weight in weights if weight > 0]
    if not positive:
        return 0.0

    total = sum(positive)
    entropy = 0.0
    for weight in positive:
        probability = weight / total
        entropy -= probability * math.log(probability)

    if normalize_by_categories:
        category_count = len(weights)
        if category_count <= 1:
            return 0.0
        return entropy / math.log(category_count)
    return entropy


def parse_die_grid_values(path: Path) -> list[float]:
    """
    Parse every numeric value from `die_grid.temps`.

    The file can contain comment/header lines. We treat every remaining token on
    numeric lines as one temperature sample and flatten the entire file.
    """
    values: list[float] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("%"):
            continue
        for token in line.split():
            values.append(float(token))
    return values


def read_maxima_rows(path: Path) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                {
                    "x_idx": float(row["x_idx"]),
                    "y_idx": float(row["y_idx"]),
                    "temp_xy": float(row["temp_xy"]),
                    "neg_MLTD": float(row["neg_MLTD"]),
                    "pos_MLTD": float(row["pos_MLTD"]),
                    "time_step": float(row["time_step"]),
                }
            )
    return rows


def _logistic(value: float, x0: float, y0: float, slope: float, amplitude: float) -> float:
    return amplitude / (1.0 + math.exp(-slope * (value - x0))) + y0


def hotspot_severity(temp_xy: float, pos_mltd: float) -> float:
    sigma_df = _logistic(temp_xy, 115.0, 0.0, 0.2, 2.0)
    sigma_m = _logistic(pos_mltd, 15.0, -0.25, 0.2, 1.25)
    sigma_t = _logistic(temp_xy, 60.0, 0.35, 0.05, 0.65)
    return sigma_df + sigma_m * sigma_t


def find_component_by_id(root: ET.Element, component_id: str) -> ET.Element | None:
    for component in root.iter("component"):
        if component.attrib.get("id") == component_id:
            return component
    return None


def find_stat_value(component: ET.Element | None, stat_name: str) -> float | None:
    if component is None:
        return None
    for stat in component.iter("stat"):
        if stat.attrib.get("name") == stat_name:
            value = stat.attrib.get("value")
            if value is None:
                return None
            return float(value)
    return None


def load_energystats_features(xml_path: Path) -> dict[str, float]:
    """
    Extract the raw per-file energystats quantities needed by the dataset.

    Step 1:
      Parse the XML tree.
    Step 2:
      Pull the exact component/stat pairs the feature spec calls for.
    Step 3:
      Derive `real_instructions` and `real_ipc` for this individual file.
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    values: dict[str, float] = {}
    for spec in ENERGYSTATS_COUNTER_SPECS:
        component = find_component_by_id(root, spec["component_id"])
        raw_value = find_stat_value(component, spec["stat_name"])
        if raw_value is None:
            raise ValueError(
                f"Missing {spec['component_id']}:{spec['stat_name']} in {xml_path}"
            )
        values[spec["feature_name"]] = raw_value

    core = find_component_by_id(root, "system.core0")
    total_cycles = find_stat_value(core, "total_cycles")
    total_instructions = find_stat_value(core, "total_instructions")
    nop_instructions = find_stat_value(core, "NOP_instructions")
    if total_cycles is None or total_instructions is None or nop_instructions is None:
        raise ValueError(f"Missing required core stats in {xml_path}")

    values["total_cycles"] = total_cycles
    values["total_instructions"] = total_instructions
    values["nop_instructions"] = nop_instructions
    values["real_instructions"] = total_instructions - nop_instructions
    values["real_ipc"] = safe_ratio(values["real_instructions"], total_cycles)

    for feature_name, component_id, stat_name in ENERGYSTATS_DUTY_CYCLE_SPECS:
        component = find_component_by_id(root, component_id)
        raw_value = find_stat_value(component, stat_name)
        if raw_value is None:
            raise ValueError(f"Missing {component_id}:{stat_name} in {xml_path}")
        values[feature_name] = raw_value

    return values


def aggregate_energystats_interval(xml_paths: list[Path]) -> dict[str, float]:
    """
    Aggregate all energystats XML files that belong to one interval.

    The user explicitly noted that these XML files are not cumulative, so the
    interval-level features sum per-file counters and then derive normalized
    values over the interval's total cycles.
    """
    if not xml_paths:
        raise ValueError("No energystats XML files were provided for this interval")

    per_file = [load_energystats_features(path) for path in xml_paths]

    total_cycles = sum(record["total_cycles"] for record in per_file)
    interval_real_instructions = sum(record["real_instructions"] for record in per_file)

    aggregated: dict[str, float] = {
        "energystats_file_count": float(len(per_file)),
        "total_cycles": total_cycles,
        "real_ipc": safe_ratio(interval_real_instructions, total_cycles),
        "max_real_ipc_over_interval_real_ipc": safe_ratio(
            max(record["real_ipc"] for record in per_file),
            safe_ratio(interval_real_instructions, total_cycles),
        ),
    }

    for spec in ENERGYSTATS_COUNTER_SPECS:
        feature_name = spec["feature_name"]
        total_value = sum(record[feature_name] for record in per_file)
        aggregated[f"sum_{feature_name}"] = total_value
        aggregated[f"per_cycle_{feature_name}"] = safe_ratio(total_value, total_cycles)

    aggregated["sum_real_instructions"] = interval_real_instructions
    aggregated["per_cycle_real_instructions"] = safe_ratio(
        interval_real_instructions,
        total_cycles,
    )

    for feature_name, _, _ in ENERGYSTATS_DUTY_CYCLE_SPECS:
        values = [record[feature_name] for record in per_file]
        average_value = sum(values) / len(values)
        peak_value = max(values)
        aggregated[f"avg_{feature_name}"] = average_value
        aggregated[f"peak_over_avg_{feature_name}"] = safe_ratio(peak_value, average_value)

    return aggregated


def normalize_register(register_text: str) -> str:
    lower = register_text.lower().lstrip("%")
    if lower in NORMALIZED_REGISTER_LOOKUP:
        return NORMALIZED_REGISTER_LOOKUP[lower]
    if lower.startswith(("xmm", "ymm", "zmm", "mm")):
        digits = "".join(character for character in lower if character.isdigit())
        return f"vec{digits}" if digits else lower
    if lower.startswith("st"):
        return lower
    return lower


def split_operands(operand_text: str) -> list[str]:
    """
    Split an AT&T operand list while respecting memory-address parentheses.
    """
    if not operand_text.strip():
        return []

    operands: list[str] = []
    current: list[str] = []
    depth = 0
    for character in operand_text:
        if character == "," and depth == 0:
            operands.append("".join(current).strip())
            current = []
            continue
        current.append(character)
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
    if current:
        operands.append("".join(current).strip())
    return [operand for operand in operands if operand]


def parse_operand(operand_text: str) -> ParsedOperand:
    text = operand_text.strip()
    registers = tuple(
        normalize_register(match.group(0))
        for match in re.finditer(r"%[A-Za-z][A-Za-z0-9]*", text)
    )
    if not text:
        return ParsedOperand(text=text, kind="empty", registers=registers)
    if text.startswith("$"):
        return ParsedOperand(text=text, kind="immediate", registers=registers)
    if "%" in text and "(" not in text and ")" not in text:
        return ParsedOperand(text=text, kind="register", registers=registers)
    if "(" in text or ")" in text:
        return ParsedOperand(text=text, kind="memory", registers=registers)
    return ParsedOperand(text=text, kind="other", registers=registers)


def classify_instruction_category(mnemonic: str, operands: list[ParsedOperand]) -> str:
    lower = mnemonic.lower()

    if lower.startswith(BRANCH_PREFIXES):
        return "branch"

    memory_operands = [operand for operand in operands if operand.kind == "memory"]
    if memory_operands:
        if lower in {"lea"}:
            pass
        elif len(operands) >= 1 and operands[-1].kind == "memory":
            return "store"
        else:
            return "load"

    if "mul" in lower or "div" in lower:
        return "mul_div"

    if lower.startswith(
        (
            "v",
            "padd",
            "psub",
            "pmul",
            "pand",
            "por",
            "pxor",
            "movdqa",
            "movdqu",
            "movaps",
            "movups",
            "shuf",
            "pack",
            "unpck",
        )
    ):
        return "simd"

    if lower.startswith(
        (
            "f",
            "ucomis",
            "comis",
            "adds",
            "subs",
            "muls",
            "divs",
            "sqrt",
            "cvts",
        )
    ):
        return "fp"

    return "integer_alu"


def resources_for_instruction(asm_text: str) -> tuple[str, list[ParsedOperand], set[str], set[str]]:
    """
    Build a conservative read/write resource model for one x86 instruction.

    The goal here is not to model every x86 nuance perfectly. Instead we prefer
    a simple, conservative approximation that is stable and easy to reason
    about for the interval-level critical-path proxy features.
    """
    stripped = asm_text.strip()
    if not stripped:
        return "", [], set(), set()

    parts = stripped.split(None, 1)
    mnemonic = parts[0].lower()
    operand_text = parts[1] if len(parts) > 1 else ""
    operands = [parse_operand(text) for text in split_operands(operand_text)]

    reads: set[str] = set()
    writes: set[str] = set()

    for operand in operands:
        if operand.kind == "memory":
            reads.update(operand.registers)
        elif operand.kind == "other":
            reads.update(operand.registers)

    if mnemonic.startswith(BRANCH_PREFIXES):
        if mnemonic not in {"jmp", "call", "ret"}:
            reads.add("FLAGS")
        for operand in operands:
            if operand.kind in {"register", "memory", "other"}:
                reads.update(operand.registers)
            if operand.kind == "memory":
                reads.add("MEM")
        if mnemonic == "call":
            reads.add("rsp")
            writes.update({"rsp", "MEM"})
        elif mnemonic == "ret":
            reads.update({"rsp", "MEM"})
            writes.add("rsp")
        return mnemonic, operands, reads, writes

    if mnemonic in COMPARE_MNEMONICS:
        for operand in operands:
            if operand.kind == "register":
                reads.update(operand.registers)
            elif operand.kind == "memory":
                reads.update(operand.registers)
                reads.add("MEM")
        writes.add("FLAGS")
        return mnemonic, operands, reads, writes

    if mnemonic in {"push"} and operands:
        source = operands[0]
        if source.kind == "register":
            reads.update(source.registers)
        elif source.kind == "memory":
            reads.update(source.registers)
            reads.add("MEM")
        reads.add("rsp")
        writes.update({"rsp", "MEM"})
        return mnemonic, operands, reads, writes

    if mnemonic in {"pop"} and operands:
        destination = operands[0]
        reads.update({"rsp", "MEM"})
        writes.add("rsp")
        if destination.kind == "register":
            writes.update(destination.registers)
        elif destination.kind == "memory":
            reads.update(destination.registers)
            writes.add("MEM")
        return mnemonic, operands, reads, writes

    if mnemonic == "lea" and len(operands) >= 2:
        source = operands[0]
        destination = operands[-1]
        reads.update(source.registers)
        if destination.kind == "register":
            writes.update(destination.registers)
        return mnemonic, operands, reads, writes

    dest = operands[-1] if operands else None
    sources = operands[:-1] if len(operands) > 1 else operands[:1]

    for operand in sources:
        if operand.kind == "register":
            reads.update(operand.registers)
        elif operand.kind == "memory":
            reads.update(operand.registers)
            reads.add("MEM")
        elif operand.kind == "other":
            reads.update(operand.registers)

    if dest is not None:
        if mnemonic in READ_WRITE_MNEMONICS or mnemonic in FLAGS_READING_MNEMONICS:
            if dest.kind == "register":
                reads.update(dest.registers)
            elif dest.kind == "memory":
                reads.update(dest.registers)
                reads.add("MEM")

        if dest.kind == "register":
            writes.update(dest.registers)
        elif dest.kind == "memory":
            reads.update(dest.registers)
            writes.add("MEM")
        elif dest.kind == "other":
            writes.update(dest.registers)

    if mnemonic in FLAGS_READING_MNEMONICS:
        reads.add("FLAGS")

    if mnemonic.startswith(FLAGS_WRITING_PREFIXES) or mnemonic in COMPARE_MNEMONICS:
        writes.add("FLAGS")

    if mnemonic.startswith("set"):
        reads.add("FLAGS")

    return mnemonic, operands, reads, writes


def analyze_bb_instructions(instructions: list[dict[str, Any]]) -> list[InstructionAnalysis]:
    """
    Analyze a BB's instruction stream and assign a conservative critical-path
    depth to each instruction.

    Step 1:
      Convert each instruction into a read/write resource model.
    Step 2:
      Build a simple dependency DAG using RAW plus conservative WAW / memory
      ordering.
    Step 3:
      Record the longest-path depth of each instruction for interval-level
      aggregation later.
    """
    last_writer: dict[str, int] = {}
    last_memory_access: int | None = None
    depths: list[int] = []
    analyses: list[InstructionAnalysis] = []

    for index, instruction in enumerate(instructions):
        mnemonic, operands, reads, writes = resources_for_instruction(instruction["asm"])
        category = classify_instruction_category(mnemonic, operands)
        predecessors: set[int] = set()

        for resource in reads:
            if resource in last_writer:
                predecessors.add(last_writer[resource])
        for resource in writes:
            if resource in last_writer:
                predecessors.add(last_writer[resource])

        touches_memory = "MEM" in reads or "MEM" in writes
        if touches_memory and last_memory_access is not None:
            predecessors.add(last_memory_access)

        cp_depth = 1
        if predecessors:
            cp_depth = 1 + max(depths[pred] for pred in predecessors)
        depths.append(cp_depth)

        for resource in writes:
            last_writer[resource] = index
        if touches_memory:
            last_memory_access = index

        analyses.append(
            InstructionAnalysis(
                mnemonic=mnemonic,
                category=category,
                is_serializing=mnemonic in SERIALIZING_MNEMONICS,
                reads=tuple(sorted(reads)),
                writes=tuple(sorted(writes)),
                cp_depth=cp_depth,
            )
        )

    return analyses


def summarize_bb_static_features(bb_record: dict[str, Any]) -> dict[str, float]:
    instructions = bb_record.get("instructions", [])
    if not instructions:
        return {
            "n_instructions": 0.0,
            "critical_path_length": 0.0,
            "normalized_critical_path": 0.0,
            "ilp_proxy": 0.0,
            "critical_path_ge_3_instruction_count": 0.0,
            "serializing_instruction_count": 0.0,
            "integer_alu_instruction_count": 0.0,
            "mul_div_instruction_count": 0.0,
            "fp_instruction_count": 0.0,
            "simd_instruction_count": 0.0,
            "load_instruction_count": 0.0,
            "store_instruction_count": 0.0,
            "branch_instruction_count": 0.0,
        }

    analyses = analyze_bb_instructions(instructions)
    cp_length = max(analysis.cp_depth for analysis in analyses)
    instruction_count = float(len(analyses))
    category_counts: Counter[str] = Counter(analysis.category for analysis in analyses)

    return {
        "n_instructions": instruction_count,
        "critical_path_length": float(cp_length),
        "normalized_critical_path": safe_ratio(cp_length, instruction_count),
        "ilp_proxy": safe_ratio(instruction_count, cp_length),
        "critical_path_ge_3_instruction_count": float(cp_length if cp_length >= 3 else 0.0),
        "serializing_instruction_count": float(
            sum(1 for analysis in analyses if analysis.is_serializing)
        ),
        "integer_alu_instruction_count": float(category_counts.get("integer_alu", 0)),
        "mul_div_instruction_count": float(category_counts.get("mul_div", 0)),
        "fp_instruction_count": float(category_counts.get("fp", 0)),
        "simd_instruction_count": float(category_counts.get("simd", 0)),
        "load_instruction_count": float(category_counts.get("load", 0)),
        "store_instruction_count": float(category_counts.get("store", 0)),
        "branch_instruction_count": float(category_counts.get("branch", 0)),
    }


def build_interval_bbv_features(interval_bb_catalog: dict[str, Any]) -> dict[str, float]:
    """
    Aggregate BB-derived features for one interval from its interval-local BBV
    catalog.
    """
    bb_records = interval_bb_catalog.get("bb_catalog", {})
    if not bb_records:
        return {}

    total_dynamic_instructions = 0.0
    total_dynamic_cp = 0.0
    total_dynamic_norm_cp = 0.0
    total_dynamic_ilp = 0.0
    max_cp = 0.0
    critical_path_ge_3_dynamic = 0.0
    bb_entropy_weights: list[float] = []
    category_dynamic_counts = {
        "integer_alu": 0.0,
        "mul_div": 0.0,
        "fp": 0.0,
        "simd": 0.0,
        "load": 0.0,
        "store": 0.0,
        "branch": 0.0,
    }
    serializing_dynamic_instructions = 0.0

    for bb_index, record in bb_records.items():
        execution_count = float(record.get("interval_execution_count", 0))
        static_features = summarize_bb_static_features(record)
        dynamic_instruction_count = execution_count * static_features["n_instructions"]

        record["static_analysis"] = static_features
        bb_entropy_weights.append(dynamic_instruction_count)
        total_dynamic_instructions += dynamic_instruction_count
        total_dynamic_cp += dynamic_instruction_count * static_features["critical_path_length"]
        total_dynamic_norm_cp += (
            dynamic_instruction_count * static_features["normalized_critical_path"]
        )
        total_dynamic_ilp += dynamic_instruction_count * static_features["ilp_proxy"]
        max_cp = max(max_cp, static_features["critical_path_length"])
        critical_path_ge_3_dynamic += (
            execution_count * static_features["critical_path_ge_3_instruction_count"]
        )
        serializing_dynamic_instructions += (
            execution_count * static_features["serializing_instruction_count"]
        )

        for category_name in category_dynamic_counts:
            key = f"{category_name}_instruction_count"
            category_dynamic_counts[category_name] += (
                execution_count * static_features[key]
            )

    category_weights = [category_dynamic_counts[name] for name in category_dynamic_counts]
    load_store_dynamic = (
        category_dynamic_counts["load"] + category_dynamic_counts["store"]
    )

    features = {
        "bb_entropy": log_safe_entropy(bb_entropy_weights, normalize_by_categories=False),
        "instruction_category_entropy": log_safe_entropy(
            category_weights,
            normalize_by_categories=True,
        ),
        "mean_cp": safe_ratio(total_dynamic_cp, total_dynamic_instructions),
        "mean_normalized_cp": safe_ratio(
            total_dynamic_norm_cp,
            total_dynamic_instructions,
        ),
        "mean_ilp": safe_ratio(total_dynamic_ilp, total_dynamic_instructions),
        "max_critical_path": max_cp,
        "pct_instructions_in_cp_ge_3": safe_ratio(
            critical_path_ge_3_dynamic,
            total_dynamic_instructions,
        ),
        "pct_serializing_instructions": safe_ratio(
            serializing_dynamic_instructions,
            total_dynamic_instructions,
        ),
        "fraction_integer_alu_instructions": safe_ratio(
            category_dynamic_counts["integer_alu"],
            total_dynamic_instructions,
        ),
        "fraction_mul_div_instructions": safe_ratio(
            category_dynamic_counts["mul_div"],
            total_dynamic_instructions,
        ),
        "fraction_fp_instructions": safe_ratio(
            category_dynamic_counts["fp"],
            total_dynamic_instructions,
        ),
        "fraction_simd_instructions": safe_ratio(
            category_dynamic_counts["simd"],
            total_dynamic_instructions,
        ),
        "fraction_load_instructions": safe_ratio(
            category_dynamic_counts["load"],
            total_dynamic_instructions,
        ),
        "fraction_store_instructions": safe_ratio(
            category_dynamic_counts["store"],
            total_dynamic_instructions,
        ),
        "fraction_branch_instructions": safe_ratio(
            category_dynamic_counts["branch"],
            total_dynamic_instructions,
        ),
        "fraction_lsu_instructions": safe_ratio(
            load_store_dynamic,
            total_dynamic_instructions,
        ),
        "total_dynamic_instructions_from_bbv": total_dynamic_instructions,
    }

    return features


def compute_intent_execution_mismatch(
    bbv_features: dict[str, float],
    energystats_features: dict[str, float],
) -> dict[str, float]:
    return {
        "alu_intent_vs_execution_mismatch": safe_ratio(
            energystats_features.get("avg_alu_duty_cycle", 0.0),
            bbv_features.get("fraction_integer_alu_instructions", 0.0),
        ),
        "lsu_intent_vs_execution_mismatch": safe_ratio(
            energystats_features.get("avg_lsu_duty_cycle", 0.0),
            bbv_features.get("fraction_lsu_instructions", 0.0),
        ),
        "fpu_intent_vs_execution_mismatch": safe_ratio(
            energystats_features.get("avg_fpu_cdb_duty_cycle", 0.0),
            bbv_features.get("fraction_fp_instructions", 0.0),
        ),
    }


def load_interval_thermal_labels(
    die_grid_path: Path,
    maxima_csv_path: Path,
) -> dict[str, float]:
    die_grid_values = parse_die_grid_values(die_grid_path)
    maxima_rows = read_maxima_rows(maxima_csv_path)

    if not die_grid_values:
        raise ValueError(f"No die_grid values found in {die_grid_path}")
    if not maxima_rows:
        raise ValueError(f"No maxima rows found in {maxima_csv_path}")

    severities = [
        hotspot_severity(row["temp_xy"], row["pos_MLTD"])
        for row in maxima_rows
    ]

    peak_temperature_kelvin = max(die_grid_values)
    average_temperature_kelvin = sum(die_grid_values) / len(die_grid_values)

    return {
        "peak_temperature": peak_temperature_kelvin - 273.15,
        "average_temperature": average_temperature_kelvin - 273.15,
        "max_positive_mltd": max(row["pos_MLTD"] for row in maxima_rows),
        "worst_hotspot_severity": max(severities),
    }


def discover_interval_dirs(experiment_output_dir: Path) -> list[Path]:
    return sorted(
        [
            path
            for path in experiment_output_dir.iterdir()
            if path.is_dir() and re.fullmatch(r"interval_\d+", path.name)
        ],
        key=lambda path: int(path.name.split("_", 1)[1]),
    )
