#!/usr/bin/env python3
"""
run_interval_power_and_thermal.py

Phase 3 driver that selects intervals from a Mayfew analysis JSON, but runs the
actual HotGauge workflow inside an existing HotGauge experiment directory.

What stays in Mayfew:
1. The chosen analysis JSON
2. The generated intervals.csv

What stays in HotGauge:
1. Full-run metadata and trace files
2. Interval-specific metadata and trace files
3. 3D-ICE simulation runs
4. Local maxima analysis
5. Temperature visualization
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path


INTERVAL_NS = "40000"
SUITE = "spec2006"
CORE_NAME = "core_0"


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def find_mayfew_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_outputs_root() -> Path:
    mayfew_root = find_mayfew_root()
    uppercase = mayfew_root / "Outputs"
    lowercase = mayfew_root / "outputs"
    if uppercase.exists():
        return uppercase
    if lowercase.exists():
        return lowercase
    return uppercase


def default_hotgauge_root() -> Path:
    return find_mayfew_root().parent / "HotGauge"


def default_tstack_path() -> Path:
    return (
        default_hotgauge_root()
        / "examples"
        / "template"
        / "warmups"
        / "skylake_HS483"
        / "idle_00"
        / "skylake7nm_7core_3_3D-ICE_template.flp"
        / "final.tstack"
    )


def run_cmd(cmd: list[str], cwd: Path, verbose: bool = True, check: bool = True) -> int:
    if verbose:
        print("[run_interval_power_and_thermal] Running command:")
        print("  " + " ".join(shlex.quote(part) for part in cmd))
        print(f"  cwd={cwd}")

    completed = subprocess.run(
        cmd,
        cwd=str(cwd),
        stdout=None if verbose else subprocess.DEVNULL,
        stderr=None if verbose else subprocess.DEVNULL,
        check=False,
    )

    if check and completed.returncode != 0:
        fail(f"Command failed with exit code {completed.returncode}: {' '.join(cmd)}")

    return completed.returncode


def get_matching_14nm_sim_out(sniper_output_dir_7nm: Path) -> Path:
    parts = list(sniper_output_dir_7nm.parts)
    if "7nm" not in parts:
        fail(f"Expected '7nm' in sniper output dir path: {sniper_output_dir_7nm}")

    index = parts.index("7nm")
    parts[index] = "14nm"
    sim_out = Path(*parts) / "sim.out"

    if not sim_out.is_file():
        fail(f"Could not find matching 14nm sim.out: {sim_out}")

    return sim_out


def parse_instruction_count(sim_out_path: Path) -> int:
    lines = sim_out_path.read_text(errors="replace").splitlines()
    for line in lines[:10]:
        numbers = re.findall(r"\b\d+\b", line)
        big_numbers = [int(value) for value in numbers if int(value) > 1000]
        if big_numbers:
            return big_numbers[0]
    fail(f"Could not parse instruction count from {sim_out_path}")
    return 0


def load_analysis_json(path: Path) -> dict:
    if not path.is_file():
        fail(f"Missing analysis JSON: {path}")
    with path.open("r") as handle:
        return json.load(handle)


def collect_target_intervals(analysis: dict, selection_mode: str) -> list[int]:
    intervals: list[int] = []

    if selection_mode in {"representatives", "both"}:
        intervals.extend(
            int(entry["chosen_interval"])
            for entry in analysis.get("representatives", [])
        )

    if selection_mode in {"non_representatives", "both"}:
        intervals.extend(
            int(entry["interval_index"])
            for entry in analysis.get("non_representatives", [])
        )

    deduped: list[int] = []
    seen: set[int] = set()
    for interval in intervals:
        if interval not in seen:
            deduped.append(interval)
            seen.add(interval)
    return deduped


def load_available_intervals(interval_csv_path: Path) -> set[int]:
    with interval_csv_path.open("r", newline="") as handle:
        reader = csv.DictReader(handle)
        return {int(row["interval_index"]) for row in reader}


def build_interval_arg(intervals: list[int]) -> str:
    return ";".join(str(value) for value in intervals)


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def require_path(path: Path, description: str) -> Path:
    if not path.exists():
        fail(f"Missing {description}: {path}")
    return path


def run_interval_pipeline(
    interval: int,
    sim_from_warmup_script: Path,
    compute_script: Path,
    diagram_script: Path,
    hotgauge_experiment_dir: Path,
    analysis_scripts_dir: Path,
    tstack_path: Path,
    flp_template: str,
    metadata_name: str,
    trace_name: str,
    verbose: bool,
) -> tuple[int, bool]:
    """
    Run the per-interval Phase 3 workflow.

    Broad steps:
    1. Launch the HotGauge thermal simulation for the chosen interval.
    2. If the simulation succeeds, compute the interval's local-maxima statistics.
    3. If the maxima pass succeeds, build the interval visualization outputs.
    """

    interval_metadata_name = f"{Path(metadata_name).stem}_interval_{interval}.json"
    interval_trace_name = f"{Path(trace_name).stem}_interval_{interval}.json"

    rc = run_cmd(
        [
            sys.executable,
            str(sim_from_warmup_script),
            "--tstack-path",
            str(tstack_path),
            "--flp-template",
            flp_template,
            "--trace-file",
            str(hotgauge_experiment_dir / "Traces" / interval_trace_name),
            "--metadata-file",
            str(hotgauge_experiment_dir / "Metadata" / interval_metadata_name),
            "--core-mapping",
            "0",
        ],
        cwd=hotgauge_experiment_dir,
        verbose=verbose,
        check=False,
    )
    if rc != 0:
        print(
            f"WARNING: Thermal simulation failed for interval {interval}. Skipping post-processing.",
            file=sys.stderr,
        )
        return interval, False

    rc = run_cmd(
        [
            sys.executable,
            str(compute_script),
            "--metadata-file-name",
            interval_metadata_name,
            "--core-name",
            CORE_NAME,
        ],
        cwd=analysis_scripts_dir,
        verbose=verbose,
        check=False,
    )
    if rc != 0:
        print(
            f"WARNING: Local maxima analysis failed for interval {interval}.",
            file=sys.stderr,
        )
        return interval, False

    rc = run_cmd(
        [
            sys.executable,
            str(diagram_script),
            "--metadata-file-name",
            interval_metadata_name,
            "--core-name",
            CORE_NAME,
            "--grid-args",
            "--heatmap_width 80.0",
            "--grid-args",
            "--scale_width 8.0",
            "--grid-args",
            "--min_t 40",
            "--grid-args",
            "--max_t 115",
        ],
        cwd=analysis_scripts_dir,
        verbose=verbose,
        check=False,
    )
    if rc != 0:
        print(
            f"WARNING: Visualization failed for interval {interval}.",
            file=sys.stderr,
        )
        return interval, False

    print(
        f"[run_interval_power_and_thermal] Finished interval {interval} in {hotgauge_experiment_dir}"
    )
    return interval, True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Phase 3 using a HotGauge experiment directory and a Mayfew analysis JSON."
    )
    parser.add_argument(
        "--analysis-json",
        required=True,
        help="Path to the selected Mayfew analysis JSON file",
    )
    parser.add_argument(
        "--sniper-output-dir",
        required=True,
        help="Example: /data/jake_m/HotGauge/snipersim/output/libq_intervals/7nm/4.0GHz",
    )
    parser.add_argument(
        "--hotgauge-experiment-dir",
        required=True,
        help="Example: /data/jake_m/HotGauge/examples/libq_intervals",
    )
    parser.add_argument(
        "--selection-mode",
        choices=["representatives", "non_representatives", "both"],
        default="representatives",
        help="Which intervals from the analysis JSON should be simulated",
    )
    parser.add_argument(
        "--outputs-root",
        default=str(default_outputs_root()),
        help="Root Mayfew outputs directory",
    )
    parser.add_argument(
        "--tstack-path",
        default=str(default_tstack_path()),
        help="Path to the thermal warmup tstack file",
    )
    parser.add_argument(
        "--flp-template",
        default="skylake7nm_7core_3_3D-ICE_template.flp",
        help="FLP template argument passed through to HotGauge sim_from_warmup.py",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=2,
        help="Maximum number of interval-level sim_from_warmup.py commands to run at once",
    )
    parser.add_argument(
        "--suite",
        default=SUITE,
        help="Suite label stored in generated metadata",
    )
    parser.add_argument(
        "--interval-ns",
        default=INTERVAL_NS,
        help="Interval size in ns used to build the power trace metadata",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show command output while the workflow runs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    analysis_json_path = Path(args.analysis_json).expanduser().resolve()
    sniper_output_dir = Path(args.sniper_output_dir).expanduser().resolve()
    hotgauge_experiment_dir = Path(args.hotgauge_experiment_dir).expanduser().resolve()
    outputs_root = Path(args.outputs_root).expanduser().resolve()
    tstack_path = Path(args.tstack_path).expanduser().resolve()

    if not sniper_output_dir.is_dir():
        fail(f"--sniper-output-dir is not a directory: {sniper_output_dir}")
    if not hotgauge_experiment_dir.is_dir():
        fail(f"--hotgauge-experiment-dir is not a directory: {hotgauge_experiment_dir}")
    if not tstack_path.exists():
        fail(f"--tstack-path does not exist: {tstack_path}")
    if args.max_parallel < 1:
        fail("--max-parallel must be at least 1")

    analysis = load_analysis_json(analysis_json_path)
    executable_output_dir = analysis_json_path.parent

    if executable_output_dir.parent != outputs_root:
        print(
            "WARNING: The analysis JSON is not directly under the configured outputs root. "
            f"Using its parent directory anyway: {executable_output_dir}",
            file=sys.stderr,
        )

    experiment_name = hotgauge_experiment_dir.name
    base_prefix = experiment_name.removesuffix("_intervals")
    metadata_name = f"{base_prefix}_metadata.json"
    trace_name = f"{base_prefix}_pow_trace.json"

    make_pow_trace_script = require_path(
        hotgauge_experiment_dir / "make_pow_trace.py",
        "HotGauge make_pow_trace.py",
    )
    group_script = require_path(
        hotgauge_experiment_dir / "group_energystats_by_interval.py",
        "HotGauge group_energystats_by_interval.py",
    )
    make_selected_script = require_path(
        hotgauge_experiment_dir / "make_selected_interval_traces.py",
        "HotGauge make_selected_interval_traces.py",
    )
    sim_from_warmup_script = require_path(
        hotgauge_experiment_dir / "sim_from_warmup.py",
        "HotGauge sim_from_warmup.py",
    )
    analysis_scripts_dir = require_path(
        hotgauge_experiment_dir / "analysis_scripts",
        "HotGauge analysis_scripts directory",
    )
    compute_script = require_path(
        analysis_scripts_dir / "compute_local_maxima_stats.py",
        "HotGauge compute_local_maxima_stats.py",
    )
    diagram_script = require_path(
        analysis_scripts_dir / "diagram_visualize.py",
        "HotGauge diagram_visualize.py",
    )

    interval_size = int(analysis["interval_size"])
    if interval_size % 1_000_000 != 0:
        fail(f"Expected interval_size divisible by 1,000,000, got {interval_size}")
    simplified_interval = interval_size // 1_000_000

    selected_intervals = collect_target_intervals(analysis, args.selection_mode)
    if not selected_intervals:
        print("WARNING: No intervals were selected from the analysis JSON.", file=sys.stderr)
        return 0

    sim_out_14nm = get_matching_14nm_sim_out(sniper_output_dir)
    instruction_count = parse_instruction_count(sim_out_14nm)

    ensure_directory(executable_output_dir)
    ensure_directory(hotgauge_experiment_dir / "Metadata")
    ensure_directory(hotgauge_experiment_dir / "Traces")

    intervals_csv_path = executable_output_dir / "intervals.csv"

    run_cmd(
        [
            sys.executable,
            str(make_pow_trace_script),
            "--sniper-output-dir",
            str(sniper_output_dir),
            "--prefix-for-files",
            base_prefix,
            "--instruction-count",
            str(instruction_count),
            "--interval-ns",
            str(args.interval_ns),
            "--suite",
            args.suite,
        ],
        cwd=hotgauge_experiment_dir,
        verbose=args.verbose,
    )

    run_cmd(
        [
            sys.executable,
            str(group_script),
            str(sniper_output_dir),
            str(simplified_interval),
            "--csv",
            str(intervals_csv_path),
        ],
        cwd=hotgauge_experiment_dir,
        verbose=args.verbose,
    )

    available_intervals = load_available_intervals(intervals_csv_path)
    runnable_intervals = [interval for interval in selected_intervals if interval in available_intervals]
    missing_intervals = [interval for interval in selected_intervals if interval not in available_intervals]

    for interval in missing_intervals:
        print(
            f"WARNING: Could not find sniper statistics for interval {interval}. Skipping it.",
            file=sys.stderr,
        )

    if not runnable_intervals:
        print("WARNING: No selected intervals had matching sniper statistics.", file=sys.stderr)
        return 0

    print(
        f"[run_interval_power_and_thermal] Selected intervals for {args.selection_mode}: "
        f"{build_interval_arg(runnable_intervals)}"
    )

    run_cmd(
        [
            sys.executable,
            str(make_selected_script),
            "--interval-csv",
            str(intervals_csv_path),
            "--intervals",
            build_interval_arg(runnable_intervals),
            "--og-metadata-file",
            str(hotgauge_experiment_dir / "Metadata" / metadata_name),
            "--og-trace-file",
            str(hotgauge_experiment_dir / "Traces" / trace_name),
            "--metadata-dir",
            str(hotgauge_experiment_dir / "Metadata"),
            "--traces-dir",
            str(hotgauge_experiment_dir / "Traces"),
        ],
        cwd=hotgauge_experiment_dir,
        verbose=args.verbose,
    )

    failures: list[int] = []

    # Broad step 5: run the interval-specific thermal workflows with a bounded
    # worker pool. Each worker executes one interval end-to-end, and the pool
    # size keeps the number of simultaneous sim_from_warmup launches under the
    # caller's chosen cap.
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_parallel) as executor:
        future_to_interval = {
            executor.submit(
                run_interval_pipeline,
                interval,
                sim_from_warmup_script,
                compute_script,
                diagram_script,
                hotgauge_experiment_dir,
                analysis_scripts_dir,
                tstack_path,
                args.flp_template,
                metadata_name,
                trace_name,
                args.verbose,
            ): interval
            for interval in runnable_intervals
        }

        for future in concurrent.futures.as_completed(future_to_interval):
            interval, succeeded = future.result()
            if not succeeded:
                failures.append(interval)

    if failures:
        print(
            "WARNING: Some intervals failed during simulation or post-processing: "
            + ", ".join(str(interval) for interval in failures),
            file=sys.stderr,
        )
        return 1

    print("[run_interval_power_and_thermal] All selected intervals completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
