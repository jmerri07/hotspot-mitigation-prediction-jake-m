#!/usr/bin/env python3
"""
run_phase1_sniper_mcpat.py

Wrapper for the Phase 1 workflow:
1. Run Sniper with energystats enabled.
2. Run the HotGauge McPAT helper script on the resulting output tree.

This script is designed to live in:
    Mayfew/scripts/phase_1

It keeps the actual Sniper and McPAT outputs in the same locations used by the
old workflow. The goal here is only to centralize the launch logic in Mayfew.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


def find_mayfew_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_hotgauge_root() -> Path:
    return find_mayfew_root().parent / "HotGauge"


def run_and_tee(command: list[str], cwd: Path, log_path: Path) -> None:
    """
    Run a command, stream stdout/stderr to the terminal, and also save it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print("[phase1] Running command:")
    print("  " + " ".join(shlex.quote(part) for part in command))
    print(f"  cwd={cwd}")
    print(f"  log={log_path}")

    with log_path.open("w") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            log_handle.write(line)

        return_code = process.wait()

    if return_code != 0:
        raise RuntimeError(
            f"Command failed with exit code {return_code}: {' '.join(command)}"
        )


def run_simple(command: list[str], cwd: Path) -> None:
    print("[phase1] Running command:")
    print("  " + " ".join(shlex.quote(part) for part in command))
    print(f"  cwd={cwd}")
    subprocess.run(command, cwd=str(cwd), check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Sniper + McPAT Phase 1 workflow from Mayfew."
    )
    parser.add_argument(
        "--hotgauge-root",
        default=str(default_hotgauge_root()),
        help="Path to the HotGauge repo",
    )
    parser.add_argument(
        "--sniper-dir",
        default=None,
        help="Path to the Sniper directory. Defaults to <hotgauge-root>/snipersim",
    )
    parser.add_argument(
        "--sniper-config",
        default=None,
        help="Sniper config file. Defaults to <hotgauge-root>/examples/40_skylake_14nm.cfg",
    )
    parser.add_argument(
        "--sniper-output-dir",
        required=True,
        help=(
            "Exact Sniper output directory to create, for example "
            "/data/jake_m/HotGauge/snipersim/output/libq_intervals/14nm/4.0GHz"
        ),
    )
    parser.add_argument(
        "--num-cores",
        type=int,
        default=1,
        help="Value passed to Sniper with -n",
    )
    parser.add_argument(
        "--energystats-ns",
        type=int,
        default=40_000,
        help="Value passed to Sniper as energystats:<N>",
    )
    parser.add_argument(
        "--stop-icount",
        type=int,
        default=2_000_000_000,
        help="Value passed to Sniper as stop-by-icount:<N>",
    )
    parser.add_argument(
        "--sde-arch",
        default="tgl",
        help="Value passed to Sniper as --sde-arch=<arch>",
    )
    parser.add_argument(
        "--mcpat-script",
        default=None,
        help=(
            "Path to the McPAT helper script. Defaults to "
            "<hotgauge-root>/scripts/14_perf_sims_to_7_10_14_power_sims.sh"
        ),
    )
    parser.add_argument(
        "--mcpat-input-root",
        default=None,
        help=(
            "Directory passed as the first argument to the McPAT helper. "
            "Defaults to the experiment directory under snipersim/output, "
            "for example .../output/libq_intervals"
        ),
    )
    parser.add_argument(
        "--mcpat-workers",
        type=int,
        default=1,
        help="Second argument passed to the McPAT helper script",
    )
    parser.add_argument(
        "--log-file",
        default="phase1_sniper_run.log",
        help="Path to the Sniper log file",
    )
    parser.add_argument(
        "--skip-mcpat",
        action="store_true",
        help="Run Sniper only and skip the McPAT conversion step",
    )
    parser.add_argument(
        "workload_cmd",
        nargs=argparse.REMAINDER,
        help="Pass the workload command after a literal --",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.workload_cmd and args.workload_cmd[0] == "--":
        workload_cmd = args.workload_cmd[1:]
    else:
        workload_cmd = args.workload_cmd

    if not workload_cmd:
        print(
            "ERROR: You must provide the workload command after --",
            file=sys.stderr,
        )
        return 1

    hotgauge_root = Path(args.hotgauge_root).expanduser().resolve()
    sniper_dir = (
        Path(args.sniper_dir).expanduser().resolve()
        if args.sniper_dir
        else (hotgauge_root / "snipersim")
    )
    sniper_config = (
        Path(args.sniper_config).expanduser().resolve()
        if args.sniper_config
        else (hotgauge_root / "examples" / "40_skylake_14nm.cfg")
    )
    sniper_output_dir = Path(args.sniper_output_dir).expanduser().resolve()
    log_path = Path(args.log_file).expanduser().resolve()

    mcpat_script = (
        Path(args.mcpat_script).expanduser().resolve()
        if args.mcpat_script
        else (
            hotgauge_root
            / "scripts"
            / "14_perf_sims_to_7_10_14_power_sims.sh"
        )
    )

    if args.mcpat_input_root:
        mcpat_input_root = Path(args.mcpat_input_root).expanduser().resolve()
    else:
        try:
            mcpat_input_root = sniper_output_dir.parents[1]
        except IndexError as exc:
            raise RuntimeError(
                f"Could not infer McPAT input root from {sniper_output_dir}"
            ) from exc

    sniper_command = [
        "./run-sniper",
        "-c",
        str(sniper_config),
        "-n",
        str(args.num_cores),
        "-d",
        str(sniper_output_dir),
        "-s",
        f"energystats:{args.energystats_ns}",
        "-s",
        f"stop-by-icount:{args.stop_icount}",
        f"--sde-arch={args.sde_arch}",
        "--",
        *workload_cmd,
    ]

    run_and_tee(sniper_command, cwd=sniper_dir, log_path=log_path)

    if args.skip_mcpat:
        print("[phase1] Skipping McPAT because --skip-mcpat was requested.")
        return 0

    mcpat_command = [
        str(mcpat_script),
        str(mcpat_input_root),
        str(args.mcpat_workers),
    ]
    run_simple(mcpat_command, cwd=mcpat_script.parent)

    print("[phase1] Phase 1 workflow finished successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
