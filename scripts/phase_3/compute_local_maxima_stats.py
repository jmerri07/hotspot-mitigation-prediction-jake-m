#!/usr/bin/env python3
"""
compute_local_maxima_stats.py

Run HotGauge local maxima analysis for one Mayfew interval run directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click


def find_mayfew_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_hotgauge_root() -> Path:
    return find_mayfew_root().parent / "HotGauge"


HOTGAUGE_ROOT = default_hotgauge_root()
HG_PACKAGE_ROOT = HOTGAUGE_ROOT / "HotGauge"
PLT_CMD = [sys.executable, "-m", "HotGauge.thermal.analysis", "local_max_stats"]


def resolve_run_dir(run_dir: str | None) -> Path:
    if run_dir is not None:
        return Path(run_dir).expanduser().resolve()
    return Path.cwd().resolve()


@click.command()
@click.option("--metadata-file-name", required=True, type=str, help="For example: metadata.json")
@click.option("--core-name", required=True, type=str, help="For example: core_0")
@click.option(
    "--run-dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Interval run directory. Defaults to the current working directory.",
)
def main(metadata_file_name: str, core_name: str, run_dir: str | None) -> int:
    run_path = resolve_run_dir(run_dir)
    metadata_path = run_path / "Metadata" / metadata_file_name

    if not metadata_path.exists():
        raise click.ClickException(f"Metadata file not found: {metadata_path}")

    with metadata_path.open("r") as handle:
        metadata = json.load(handle)

    interval_ns = str(metadata["interval_ns"])
    workload = str(metadata["workload"])
    frequency = str(metadata["frequency"])
    tech_node = str(metadata["tech_node"])

    sim_dir = (
        run_path
        / "outputs"
        / "sims"
        / interval_ns
        / workload
        / tech_node
        / frequency
        / core_name
        / "idle_00"
    )

    if not sim_dir.exists():
        raise click.ClickException(f"Simulation directory not found: {sim_dir}")

    print(f"Using SIM_DIR: {sim_dir}")
    print("Starting local maxima analysis...")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(HG_PACKAGE_ROOT) + (
        ":" + env["PYTHONPATH"] if "PYTHONPATH" in env else ""
    )

    cmd_2d = PLT_CMD + [
        "die_grid.temps",
        "20",
        "-o",
        "die_grid.temps.2dmaxima",
        "-o",
        "die_grid.temps.2dmaxima.pkl",
        "-o",
        "die_grid.temps.2dmaxima.csv",
    ]
    cmd_1d = PLT_CMD + [
        "die_grid.temps",
        "20",
        "-o",
        "die_grid.temps.1dmaxima",
        "--in_either_dimension",
    ]

    print("Running 2D maxima command:")
    print("  " + " ".join(cmd_2d))
    subprocess.run(cmd_2d, check=True, cwd=str(sim_dir), env=env)

    print("Running 1D maxima command:")
    print("  " + " ".join(cmd_1d))
    subprocess.run(cmd_1d, check=True, cwd=str(sim_dir), env=env)

    print("Analysis complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
