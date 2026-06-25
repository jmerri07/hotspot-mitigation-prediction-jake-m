#!/usr/bin/env python3
"""
make_pow_trace.py

Create the full-run power trace and metadata JSON used as the source for
interval-specific trace generation in Mayfew Phase 3.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


def find_mayfew_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_hotgauge_root() -> Path:
    return find_mayfew_root().parent / "HotGauge"


HOTGAUGE_ROOT = default_hotgauge_root()
sys.path.insert(0, str(HOTGAUGE_ROOT / "HotGauge"))

from HotGauge.configuration import load_block_powers  # type: ignore
from HotGauge.power.traces import JSONFilesPowerTrace  # type: ignore


def fill_metadata_json(
    sniper_output_dir: str,
    instruction_count: int,
    interval_ns: int,
    suite: str,
    metadata_path: Path,
) -> None:
    sniper_path = Path(sniper_output_dir).expanduser().resolve()
    parts = sniper_path.parts

    try:
        frequency = parts[-1]
        tech_node = parts[-2]
        workload = parts[-3]
    except IndexError as exc:
        raise ValueError(
            f"Unexpected sniper_output_dir layout: {sniper_output_dir}"
        ) from exc

    metadata = {
        "region": "start",
        "instruction_count": int(instruction_count),
        "interval_ns": int(interval_ns),
        "suite": suite,
        "workload": workload,
        "tech_node": tech_node,
        "frequency": frequency,
    }

    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with metadata_path.open("w") as handle:
        json.dump(metadata, handle, indent=2)
        handle.write("\n")


@click.command()
@click.option(
    "--sniper-output-dir",
    required=True,
    type=click.Path(file_okay=False, exists=True),
    help="Sniper/McPAT output directory",
)
@click.option(
    "--prefix-for-files",
    required=True,
    type=str,
    help="Base name used for <prefix>_metadata.json and <prefix>_pow_trace.json",
)
@click.option(
    "--instruction-count",
    required=True,
    type=int,
    help="Instruction count used for the Sniper simulation",
)
@click.option(
    "--interval-ns",
    required=True,
    type=int,
    help="Value passed to Sniper as energystats:<N>",
)
@click.option(
    "--suite",
    default="spec2006",
    show_default=True,
    type=str,
    help="Benchmark suite label to store in metadata",
)
@click.option(
    "--metadata-dir",
    default="Metadata",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Directory where metadata JSON files are written",
)
@click.option(
    "--traces-dir",
    default="Traces",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Directory where power trace JSON files are written",
)
def main(
    sniper_output_dir: str,
    prefix_for_files: str,
    instruction_count: int,
    interval_ns: int,
    suite: str,
    metadata_dir: str,
    traces_dir: str,
) -> None:
    base_dir = Path.cwd()
    metadata_path = base_dir / metadata_dir / f"{prefix_for_files}_metadata.json"
    trace_path = base_dir / traces_dir / f"{prefix_for_files}_pow_trace.json"

    trace_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)

    fill_metadata_json(
        sniper_output_dir=sniper_output_dir,
        instruction_count=instruction_count,
        interval_ns=interval_ns,
        suite=suite,
        metadata_path=metadata_path,
    )

    block_powers = load_block_powers(sniper_output_dir)
    workload_trace = JSONFilesPowerTrace(block_powers, float(interval_ns))
    powers = {unit: list(series) for unit, series in workload_trace.powers.items()}

    with trace_path.open("w") as handle:
        json.dump(powers, handle, indent=2)
        handle.write("\n")

    print(f"Wrote metadata: {metadata_path}")
    print(f"Wrote trace:    {trace_path}")


if __name__ == "__main__":
    main()
