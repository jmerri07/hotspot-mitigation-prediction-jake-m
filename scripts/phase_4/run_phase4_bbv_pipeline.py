#!/usr/bin/env python3
"""
run_phase4_bbv_pipeline.py

Phase 4 Step 1 wrapper.

This script recreates the older BBV / SimPoint / BB-sequence workflow while
moving the durable outputs into `Mayfew/Outputs/<experiment-dir-name>`.

Broad execution steps:
1. Run hotblocks profiling and extended-BBV profiling inside the profiler repo.
2. Strip the extended BBV into a SimPoint-compatible BBV.
3. Run fixed-k SimPoint clustering with `new_simpoint.py`.
4. Parse the representative interval ids from `results.simpts`.
5. Choose which intervals should be traced by the BB-sequence plugin.
6. Generate objdump text for the executable.
7. Run the BB-sequence plugin for the chosen intervals only.
8. Move/copy profiler artifacts into the Mayfew output directory.
9. Run the Phase 4 helper scripts from `Mayfew/scripts/phase_4` so the
   objdump and BB-trace outputs are indexed and cataloged in place.
"""

from __future__ import annotations

import argparse
import subprocess
import shutil
import sys
from pathlib import Path

from phase4_common import (
    default_outputs_root,
    default_profiler_dir,
    ensure_directory,
    find_mayfew_root,
    load_representative_intervals,
    run_command,
    sanitize_name,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, help="Path to the executable")
    parser.add_argument(
        "--interval-size",
        required=True,
        type=int,
        help="Instruction interval size used for BBV and BB-sequence tracing",
    )
    parser.add_argument(
        "--k-clusters",
        required=True,
        type=int,
        help="Fixed number of clusters passed to new_simpoint.py",
    )
    parser.add_argument(
        "--bb-file-prefix",
        required=True,
        help="Prefix used for profiler-side BB files",
    )
    parser.add_argument(
        "--trace-intervals",
        default=None,
        help=(
            "Semicolon-separated interval ids to trace with the BB-sequence plugin, "
            "for example '3;7;22'. Defaults to the SimPoint representative intervals."
        ),
    )
    parser.add_argument(
        "--experiment-dir-name",
        required=True,
        help="Mayfew output directory name under Outputs/",
    )
    parser.add_argument(
        "--profiler-dir",
        default=str(default_profiler_dir()),
        help="Path to /data/jake_m/coop-precommit/profilers",
    )
    parser.add_argument(
        "--outputs-root",
        default=str(default_outputs_root()),
        help="Root directory for Mayfew outputs",
    )
    parser.add_argument(
        "--objdump-bin",
        default="objdump",
        help="Objdump binary used to create the executable disassembly text",
    )
    parser.add_argument(
        "--new-simpoint",
        default=str(find_mayfew_root() / "scripts" / "phase_2" / "new_simpoint.py"),
        help="Path to new_simpoint.py",
    )
    parser.add_argument(
        "--run-profile",
        default=None,
        help="Override path to run_profile.sh. Defaults to <profiler-dir>/run_profile.sh",
    )
    parser.add_argument(
        "--strip-bbv",
        default=None,
        help="Override path to strip_bbv.sh. Defaults to <profiler-dir>/scripts/strip_bbv.sh",
    )
    parser.add_argument(
        "--qemu-bin",
        default=None,
        help="Override path to qemu-x86_64. Defaults to <profiler-dir>/qemu/build/qemu-x86_64",
    )
    parser.add_argument(
        "--bb-sequence-plugin",
        default=None,
        help=(
            "Override path to libbb_sequence_intervals.so. Defaults to "
            "<profiler-dir>/qemu/build/contrib/plugins/libbb_sequence_intervals.so"
        ),
    )
    parser.add_argument(
        "workload_args",
        nargs=argparse.REMAINDER,
        help="Optional additional executable arguments supplied after --",
    )
    return parser


def require_path(path: Path, description: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Missing {description}: {path}")
    return path


def move_artifact(src: Path, dest: Path) -> None:
    ensure_directory(dest.parent)
    shutil.copy2(src, dest)
    if src.exists():
        src.unlink()


def parse_interval_list(raw_value: str) -> list[int]:
    """
    Parse a semicolon-separated interval list such as "3;7;22".

    Broad steps:
    1. Split on semicolons because that is the CLI format the user requested.
    2. Trim whitespace and discard any empty fragments.
    3. Convert each remaining token to an integer interval id.
    4. Deduplicate while preserving the user's order.
    """
    interval_ids: list[int] = []
    seen: set[int] = set()
    for piece in raw_value.split(';'):
        token = piece.strip()
        if not token:
            continue
        interval_id = int(token)
        if interval_id not in seen:
            interval_ids.append(interval_id)
            seen.add(interval_id)
    if not interval_ids:
        raise ValueError("--trace-intervals did not contain any interval ids")
    return interval_ids


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    executable = Path(args.executable).expanduser().resolve()
    profiler_dir = Path(args.profiler_dir).expanduser().resolve()
    outputs_root = Path(args.outputs_root).expanduser().resolve()
    output_dir = outputs_root / args.experiment_dir_name
    phase4_dir = Path(__file__).resolve().parent

    run_profile = Path(args.run_profile).expanduser().resolve() if args.run_profile else profiler_dir / "run_profile.sh"
    strip_bbv = Path(args.strip_bbv).expanduser().resolve() if args.strip_bbv else profiler_dir / "scripts" / "strip_bbv.sh"
    new_simpoint = Path(args.new_simpoint).expanduser().resolve()
    qemu_bin = Path(args.qemu_bin).expanduser().resolve() if args.qemu_bin else profiler_dir / "qemu" / "build" / "qemu-x86_64"
    bb_sequence_plugin = (
        Path(args.bb_sequence_plugin).expanduser().resolve()
        if args.bb_sequence_plugin
        else profiler_dir / "qemu" / "build" / "contrib" / "plugins" / "libbb_sequence_intervals.so"
    )

    require_path(executable, "executable")
    require_path(profiler_dir, "profiler directory")
    require_path(run_profile, "run_profile.sh")
    require_path(strip_bbv, "strip_bbv.sh")
    require_path(new_simpoint, "new_simpoint.py")
    require_path(qemu_bin, "qemu-x86_64")
    require_path(bb_sequence_plugin, "libbb_sequence_intervals.so")

    ensure_directory(output_dir)

    # Step 1: build unique profiler-side temporary names so that the workflow
    # can run in-place without trampling another invocation's files.
    temp_token = f"{sanitize_name(args.experiment_dir_name)}_{sanitize_name(args.bb_file_prefix)}"
    hotblocks_temp = profiler_dir / f"{temp_token}_hotblocks"
    extended_bbv_temp_prefix = profiler_dir / f"{temp_token}_bbv"
    extended_bbv_temp = profiler_dir / f"{temp_token}_bbv.0.bb"
    stripped_bbv_temp = profiler_dir / f"{temp_token}_bbv_simpoint.0.bb"
    results_temp_prefix = profiler_dir / f"{temp_token}_results"
    bb_seq_temp_prefix = profiler_dir / f"{temp_token}_bb_seq"
    bb_seq_temp = profiler_dir / f"{temp_token}_bb_seq.0.csv"

    workload_args = args.workload_args[:]
    if workload_args and workload_args[0] == "--":
        workload_args = workload_args[1:]
    workload = [str(executable), *workload_args]

    # Step 2: run the legacy profiler-side commands in the profiler repo.
    run_command(
        [
            str(run_profile),
            "--option",
            "0",
            "--out",
            str(hotblocks_temp),
            "--interval",
            str(args.interval_size),
            "--",
            *workload,
        ],
        cwd=profiler_dir,
    )
    run_command(
        [
            str(run_profile),
            "--option",
            "2",
            "--out",
            str(extended_bbv_temp_prefix),
            "--interval",
            str(args.interval_size),
            "--",
            *workload,
        ],
        cwd=profiler_dir,
    )
    run_command(
        [
            str(strip_bbv),
            str(extended_bbv_temp),
            str(stripped_bbv_temp),
        ],
        cwd=profiler_dir,
    )
    run_command(
        [
            sys.executable,
            str(new_simpoint),
            "-i",
            str(stripped_bbv_temp),
            "-o",
            str(results_temp_prefix),
            "-k",
            str(args.k_clusters),
        ],
        cwd=profiler_dir,
    )

    # Step 3: parse the SimPoint representatives first because they remain
    # part of the durable output even when the user chooses a different BB
    # tracing subset.
    representative_intervals = load_representative_intervals(
        profiler_dir / f"{temp_token}_results.simpts"
    )
    if not representative_intervals:
        raise ValueError("No representative intervals were found in the SimPoint output")

    # Step 4: choose which intervals the dynamic BB-sequence trace should
    # capture. By default we trace the SimPoint representatives, but the user
    # can override that with an explicit semicolon-separated interval list.
    if args.trace_intervals:
        traced_intervals = parse_interval_list(args.trace_intervals)
    else:
        traced_intervals = representative_intervals

    interval_option_parts = [f"intervals={interval_id}" for interval_id in traced_intervals]

    # Step 5: create the objdump text file directly in Mayfew because all
    # later helper scripts read it from there.
    objdump_text_path = output_dir / f"{executable.name}.objdump.txt"
    with objdump_text_path.open("w", encoding="utf-8") as handle:
        completed = shutil.which(args.objdump_bin)
        if completed is None:
            raise FileNotFoundError(f"Could not locate objdump binary: {args.objdump_bin}")
        subprocess.run(
            [completed, "-d", str(executable)],
            cwd=str(profiler_dir),
            stdout=handle,
            check=True,
        )

    plugin_arg = ",".join(
        [
            str(bb_sequence_plugin),
            f"outfile={bb_seq_temp_prefix}",
            f"interval={args.interval_size}",
            *interval_option_parts,
            "include_first=false",
        ]
    )

    run_command(
        [
            str(qemu_bin),
            "-plugin",
            plugin_arg,
            *workload,
        ],
        cwd=profiler_dir,
    )

    # Step 6: move the requested profiler outputs into the Mayfew experiment
    # directory, using stable final names there.
    move_artifact(hotblocks_temp, output_dir / f"{args.bb_file_prefix}_hotblocks.txt")
    move_artifact(extended_bbv_temp, output_dir / f"{args.bb_file_prefix}.0.bb")
    move_artifact(stripped_bbv_temp, output_dir / f"{args.bb_file_prefix}_simpoint.0.bb")
    move_artifact(profiler_dir / f"{temp_token}_results.simpts", output_dir / "results.simpts")
    move_artifact(profiler_dir / f"{temp_token}_results.weights", output_dir / "results.weights")
    move_artifact(profiler_dir / f"{temp_token}_results.labels", output_dir / "results.labels")
    move_artifact(
        profiler_dir / f"{temp_token}_results.cluster_members",
        output_dir / "results.cluster_members",
    )
    move_artifact(bb_seq_temp, output_dir / "bb_seq.0.csv")

    # Step 7: run the Mayfew-side helper scripts so the trace data is indexed
    # and cataloged entirely inside the experiment output directory.
    run_command(
        [
            sys.executable,
            str(phase4_dir / "parse_objdump.py"),
            "--objdump",
            str(objdump_text_path),
            "--out-json",
            str(output_dir / "objdump_index.json"),
        ],
        cwd=phase4_dir,
    )
    run_command(
        [
            sys.executable,
            str(phase4_dir / "filter_trace_csv.py"),
            "--csv",
            str(output_dir / "bb_seq.0.csv"),
            "--objdump",
            str(objdump_text_path),
            "--out-csv",
            str(output_dir / "filtered_bb_seq.csv"),
            "--summary-json",
            str(output_dir / "filtered_trace_summary.json"),
        ],
        cwd=phase4_dir,
    )
    run_command(
        [
            sys.executable,
            str(phase4_dir / "extract_bbs.py"),
            "--csv",
            str(output_dir / "filtered_bb_seq.csv"),
            "--objdump-json",
            str(output_dir / "objdump_index.json"),
            "--out-json",
            str(output_dir / "bb_catalog.json"),
        ],
        cwd=phase4_dir,
    )

    print(f"[phase4] Phase 4 Step 1 outputs written to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
