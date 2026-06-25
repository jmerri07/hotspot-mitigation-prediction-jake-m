#!/usr/bin/env python3
"""
run_hotgauge_interval_postprocess.py

Phase 4 helper that reruns the HotGauge maxima and visualization steps for
every interval-local metadata/trace pair in one HotGauge experiment directory.

Broad execution steps:
1. Discover every interval metadata file in the HotGauge `Metadata` directory.
2. Confirm that each metadata file has a matching interval trace file in
   `Traces` so we only process complete interval pairs.
3. Run all `compute_local_maxima_stats.py` commands in parallel and wait for
   the entire maxima stage to finish.
4. Run `diagram_visualize.py` serially only for intervals whose maxima
   stage succeeded, using the fixed grid arguments that produce the preferred
   video layout.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import shlex
import subprocess
from pathlib import Path

from phase4_common import parse_metadata_interval_filename


GRID_ARGS = [
    "--grid-args",
    "--heatmap_width 80.0",
    "--grid-args",
    "--scale_width 8.0",
    "--grid-args",
    "--min_t 40",
    "--grid-args",
    "--max_t 115",
]
CORE_NAME = "core_0"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hotgauge-experiment-dir",
        required=True,
        help="Path to the HotGauge experiment directory that contains Metadata, Traces, and analysis_scripts",
    )
    parser.add_argument(
        "--core-name",
        default=CORE_NAME,
        help="Core name passed through to the HotGauge post-processing scripts",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=2,
        help="Maximum number of post-processing commands to run at once in each stage",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print commands before launching them; successful command output remains suppressed",
    )
    return parser


def require_path(path: Path, description: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Missing {description}: {path}")
    return path


def run_command_capture(command: list[str], cwd: Path) -> tuple[int, str, str]:
    """
    Run one subprocess while capturing stdout and stderr for failure reporting.

    Broad steps:
    1. Launch the subprocess without streaming output during successful runs.
    2. Capture stdout and stderr so failures can be reported clearly.
    3. Return the exit code and captured text to the caller.
    """
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def discover_interval_pairs(metadata_dir: Path, traces_dir: Path) -> list[tuple[int, str]]:
    """
    Return the interval id plus metadata filename for each complete interval pair.

    Step 1:
      Scan `Metadata/` for interval-local metadata files.
    Step 2:
      Extract the interval number from each filename.
    Step 3:
      Reconstruct the expected trace filename and keep only intervals whose
      trace file is present in `Traces/`.
    """
    pairs: list[tuple[int, str]] = []

    for metadata_path in sorted(metadata_dir.glob("*_metadata_interval_*.json")):
        interval_id = parse_metadata_interval_filename(metadata_path)
        if interval_id is None:
            continue

        trace_name = metadata_path.name.replace("_metadata_interval_", "_pow_trace_interval_")
        trace_path = traces_dir / trace_name
        if not trace_path.is_file():
            print(
                "WARNING: Skipping interval "
                f"{interval_id} because the matching trace file is missing: {trace_path}"
            )
            continue

        pairs.append((interval_id, metadata_path.name))

    return pairs


def maybe_log_queued_command(label: str, command: list[str], cwd: Path, verbose: bool) -> None:
    if not verbose:
        return
    print(f"[run_hotgauge_interval_postprocess] Queueing {label} command:")
    print("  " + " ".join(shlex.quote(part) for part in command))
    print(f"  cwd={cwd}")


def report_failure(label: str, interval_id: int, command: list[str], stdout_text: str, stderr_text: str) -> None:
    print(f"ERROR: {label} failed for interval {interval_id}.")
    print("Command:")
    print("  " + " ".join(shlex.quote(part) for part in command))
    if stdout_text.strip():
        print("stdout:")
        print(stdout_text.rstrip())
    if stderr_text.strip():
        print("stderr:")
        print(stderr_text.rstrip())


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    hotgauge_experiment_dir = Path(args.hotgauge_experiment_dir).expanduser().resolve()
    metadata_dir = require_path(hotgauge_experiment_dir / "Metadata", "HotGauge Metadata directory")
    traces_dir = require_path(hotgauge_experiment_dir / "Traces", "HotGauge Traces directory")
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

    if args.max_parallel < 1:
        raise ValueError("--max-parallel must be at least 1")

    interval_pairs = discover_interval_pairs(metadata_dir, traces_dir)
    if not interval_pairs:
        print("WARNING: No complete metadata/trace interval pairs were found.")
        return 0

    failures: list[int] = []
    diagram_ready_pairs: list[tuple[int, str]] = []

    # Step 3: run every maxima command in parallel and wait for the entire
    # maxima stage to finish before any visualization begins.
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_parallel) as executor:
        future_to_context = {}
        for interval_id, metadata_name in interval_pairs:
            command = [
                "python3",
                str(compute_script),
                "--metadata-file-name",
                metadata_name,
                "--core-name",
                args.core_name,
            ]
            maybe_log_queued_command("maxima", command, analysis_scripts_dir, args.verbose)
            future = executor.submit(run_command_capture, command, analysis_scripts_dir)
            future_to_context[future] = (interval_id, metadata_name, command)

        for future in concurrent.futures.as_completed(future_to_context):
            interval_id, metadata_name, command = future_to_context[future]
            rc, stdout_text, stderr_text = future.result()
            if rc != 0:
                report_failure(
                    "Local maxima analysis",
                    interval_id,
                    command,
                    stdout_text,
                    stderr_text,
                )
                failures.append(interval_id)
                continue
            diagram_ready_pairs.append((interval_id, metadata_name))

    # Step 4: only after the entire maxima stage completes do we launch the
    # visualization stage serially for the intervals that passed stage 3.
    for interval_id, metadata_name in diagram_ready_pairs:
        command = [
            "python3",
            str(diagram_script),
            "--metadata-file-name",
            metadata_name,
            "--core-name",
            args.core_name,
            *GRID_ARGS,
        ]
        maybe_log_queued_command("visualization", command, analysis_scripts_dir, args.verbose)
        rc, stdout_text, stderr_text = run_command_capture(command, analysis_scripts_dir)
        if rc != 0:
            report_failure(
                "Visualization",
                interval_id,
                command,
                stdout_text,
                stderr_text,
            )
            failures.append(interval_id)
            continue

        print(
            f"[run_hotgauge_interval_postprocess] Finished interval {interval_id} in "
            f"{hotgauge_experiment_dir}"
        )

    if failures:
        print(
            "WARNING: Some intervals failed during maxima or visualization post-processing: "
            + ", ".join(str(interval_id) for interval_id in sorted(set(failures)))
        )
        return 1

    print("[run_hotgauge_interval_postprocess] All discovered intervals completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
