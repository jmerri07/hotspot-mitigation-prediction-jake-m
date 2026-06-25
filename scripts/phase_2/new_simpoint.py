#!/usr/bin/env python3
"""
SimPoint - BBV Clustering Tool (Python Implementation)

A modern Python implementation of SimPoint using sklearn, replacing the
legacy C++ SimPoint 3.2 tool.

Algorithm:
1. Parse BBV file (frequency vectors per interval)
2. Normalize vectors (sum = 1)
3. Apply random projection for dimensionality reduction (default: 15D)
4. Run k-means clustering for k = 1 to maxK
5. Select best k using BIC (Bayesian Information Criterion)
6. Output simpoints (representative intervals) and weights
7. Output per-cluster ranked interval membership

Usage:
    python3 simpoint.py -i bbv.0.bb -o simpoints -maxK 15
    python3 simpoint.py -i bbv.0.bb -o simpoints --no-projection

Output:
    simpoints.simpts           - Representative interval indices
    simpoints.weights          - Cluster weights (fraction of total)
    simpoints.labels           - Cluster assignment for each interval
    simpoints.cluster_members  - All intervals per cluster, ranked by representativeness

References:
    - SimPoint 3.0: https://cseweb.ucsd.edu/~calder/simpoint/
    - "Automatically Characterizing Large Scale Program Behavior" (ASPLOS 2002)
"""

import argparse
import gzip
import re
import sys
import numpy as np
from pathlib import Path

try:
    from sklearn.cluster import KMeans
    from sklearn.random_projection import GaussianRandomProjection
except ImportError:
    print("Error: sklearn not found. Install with: pip install scikit-learn")
    sys.exit(1)


def parse_bbv_line(line):
    """
    Parse a single BBV line into a sparse vector representation.

    Formats supported:
        Standard:  T:bb:count :bb:count ...
        Extended:  T:bb:count:C#:M#:B# :bb:count:C#:M#:B# ...

    Returns:
        dict: {bb_index: count, ...}
    """
    vector = {}

    line = line.strip()
    if not line or not line.startswith('T'):
        return None

    # Remove 'T' prefix
    line = line[1:]

    # Pattern matches :bb:count or :bb:count:C#:M#:B#
    # We only care about bb and count for clustering
    pattern = r':(\d+):(\d+)'

    for match in re.finditer(pattern, line):
        bb_index = int(match.group(1))
        count = int(match.group(2))
        vector[bb_index] = vector.get(bb_index, 0) + count

    return vector if vector else None


def load_bbv_file(filepath):
    """
    Load BBV file and convert to dense matrix.

    Args:
        filepath: Path to BBV file (supports .gz)

    Returns:
        numpy.ndarray: Matrix of shape (n_intervals, n_dimensions)
    """
    vectors = []
    max_dim = 0

    # Handle gzipped files
    if str(filepath).endswith('.gz'):
        open_func = lambda f: gzip.open(f, 'rt')
    else:
        open_func = lambda f: open(f, 'r')

    print(f"Loading BBV file: {filepath}")

    with open_func(filepath) as f:
        for line_num, line in enumerate(f, 1):
            vec = parse_bbv_line(line)
            if vec is not None:
                vectors.append(vec)
                if vec:
                    max_dim = max(max_dim, max(vec.keys()))

    if not vectors:
        print("Error: No valid intervals found in BBV file")
        sys.exit(1)

    n_intervals = len(vectors)
    n_dims = max_dim + 1  # BB indices are 1-based in source data; dense matrix uses direct index

    print(f"  Intervals: {n_intervals}")
    print(f"  Max BB index: {max_dim}")
    print(f"  Dimensions: {n_dims}")

    # Convert sparse vectors to dense matrix
    matrix = np.zeros((n_intervals, n_dims), dtype=np.float64)
    for i, vec in enumerate(vectors):
        for bb_idx, count in vec.items():
            matrix[i, bb_idx] = count

    return matrix


def normalize_vectors(matrix):
    """
    Normalize each vector so elements sum to 1.
    This makes vectors comparable regardless of interval length.
    """
    row_sums = matrix.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1
    return matrix / row_sums


def apply_random_projection(matrix, target_dim=15, random_state=42):
    """
    Reduce dimensionality using random projection.
    SimPoint uses 15 dimensions by default.
    """
    if matrix.shape[1] <= target_dim:
        print(f"  Skipping projection (dims={matrix.shape[1]} <= {target_dim})")
        return matrix

    print(f"  Projecting {matrix.shape[1]} dims -> {target_dim} dims")
    projector = GaussianRandomProjection(
        n_components=target_dim,
        random_state=random_state
    )
    return projector.fit_transform(matrix)


def compute_bic(matrix, labels, centers):
    """
    Compute Bayesian Information Criterion (BIC) for clustering.

    Lower BIC = better fit (balances goodness-of-fit vs complexity)

    BIC = n * log(RSS/n) + k * log(n)
    where:
        n = number of points
        k = number of parameters (centers * dimensions)
        RSS = residual sum of squares
    """
    n_samples, n_features = matrix.shape
    n_clusters = len(centers)

    rss = 0.0
    for i, label in enumerate(labels):
        diff = matrix[i] - centers[label]
        rss += np.dot(diff, diff)

    if rss == 0:
        rss = 1e-10

    n_params = n_clusters * n_features + n_clusters
    bic = n_samples * np.log(rss / n_samples) + n_params * np.log(n_samples)

    return bic


def find_representative(matrix, labels, center, cluster_id):
    """
    Find the interval closest to cluster center.
    This is the representative (simpoint) for the cluster.
    """
    cluster_indices = np.where(labels == cluster_id)[0]
    if len(cluster_indices) == 0:
        return None

    min_dist = float('inf')
    representative = None

    for idx in cluster_indices:
        dist = np.linalg.norm(matrix[idx] - center)
        if dist < min_dist:
            min_dist = dist
            representative = idx

    return representative


def get_cluster_rankings(matrix, labels, centers):
    """
    For each cluster, rank all assigned intervals by distance to cluster center.

    Returns:
        dict:
            {
                cluster_id: [
                    (rank, interval_idx, distance),
                    ...
                ],
                ...
            }
    """
    cluster_rankings = {}

    for cluster_id in range(len(centers)):
        cluster_indices = np.where(labels == cluster_id)[0]
        ranked = []

        for idx in cluster_indices:
            dist = np.linalg.norm(matrix[idx] - centers[cluster_id])
            ranked.append((idx, dist))

        ranked.sort(key=lambda x: x[1])  # smallest distance first

        cluster_rankings[cluster_id] = [
            (rank, interval_idx, distance)
            for rank, (interval_idx, distance) in enumerate(ranked, start=1)
        ]

    return cluster_rankings


def run_simpoint(matrix, max_k=15, min_k=1, random_state=42, verbose=True):
    """
    Run SimPoint clustering algorithm.

    Args:
        matrix: Normalized, projected feature matrix
        max_k: Maximum number of clusters to try
        min_k: Minimum number of clusters
        random_state: Random seed for reproducibility
        verbose: Print progress

    Returns:
        simpoints: List of (interval_index, cluster_id) tuples
        weights: List of cluster weights
        best_k: Selected number of clusters
        best_labels: Cluster assignments for all intervals
        best_centers: Cluster centers
        cluster_rankings: Per-cluster full ranking of intervals by representativeness
    """
    n_samples = matrix.shape[0]
    max_k = min(max_k, n_samples)

    if verbose:
        print(f"\nRunning k-means clustering (k={min_k} to {max_k})...")

    best_bic = float('inf')
    best_k = min_k
    best_labels = None
    best_centers = None

    bic_scores = {}

    for k in range(min_k, max_k + 1):
        kmeans = KMeans(
            n_clusters=k,
            random_state=random_state,
            n_init=10,
            max_iter=300
        )
        labels = kmeans.fit_predict(matrix)
        centers = kmeans.cluster_centers_

        bic = compute_bic(matrix, labels, centers)
        bic_scores[k] = bic

        if verbose:
            print(f"  k={k:3d}: BIC={bic:.2f}")

        if bic < best_bic * 0.9 or (bic <= best_bic and k < best_k):
            best_bic = bic
            best_k = k
            best_labels = labels
            best_centers = centers

    if verbose:
        print(f"\nSelected k={best_k} (BIC={best_bic:.2f})")

    simpoints = []
    weights = []

    for cluster_id in range(best_k):
        cluster_size = np.sum(best_labels == cluster_id)
        if cluster_size == 0:
            continue

        weight = cluster_size / n_samples
        representative = find_representative(
            matrix, best_labels, best_centers[cluster_id], cluster_id
        )

        if representative is not None:
            simpoints.append((representative, cluster_id))
            weights.append(weight)

    # Sort representative simpoints by weight descending
    sorted_indices = np.argsort(weights)[::-1]
    simpoints = [simpoints[i] for i in sorted_indices]
    weights = [weights[i] for i in sorted_indices]

    cluster_rankings = get_cluster_rankings(matrix, best_labels, best_centers)

    return simpoints, weights, best_k, best_labels, best_centers, cluster_rankings


def save_results(simpoints, weights, labels, cluster_rankings, output_prefix):
    """
    Save simpoints, weights, labels, and per-cluster interval rankings.

    simpoints file format: interval_index cluster_id
    weights file format: weight cluster_id
    labels file format: interval_index cluster_id (for all intervals)

    cluster_members file format:
        Cluster <id>:
          Rank  Interval  Distance
          ...
    """
    simpoints_file = f"{output_prefix}.simpts"
    weights_file = f"{output_prefix}.weights"
    labels_file = f"{output_prefix}.labels"
    cluster_members_file = f"{output_prefix}.cluster_members"

    with open(simpoints_file, 'w') as f:
        for interval_idx, cluster_id in simpoints:
            f.write(f"{interval_idx} {cluster_id}\n")

    with open(weights_file, 'w') as f:
        for i, weight in enumerate(weights):
            f.write(f"{weight:.10f} {simpoints[i][1]}\n")

    with open(labels_file, 'w') as f:
        for interval_idx, cluster_id in enumerate(labels):
            f.write(f"{interval_idx} {cluster_id}\n")

    with open(cluster_members_file, 'w') as f:
        for cluster_id in sorted(cluster_rankings.keys()):
            f.write(f"Cluster {cluster_id}:\n")
            f.write(f"{'Rank':>6}  {'Interval':>10}  {'Distance':>14}\n")
            for rank, interval_idx, distance in cluster_rankings[cluster_id]:
                f.write(f"{rank:6d}  {interval_idx:10d}  {distance:14.8f}\n")
            f.write("\n")

    print(f"\nResults saved:")
    print(f"  Simpoints:        {simpoints_file}")
    print(f"  Weights:          {weights_file}")
    print(f"  Labels:           {labels_file}")
    print(f"  Cluster members:  {cluster_members_file}")

    return simpoints_file, weights_file, labels_file, cluster_members_file


def print_summary(simpoints, weights):
    """Print a summary of the simpoint analysis."""
    print("\n" + "=" * 60)
    print("SimPoint Analysis Results")
    print("=" * 60)
    print(f"{'Rank':<6} {'Interval':<12} {'Weight':<12} {'Cluster':<8}")
    print("-" * 60)

    for rank, ((interval, cluster), weight) in enumerate(zip(simpoints, weights), 1):
        print(f"{rank:<6} {interval:<12} {weight*100:>10.2f}%  {cluster:<8}")

    print("-" * 60)
    print(f"Total: {len(simpoints)} simpoints, {sum(weights)*100:.1f}% coverage")


def main():
    parser = argparse.ArgumentParser(
        description="SimPoint BBV Clustering Tool (Python/sklearn implementation)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -i bbv.0.bb -o results -maxK 15
  %(prog)s -i bbv.gz -o results --no-projection
  %(prog)s -i bbv.0.bb -o results -k 5  # Force exactly 5 clusters

Output files:
  <output>.simpts           - Representative intervals (one per line: interval cluster)
  <output>.weights          - Cluster weights (one per line: weight cluster)
  <output>.labels           - All interval assignments (one per line: interval cluster)
  <output>.cluster_members  - All intervals in each cluster, ranked by representativeness
        """
    )

    parser.add_argument('-i', '--input', required=True,
                        help='Input BBV file (supports .gz)')
    parser.add_argument('-o', '--output', required=True,
                        help='Output prefix for simpoints/weights files')
    parser.add_argument('-maxK', '--max-clusters', type=int, default=15,
                        help='Maximum number of clusters (default: 15)')
    parser.add_argument('-k', '--fixed-k', type=int, default=None,
                        help='Use fixed number of clusters (skip BIC selection)')
    parser.add_argument('--projection-dim', type=int, default=15,
                        help='Random projection dimensions (default: 15)')
    parser.add_argument('--no-projection', action='store_true',
                        help='Disable random projection')
    parser.add_argument('--seed', type=int, default=42,
                        help='Random seed (default: 42)')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Minimal output')

    args = parser.parse_args()
    verbose = not args.quiet

    # Load and preprocess
    matrix = load_bbv_file(args.input)

    if verbose:
        print("\nPreprocessing...")

    matrix = normalize_vectors(matrix)
    if verbose:
        print("  Normalized vectors (sum=1)")

    if not args.no_projection:
        matrix = apply_random_projection(
            matrix,
            target_dim=args.projection_dim,
            random_state=args.seed
        )

    if args.fixed_k:
        min_k = max_k = args.fixed_k
    else:
        min_k = 1
        max_k = args.max_clusters

    simpoints, weights, best_k, labels, centers, cluster_rankings = run_simpoint(
        matrix,
        max_k=max_k,
        min_k=min_k,
        random_state=args.seed,
        verbose=verbose
    )

    save_results(simpoints, weights, labels, cluster_rankings, args.output)

    if verbose:
        print_summary(simpoints, weights)


if __name__ == "__main__":
    main()
