#!/usr/bin/env python3
"""
analyze_prefix_coverage.py

Analyze one fixed clustering configuration produced by new_simpoint.py and
compute:
1. Clustering quality metrics
2. The minimum prefix needed to cover all important clusters
3. Representative intervals and non-representative intervals for those clusters

The output JSON is designed to feed the Mayfew Phase 2/3 workflow.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

try:
    from sklearn.metrics import silhouette_score
    from sklearn.random_projection import GaussianRandomProjection
except ImportError:
    print("Error: sklearn not found. Install with: pip install scikit-learn")
    sys.exit(1)


def parse_bbv_line(line: str) -> dict[int, int] | None:
    vector: dict[int, int] = {}
    line = line.strip()
    if not line or not line.startswith("T"):
        return None

    line = line[1:]
    for match in re.finditer(r":(\d+):(\d+)", line):
        bb_index = int(match.group(1))
        count = int(match.group(2))
        vector[bb_index] = vector.get(bb_index, 0) + count

    return vector if vector else None


def load_bbv_file(filepath: str) -> np.ndarray:
    vectors: list[dict[int, int]] = []
    max_dim = 0

    if filepath.endswith(".gz"):
        open_func = lambda path: gzip.open(path, "rt")
    else:
        open_func = lambda path: open(path, "r")

    with open_func(filepath) as handle:
        for line in handle:
            vector = parse_bbv_line(line)
            if vector is None:
                continue
            vectors.append(vector)
            max_dim = max(max_dim, max(vector.keys()))

    if not vectors:
        raise RuntimeError(f"No valid BBV intervals found in {filepath}")

    matrix = np.zeros((len(vectors), max_dim + 1), dtype=np.float64)
    for row_index, vector in enumerate(vectors):
        for bb_index, count in vector.items():
            matrix[row_index, bb_index] = count
    return matrix


def normalize_vectors(matrix: np.ndarray) -> np.ndarray:
    row_sums = matrix.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1
    return matrix / row_sums


def apply_random_projection(
    matrix: np.ndarray,
    target_dim: int = 15,
    random_state: int = 42,
) -> np.ndarray:
    if matrix.shape[1] <= target_dim:
        return matrix
    projector = GaussianRandomProjection(
        n_components=target_dim,
        random_state=random_state,
    )
    return projector.fit_transform(matrix)


def load_labels(labels_path: str) -> np.ndarray:
    labels: list[int] = []
    with open(labels_path, "r") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            interval_idx_str, cluster_id_str = line.split()
            interval_idx = int(interval_idx_str)
            cluster_id = int(cluster_id_str)
            if interval_idx != len(labels):
                raise RuntimeError(
                    f"Labels file {labels_path} is not sequential at interval {interval_idx}"
                )
            labels.append(cluster_id)
    return np.array(labels, dtype=np.int32)


def load_weights(weights_path: str) -> dict[int, float]:
    weights: dict[int, float] = {}
    with open(weights_path, "r") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            weight_str, cluster_id_str = line.split()
            weights[int(cluster_id_str)] = float(weight_str)
    return weights


def load_cluster_members(cluster_members_path: str) -> dict[int, list[dict]]:
    cluster_members: dict[int, list[dict]] = {}
    current_cluster: int | None = None

    with open(cluster_members_path, "r") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("Cluster ") and line.endswith(":"):
                current_cluster = int(line.split()[1].rstrip(":"))
                cluster_members[current_cluster] = []
                continue

            if line.startswith("Rank") or current_cluster is None:
                continue

            parts = line.split()
            if len(parts) != 3:
                continue

            rank, interval_index, distance = parts
            cluster_members[current_cluster].append(
                {
                    "rank": int(rank),
                    "interval_index": int(interval_index),
                    "distance": float(distance),
                }
            )

    return cluster_members


def compute_centroids(matrix: np.ndarray, labels: np.ndarray) -> dict[int, np.ndarray]:
    centroids: dict[int, np.ndarray] = {}
    for cluster_id in sorted(set(labels.tolist())):
        member_indices = np.where(labels == cluster_id)[0]
        centroids[cluster_id] = matrix[member_indices].mean(axis=0)
    return centroids


def compute_weighted_distortion(
    matrix: np.ndarray,
    labels: np.ndarray,
    weights: dict[int, float],
    centroids: dict[int, np.ndarray],
) -> tuple[float, dict[int, float]]:
    distortion = 0.0
    per_cluster: dict[int, float] = {}

    for cluster_id, centroid in centroids.items():
        member_indices = np.where(labels == cluster_id)[0]
        if len(member_indices) == 0:
            per_cluster[cluster_id] = 0.0
            continue

        distances = np.linalg.norm(matrix[member_indices] - centroid, axis=1)
        mean_distance = float(np.mean(distances))
        per_cluster[cluster_id] = mean_distance
        distortion += weights.get(cluster_id, 0.0) * mean_distance

    return float(distortion), per_cluster


def compute_average_silhouette(matrix: np.ndarray, labels: np.ndarray) -> float:
    unique_clusters = sorted(set(labels.tolist()))
    if len(unique_clusters) < 2:
        return float("nan")
    try:
        return float(silhouette_score(matrix, labels, metric="euclidean"))
    except Exception:
        return float("nan")


def find_important_clusters(weights: dict[int, float], threshold: float) -> list[int]:
    return sorted(
        cluster_id
        for cluster_id, weight in weights.items()
        if weight >= threshold
    )


def first_member_interval_for_cluster(labels: np.ndarray, cluster_id: int) -> int | None:
    member_indices = np.where(labels == cluster_id)[0]
    if len(member_indices) == 0:
        return None
    return int(member_indices[0])


def best_available_representative(
    cluster_members: dict[int, list[dict]],
    cluster_id: int,
    prefix_interval_count: int,
) -> dict | None:
    for entry in cluster_members.get(cluster_id, []):
        if entry["interval_index"] < prefix_interval_count:
            return entry
    return None


def build_interval_sections(
    important_clusters: list[int],
    weights: dict[int, float],
    cluster_members: dict[int, list[dict]],
    first_intervals: dict[int, int],
    prefix_intervals: int,
) -> tuple[list[dict], list[dict]]:
    representatives: list[dict] = []
    non_representatives: list[dict] = []

    for cluster_id in important_clusters:
        chosen = best_available_representative(
            cluster_members,
            cluster_id,
            prefix_intervals,
        )
        if chosen is None:
            raise RuntimeError(
                f"No representative found inside the covering prefix for cluster {cluster_id}"
            )

        representative_entry = {
            "cluster_id": cluster_id,
            "weight": weights[cluster_id],
            "first_interval": first_intervals[cluster_id],
            "chosen_interval": chosen["interval_index"],
            "chosen_rank": chosen["rank"],
            "chosen_distance": chosen["distance"],
        }
        representatives.append(representative_entry)

        for member in cluster_members.get(cluster_id, []):
            if member["interval_index"] == chosen["interval_index"]:
                continue
            non_representatives.append(
                {
                    "cluster_id": cluster_id,
                    "weight": weights[cluster_id],
                    "first_interval": first_intervals[cluster_id],
                    "interval_index": member["interval_index"],
                    "rank": member["rank"],
                    "distance": member["distance"],
                    "within_prefix": member["interval_index"] < prefix_intervals,
                }
            )

    return representatives, non_representatives


def build_analysis_result(
    matrix: np.ndarray,
    labels: np.ndarray,
    weights: dict[int, float],
    cluster_members: dict[int, list[dict]],
    interval_size: int,
    important_threshold: float,
    config_name: str,
) -> dict:
    centroids = compute_centroids(matrix, labels)
    silhouette = compute_average_silhouette(matrix, labels)
    weighted_distortion, per_cluster_distortion = compute_weighted_distortion(
        matrix,
        labels,
        weights,
        centroids,
    )

    important_clusters = find_important_clusters(weights, important_threshold)

    if not important_clusters:
        prefix_intervals = 0
        prefix_instructions = 0
        first_intervals: dict[int, int] = {}
        representatives: list[dict] = []
        non_representatives: list[dict] = []
    else:
        first_intervals = {}
        for cluster_id in important_clusters:
            first_interval = first_member_interval_for_cluster(labels, cluster_id)
            if first_interval is None:
                raise RuntimeError(
                    f"Important cluster {cluster_id} has no members in labels"
                )
            first_intervals[cluster_id] = first_interval

        prefix_intervals = max(first_intervals.values()) + 1
        prefix_instructions = prefix_intervals * interval_size
        representatives, non_representatives = build_interval_sections(
            important_clusters,
            weights,
            cluster_members,
            first_intervals,
            prefix_intervals,
        )

    largest_cluster_weight = max(weights.values()) if weights else float("nan")
    rejected_for_silhouette = bool(math.isnan(silhouette) or silhouette < 0.10)

    return {
        "config_name": config_name,
        "num_intervals": int(matrix.shape[0]),
        "num_clusters": int(len(set(labels.tolist()))),
        "interval_size": int(interval_size),
        "important_threshold": float(important_threshold),
        "silhouette": float(silhouette),
        "weighted_distortion": float(weighted_distortion),
        "largest_cluster_weight": float(largest_cluster_weight),
        "rejected_for_silhouette": rejected_for_silhouette,
        "important_clusters": important_clusters,
        "important_cluster_count": int(len(important_clusters)),
        "prefix_intervals": int(prefix_intervals),
        "prefix_instructions": int(prefix_instructions),
        "representatives": representatives,
        "non_representatives": non_representatives,
        "per_cluster_distortion": per_cluster_distortion,
    }


def write_json(path: str, payload: dict) -> None:
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def write_summary_text(path: str, result: dict) -> None:
    with open(path, "w") as handle:
        handle.write("Prefix coverage analysis summary\n")
        handle.write("=" * 80 + "\n\n")
        handle.write(f"Configuration:           {result['config_name']}\n")
        handle.write(f"Interval size:           {result['interval_size']} instructions\n")
        handle.write(f"Number of intervals:     {result['num_intervals']}\n")
        handle.write(f"Number of clusters:      {result['num_clusters']}\n")
        handle.write(f"Important threshold:     {result['important_threshold']:.2f}\n")
        handle.write(f"Average silhouette:      {result['silhouette']:.6f}\n")
        handle.write(f"Weighted distortion:     {result['weighted_distortion']:.8f}\n")
        handle.write(f"Largest cluster weight:  {result['largest_cluster_weight']:.8f}\n")
        handle.write(f"Rejected by silhouette:  {result['rejected_for_silhouette']}\n")
        handle.write(f"Important clusters:      {result['important_clusters']}\n")
        handle.write(f"Prefix length:           {result['prefix_instructions']} instructions\n")
        handle.write("\n")

        handle.write("Chosen representatives inside the minimum prefix\n")
        handle.write("-" * 80 + "\n")
        if not result["representatives"]:
            handle.write("No important clusters at this threshold. Prefix length is 0.\n")
        else:
            handle.write(
                f"{'Cluster':>8}  {'Weight':>10}  {'FirstSeen':>10}  "
                f"{'ChosenInt':>10}  {'Rank':>6}  {'Distance':>12}\n"
            )
            for representative in result["representatives"]:
                handle.write(
                    f"{representative['cluster_id']:8d}  "
                    f"{representative['weight']:10.6f}  "
                    f"{representative['first_interval']:10d}  "
                    f"{representative['chosen_interval']:10d}  "
                    f"{representative['chosen_rank']:6d}  "
                    f"{representative['chosen_distance']:12.8f}\n"
                )

        handle.write("\n")
        handle.write("Non-representative intervals from important clusters\n")
        handle.write("-" * 80 + "\n")
        if not result["non_representatives"]:
            handle.write("No non-representative intervals were recorded.\n")
        else:
            handle.write(
                f"{'Cluster':>8}  {'Weight':>10}  {'FirstSeen':>10}  "
                f"{'Interval':>10}  {'Rank':>6}  {'Distance':>12}  {'InPrefix':>8}\n"
            )
            for member in result["non_representatives"]:
                handle.write(
                    f"{member['cluster_id']:8d}  "
                    f"{member['weight']:10.6f}  "
                    f"{member['first_interval']:10d}  "
                    f"{member['interval_index']:10d}  "
                    f"{member['rank']:6d}  "
                    f"{member['distance']:12.8f}  "
                    f"{str(member['within_prefix']):>8}\n"
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze prefix coverage and clustering quality for one fixed configuration."
    )
    parser.add_argument("--bbv", required=True, help="Stripped BBV file used for clustering")
    parser.add_argument("--labels", required=True, help="Path to .labels file from new_simpoint.py")
    parser.add_argument("--weights", required=True, help="Path to .weights file from new_simpoint.py")
    parser.add_argument("--cluster-members", required=True, help="Path to .cluster_members file")
    parser.add_argument("--interval-size", required=True, type=int, help="Interval size in instructions")
    parser.add_argument("--important-threshold", required=True, type=float)
    parser.add_argument("--output-prefix", required=True, help="Prefix for analysis outputs")
    parser.add_argument("--projection-dim", type=int, default=15, help="Projection dimension")
    parser.add_argument("--no-projection", action="store_true", help="Disable random projection")
    parser.add_argument("--seed", type=int, default=42, help="Random seed used by new_simpoint.py")
    parser.add_argument("--config-name", default=None, help="Optional human-readable name")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config_name = args.config_name or f"interval_{args.interval_size}_thr_{args.important_threshold:.2f}"

    print(f"[analyze_prefix_coverage] Loading BBV matrix for {config_name}")
    matrix = load_bbv_file(args.bbv)
    matrix = normalize_vectors(matrix)
    if not args.no_projection:
        matrix = apply_random_projection(
            matrix,
            target_dim=args.projection_dim,
            random_state=args.seed,
        )

    print(f"[analyze_prefix_coverage] Loading clustering outputs for {config_name}")
    labels = load_labels(args.labels)
    weights = load_weights(args.weights)
    cluster_members = load_cluster_members(args.cluster_members)

    if len(labels) != matrix.shape[0]:
        raise RuntimeError(
            f"Label count ({len(labels)}) does not match number of BBV intervals ({matrix.shape[0]})"
        )

    print(f"[analyze_prefix_coverage] Computing metrics for {config_name}")
    result = build_analysis_result(
        matrix=matrix,
        labels=labels,
        weights=weights,
        cluster_members=cluster_members,
        interval_size=args.interval_size,
        important_threshold=args.important_threshold,
        config_name=config_name,
    )

    json_path = f"{args.output_prefix}.analysis.json"
    txt_path = f"{args.output_prefix}.analysis.txt"

    write_json(json_path, result)
    write_summary_text(txt_path, result)

    print(f"[analyze_prefix_coverage] Wrote {json_path}")
    print(f"[analyze_prefix_coverage] Wrote {txt_path}")
    print(
        f"[analyze_prefix_coverage] prefix={result['prefix_instructions']} instructions, "
        f"silhouette={result['silhouette']:.6f}, "
        f"distortion={result['weighted_distortion']:.8f}"
    )


if __name__ == "__main__":
    main()
