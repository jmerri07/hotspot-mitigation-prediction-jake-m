#!/usr/bin/env python3
"""
run_prefix_sweep.py

Phase 2 driver for the Mayfew prefix sweep workflow.

Key Mayfew-specific behavior:
1. Outputs are written under Mayfew/Outputs/<executable_dir_name>.
2. The default important-cluster thresholds are 0.01 through 0.10.
3. The default fixed-k sweep is 7, 9, 11, 13, 15, 17.
4. Only the best per-threshold analysis JSON files are kept in the final
   output directory. Intermediate sweep artifacts stay in a scratch directory.
5. The extended and stripped BBV files for interval sizes that appear in the
   best-configuration list are copied into the final output directory.
6. The old prefix_sweep_master.csv and prefix_sweep_all_records.json outputs
   are intentionally not produced.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shlex
import shutil
import statistics
import subprocess
import sys
from pathlib import Path


DEFAULT_INTERVAL_SIZES = list(range(4_000_000, 20_000_001, 2_000_000))
DEFAULT_K_VALUES = [7, 9, 11, 13, 15, 17]
DEFAULT_THRESHOLDS = [round(value / 100.0, 2) for value in range(1, 11)]


def parse_csv_float_list(text: str) -> list[float]:
    return [float(part.strip()) for part in text.split(",") if part.strip()]


def parse_csv_int_list(text: str) -> list[int]:
    return [int(part.strip()) for part in text.split(",") if part.strip()]


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


def default_cleanup_bbv_dir() -> Path:
    return find_mayfew_root().parent / "coop-precommit" / "profilers" / "scripts"


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sanitize_name(text: str) -> str:
    sanitized = [
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in text
    ]
    return "".join(sanitized).strip("_") or "run"


def run_command(command: list[str], cwd: Path | None = None) -> None:
    print("[run_prefix_sweep] Running command:")
    print("  " + " ".join(shlex.quote(part) for part in command))
    if cwd is not None:
        print(f"  cwd={cwd}")
    subprocess.run(command, cwd=str(cwd) if cwd else None, check=True)


def read_json(path: Path) -> dict:
    with path.open("r") as handle:
        return json.load(handle)


def write_text(path: Path, text: str) -> None:
    with path.open("w") as handle:
        handle.write(text)


def strip_extended_bbv_file(source_path: Path, dest_path: Path) -> None:
    """
    Convert lines like:
        T:12:7:C1:M2:B3 :55:1:C0:M0:B1
    into:
        T:12:7 :55:1
    """
    with source_path.open("r") as src, dest_path.open("w") as dst:
        for raw_line in src:
            line = raw_line.rstrip("\n")
            if not line.startswith("T"):
                dst.write(raw_line)
                continue

            tokens = line.split()
            stripped_tokens: list[str] = []
            for token in tokens:
                if token.startswith("T:"):
                    parts = token.split(":")
                    if len(parts) >= 3:
                        stripped_tokens.append(":".join(parts[:3]))
                    else:
                        stripped_tokens.append(token)
                elif token.startswith(":"):
                    parts = token.split(":")
                    if len(parts) >= 3:
                        stripped_tokens.append(":".join(parts[:3]))
                    else:
                        stripped_tokens.append(token)
                else:
                    stripped_tokens.append(token)

            dst.write(" ".join(stripped_tokens) + "\n")


def percentile_75(values: list[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = 0.75 * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def summarize_rejection_reason(record: dict) -> str:
    reasons: list[str] = []
    if record["raw_reject_silhouette"]:
        reasons.append("silhouette < 0.10")
    if record["raw_reject_distortion_quartile"]:
        reasons.append("distortion in worst quartile")
    if record["raw_reject_distortion_ratio"]:
        reasons.append("distortion > 1.25x best")
    if record["raw_reject_dominance"]:
        reasons.append("largest cluster dominates")
    return "accepted" if not reasons else "; ".join(reasons)


def choose_best_configuration(records: list[dict], threshold: float) -> dict | None:
    acceptable = [
        record
        for record in records
        if abs(record["important_threshold"] - threshold) < 1e-12 and record["final_accept"]
    ]
    if not acceptable:
        return None
    acceptable.sort(
        key=lambda record: (
            record["prefix_instructions"],
            -record["silhouette"],
            record["weighted_distortion"],
        )
    )
    return acceptable[0]


def write_best_csv(path: Path, best_records: list[dict | None]) -> None:
    fieldnames = [
        "important_threshold",
        "config_name",
        "interval_size",
        "k",
        "prefix_instructions",
        "silhouette",
        "weighted_distortion",
        "largest_cluster_weight",
        "important_cluster_count",
        "analysis_json",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in best_records:
            if record is None:
                continue
            writer.writerow({field: record.get(field) for field in fieldnames})


def build_rules_document(args: argparse.Namespace) -> str:
    lines: list[str] = []
    lines.append("Prefix sweep rules and interpretation guide")
    lines.append("=" * 80)
    lines.append("")
    lines.append("What this sweep does")
    lines.append("-" * 80)
    lines.append(
        "For each interval size and fixed k, the workflow generates an extended BBV file,"
    )
    lines.append(
        "strips the instruction-mix fields for clustering, runs SimPoint clustering,"
    )
    lines.append(
        "and then analyzes the minimum prefix needed to cover every important cluster."
    )
    lines.append("")
    lines.append(f"Interval sizes: {args.interval_sizes}")
    lines.append(f"k values:       {args.k_values}")
    lines.append(f"Thresholds:     {args.thresholds}")
    lines.append("")
    lines.append("Metrics used in the decision")
    lines.append("-" * 80)
    lines.append(
        "Silhouette score measures how well-separated the clusters are. Higher is better."
    )
    lines.append(
        "A low silhouette score means intervals are not clearly grouped and the cluster"
    )
    lines.append(
        "assignment is less trustworthy, so this workflow hard-rejects any configuration"
    )
    lines.append("with silhouette < 0.10.")
    lines.append("")
    lines.append(
        "Weighted distortion measures how far intervals sit from their cluster centroids,"
    )
    lines.append(
        "weighted by cluster importance. Lower is better. A low-distortion configuration"
    )
    lines.append(
        "keeps representative intervals close to the behavior of the cluster they stand for."
    )
    lines.append(
        "This workflow rejects configurations whose distortion is both poor relative to"
    )
    lines.append("their peers and likely to produce weaker representatives.")
    lines.append("")
    lines.append("Implemented rejection rules")
    lines.append("-" * 80)
    lines.append("1) Hard reject if average silhouette < 0.10.")
    lines.append(
        "2) Reject if weighted distortion is in the worst quartile for that threshold,"
    )
    lines.append(
        "   or if it is more than 1.25x the best distortion seen at that threshold."
    )
    lines.append(
        "3) Reject if the largest cluster weight is > 0.90 and that dominance is not"
    )
    lines.append("   common across at least half of the configurations at that threshold.")
    lines.append("")
    lines.append("How to read the summary")
    lines.append("-" * 80)
    lines.append(
        "The summary table groups results by important-cluster threshold. Each row is one"
    )
    lines.append(
        "interval-size / fixed-k configuration. The status column shows whether the"
    )
    lines.append(
        "configuration survived the quality filters, and the reason column explains why"
    )
    lines.append("a rejected configuration was filtered out.")
    lines.append("")
    lines.append(
        "For acceptable configurations, Mayfew chooses the smallest prefix in instructions."
    )
    lines.append(
        "If there is a tie, the workflow prefers higher silhouette and then lower distortion."
    )
    lines.append("")
    lines.append(
        "Only the analysis JSON files for the best per-threshold configurations are kept"
    )
    lines.append(
        "in the final output directory. Intermediate sweep artifacts remain in a scratch"
    )
    lines.append("directory while the sweep is running.")
    return "\n".join(lines) + "\n"


def build_threshold_summary(records: list[dict], threshold: float) -> str:
    threshold_records = [
        record
        for record in records
        if abs(record["important_threshold"] - threshold) < 1e-12
    ]
    threshold_records.sort(
        key=lambda record: (
            record["prefix_instructions"],
            -record["silhouette"],
            record["weighted_distortion"],
        )
    )

    lines: list[str] = []
    lines.append(f"Important-cluster threshold {threshold:.2f}")
    lines.append("-" * 110)
    lines.append(
        "This table shows every configuration tested at this threshold. The prefix column"
    )
    lines.append(
        "is the minimum instruction prefix needed to cover all important clusters, while"
    )
    lines.append(
        "the reason column explains the rejection rule that filtered a configuration out."
    )
    lines.append("")
    lines.append(
        f"{'Status':<10} {'PrefixInstr':>14} {'Interval':>10} {'k':>4} "
        f"{'Silhouette':>12} {'Distortion':>12} {'LargestWt':>10}  Reason"
    )
    for record in threshold_records:
        status = "ACCEPT" if record["final_accept"] else "REJECT"
        lines.append(
            f"{status:<10} "
            f"{record['prefix_instructions']:14d} "
            f"{record['interval_size']:10d} "
            f"{record['k']:4d} "
            f"{record['silhouette']:12.6f} "
            f"{record['weighted_distortion']:12.8f} "
            f"{record['largest_cluster_weight']:10.6f}  "
            f"{record['rejection_reason']}"
        )
    lines.append("")
    return "\n".join(lines)


def cleanup_profile_bbv_files(profile_dir: Path) -> None:
    """
    Remove any `.bb` files from the directory that hosts run_profile.sh.

    This keeps the profiler directory clean after the retained BBVs have been
    copied into the executable-specific Mayfew output directory.
    """
    for bbv_file in profile_dir.glob("*.bb"):
        if bbv_file.is_file():
            bbv_file.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full Phase 2 prefix sweep in the Mayfew layout."
    )
    parser.add_argument(
        "--workload-cmd",
        required=True,
        help="Workload command passed after the '--' of run_profile.sh",
    )
    parser.add_argument(
        "--executable-dir-name",
        required=True,
        help="Name of the executable-specific output directory under Mayfew/Outputs",
    )
    parser.add_argument(
        "--outputs-root",
        default=str(default_outputs_root()),
        help="Root directory for Mayfew outputs",
    )
    parser.add_argument(
        "--run-profile",
        default=str(Path(__file__).resolve().with_name("run_profile.sh")),
        help="Path to run_profile.sh",
    )
    parser.add_argument(
        "--cleanup-bbv-dir",
        default=str(default_cleanup_bbv_dir()),
        help="Directory whose *.bb files should be deleted after the retained BBVs are copied out",
    )
    parser.add_argument(
        "--new-simpoint",
        default=str(Path(__file__).resolve().with_name("new_simpoint.py")),
        help="Path to new_simpoint.py",
    )
    parser.add_argument(
        "--analyze-prefix",
        default=str(Path(__file__).resolve().with_name("analyze_prefix_coverage.py")),
        help="Path to analyze_prefix_coverage.py",
    )
    parser.add_argument(
        "--interval-sizes",
        type=parse_csv_int_list,
        default=DEFAULT_INTERVAL_SIZES,
        help="Comma-separated interval sizes in instructions",
    )
    parser.add_argument(
        "--k-values",
        type=parse_csv_int_list,
        default=DEFAULT_K_VALUES,
        help="Comma-separated fixed k values",
    )
    parser.add_argument(
        "--thresholds",
        type=parse_csv_float_list,
        default=DEFAULT_THRESHOLDS,
        help="Comma-separated important-cluster thresholds",
    )
    parser.add_argument("--projection-dim", type=int, default=15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--keep-scratch",
        action="store_true",
        help="Keep the scratch sweep directory after the run finishes",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    outputs_root = Path(args.outputs_root).expanduser().resolve()
    output_dir = outputs_root / args.executable_dir_name
    scratch_dir = output_dir / "_prefix_sweep_scratch"
    bbv_dir = scratch_dir / "bbv"
    simpoint_dir = scratch_dir / "simpoint"
    analysis_dir = scratch_dir / "analysis"

    ensure_directory(output_dir)
    ensure_directory(bbv_dir)
    ensure_directory(simpoint_dir)
    ensure_directory(analysis_dir)

    run_profile_path = Path(args.run_profile).expanduser().resolve()
    run_profile_dir = run_profile_path.parent
    cleanup_bbv_dir = Path(args.cleanup_bbv_dir).expanduser().resolve()
    run_token = f"{sanitize_name(args.executable_dir_name)}_pid{os.getpid()}"

    rules_doc = output_dir / "prefix_sweep_rules_and_interpretation.txt"
    write_text(rules_doc, build_rules_document(args))
    print(f"[run_prefix_sweep] Wrote rules document to {rules_doc}")

    all_records: list[dict] = []
    bbv_files_by_interval: dict[int, dict[str, Path]] = {}
    total_configs = len(args.interval_sizes) * len(args.k_values) * len(args.thresholds)
    config_counter = 0

    for interval_size in args.interval_sizes:
        print("\n" + "=" * 90)
        print(f"[run_prefix_sweep] Starting interval size {interval_size} instructions")
        print("=" * 90)

        interval_tag = f"int_{interval_size}"
        raw_bbv_prefix = bbv_dir / f"{run_token}_prefix_sweep_{interval_tag}"
        raw_bbv_path = Path(f"{raw_bbv_prefix}.0.bb")
        stripped_bbv_path = bbv_dir / f"{run_token}_prefix_sweep_{interval_tag}_simpoint.0.bb"
        bbv_files_by_interval[interval_size] = {
            "extended": raw_bbv_path,
            "stripped": stripped_bbv_path,
        }

        run_command(
            [
                str(run_profile_path),
                "--option",
                "2",
                "--out",
                str(raw_bbv_prefix),
                "--interval",
                str(interval_size),
                "--",
                *shlex.split(args.workload_cmd),
            ],
            cwd=run_profile_dir,
        )

        strip_extended_bbv_file(raw_bbv_path, stripped_bbv_path)

        for k in args.k_values:
            print("\n" + "-" * 90)
            print(f"[run_prefix_sweep] interval={interval_size}, k={k}")
            print("-" * 90)

            simpoint_prefix = simpoint_dir / f"{run_token}_prefix_sweep_{interval_tag}_k_{k}"
            run_command(
                [
                    sys.executable,
                    args.new_simpoint,
                    "-i",
                    str(stripped_bbv_path),
                    "-o",
                    str(simpoint_prefix),
                    "-k",
                    str(k),
                    "--projection-dim",
                    str(args.projection_dim),
                    "--seed",
                    str(args.seed),
                ]
            )

            labels_path = Path(f"{simpoint_prefix}.labels")
            weights_path = Path(f"{simpoint_prefix}.weights")
            cluster_members_path = Path(f"{simpoint_prefix}.cluster_members")

            for threshold in args.thresholds:
                config_counter += 1
                config_name = f"interval_{interval_size}_k_{k}_thr_{threshold:.2f}"
                analysis_prefix = analysis_dir / config_name

                print(
                    f"[run_prefix_sweep] ({config_counter}/{total_configs}) "
                    f"Analyzing {config_name}"
                )

                run_command(
                    [
                        sys.executable,
                        args.analyze_prefix,
                        "--bbv",
                        str(stripped_bbv_path),
                        "--labels",
                        str(labels_path),
                        "--weights",
                        str(weights_path),
                        "--cluster-members",
                        str(cluster_members_path),
                        "--interval-size",
                        str(interval_size),
                        "--important-threshold",
                        f"{threshold:.2f}",
                        "--output-prefix",
                        str(analysis_prefix),
                        "--projection-dim",
                        str(args.projection_dim),
                        "--seed",
                        str(args.seed),
                        "--config-name",
                        config_name,
                    ]
                )

                analysis_json = analysis_prefix.parent / f"{analysis_prefix.name}.analysis.json"
                analysis = read_json(analysis_json)

                all_records.append(
                    {
                        "config_name": config_name,
                        "interval_size": interval_size,
                        "k": k,
                        "important_threshold": threshold,
                        "prefix_instructions": int(analysis["prefix_instructions"]),
                        "silhouette": float(analysis["silhouette"]),
                        "weighted_distortion": float(analysis["weighted_distortion"]),
                        "largest_cluster_weight": float(analysis["largest_cluster_weight"]),
                        "important_cluster_count": int(analysis["important_cluster_count"]),
                        "raw_reject_silhouette": bool(analysis["rejected_for_silhouette"]),
                        "raw_reject_distortion_quartile": False,
                        "raw_reject_distortion_ratio": False,
                        "raw_reject_dominance": False,
                        "final_accept": False,
                        "rejection_reason": "",
                        "analysis_json": str(output_dir / analysis_json.name),
                        "scratch_analysis_json": str(analysis_json),
                        "scratch_analysis_txt": str(
                            analysis_prefix.parent / f"{analysis_prefix.name}.analysis.txt"
                        ),
                    }
                )

    print("\n" + "=" * 90)
    print("[run_prefix_sweep] Applying cross-configuration rejection rules")
    print("=" * 90)

    for threshold in args.thresholds:
        threshold_records = [
            record
            for record in all_records
            if abs(record["important_threshold"] - threshold) < 1e-12
        ]
        distortions = [record["weighted_distortion"] for record in threshold_records]
        distortion_q75 = percentile_75(distortions)
        best_distortion = min(distortions) if distortions else float("nan")

        dominant_count = sum(
            1 for record in threshold_records if record["largest_cluster_weight"] > 0.90
        )
        dominance_is_common = (
            dominant_count >= math.ceil(0.5 * len(threshold_records))
            if threshold_records
            else False
        )

        print(
            f"[run_prefix_sweep] threshold={threshold:.2f}: "
            f"q75 distortion={distortion_q75:.8f}, "
            f"best distortion={best_distortion:.8f}, "
            f"dominant_count={dominant_count}/{len(threshold_records)}"
        )

        for record in threshold_records:
            record["raw_reject_distortion_quartile"] = (
                record["weighted_distortion"] >= distortion_q75
            )
            record["raw_reject_distortion_ratio"] = (
                record["weighted_distortion"] > 1.25 * best_distortion
            )
            record["raw_reject_dominance"] = (
                record["largest_cluster_weight"] > 0.90 and not dominance_is_common
            )
            record["final_accept"] = not (
                record["raw_reject_silhouette"]
                or record["raw_reject_distortion_quartile"]
                or record["raw_reject_distortion_ratio"]
                or record["raw_reject_dominance"]
            )
            record["rejection_reason"] = summarize_rejection_reason(record)

    best_records: list[dict | None] = []
    summary_lines: list[str] = []
    summary_lines.append("Prefix sweep summary")
    summary_lines.append("=" * 90)
    summary_lines.append("")
    summary_lines.append(
        "This report summarizes the full sweep across interval size, fixed-k, and"
    )
    summary_lines.append(
        "important-cluster threshold. Each table shows the minimum prefix required"
    )
    summary_lines.append(
        "for coverage at that threshold, along with the clustering-quality metrics"
    )
    summary_lines.append(
        "used to accept or reject each configuration."
    )
    summary_lines.append("")

    kept_scratch_jsons: set[str] = set()

    for threshold in args.thresholds:
        summary_lines.append(build_threshold_summary(all_records, threshold))
        best = choose_best_configuration(all_records, threshold)
        best_records.append(best)

        if best is None:
            summary_lines.append(
                f"No acceptable configuration was found for threshold {threshold:.2f}."
            )
            summary_lines.append("")
            continue

        kept_scratch_jsons.add(best["scratch_analysis_json"])
        shutil.copy2(best["scratch_analysis_json"], output_dir / Path(best["scratch_analysis_json"]).name)

        summary_lines.append(f"Best acceptable configuration for threshold {threshold:.2f}:")
        summary_lines.append(f"  config_name         = {best['config_name']}")
        summary_lines.append(f"  prefix_instructions = {best['prefix_instructions']}")
        summary_lines.append(f"  interval_size       = {best['interval_size']}")
        summary_lines.append(f"  k                   = {best['k']}")
        summary_lines.append(f"  silhouette          = {best['silhouette']:.6f}")
        summary_lines.append(f"  weighted_distortion = {best['weighted_distortion']:.8f}")
        summary_lines.append(f"  largest_cluster_wt  = {best['largest_cluster_weight']:.6f}")
        summary_lines.append("")

    kept_interval_sizes = sorted(
        {
            int(record["interval_size"])
            for record in best_records
            if record is not None
        }
    )
    for interval_size in kept_interval_sizes:
        bbv_paths = bbv_files_by_interval.get(interval_size)
        if bbv_paths is None:
            continue
        shutil.copy2(bbv_paths["extended"], output_dir / bbv_paths["extended"].name)
        shutil.copy2(bbv_paths["stripped"], output_dir / bbv_paths["stripped"].name)

    best_csv = output_dir / "prefix_sweep_best_by_threshold.csv"
    summary_txt = output_dir / "prefix_sweep_summary.txt"

    write_best_csv(best_csv, best_records)
    write_text(summary_txt, "\n".join(summary_lines) + "\n")
    cleanup_profile_bbv_files(run_profile_dir)
    if cleanup_bbv_dir != run_profile_dir:
        cleanup_profile_bbv_files(cleanup_bbv_dir)

    if not args.keep_scratch:
        for path in analysis_dir.glob("*.analysis.json"):
            if str(path) not in kept_scratch_jsons and path.exists():
                path.unlink()
        shutil.rmtree(scratch_dir, ignore_errors=True)

    print(f"[run_prefix_sweep] Wrote {best_csv}")
    print(f"[run_prefix_sweep] Wrote {summary_txt}")
    print(f"[run_prefix_sweep] Wrote {rules_doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
