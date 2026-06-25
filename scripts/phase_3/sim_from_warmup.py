#!/usr/bin/env python3
"""
sim_from_warmup.py

Launch one HotGauge thermal simulation from either a single trace/metadata pair
or a workload manifest. This version is adapted for Mayfew, where the script
lives outside the HotGauge repo and is typically run from an interval-specific
output directory.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import click
from tqdm import tqdm


def find_mayfew_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_hotgauge_root() -> Path:
    return find_mayfew_root().parent / "HotGauge"


HOTGAUGE_ROOT = default_hotgauge_root()
sys.path.insert(0, str(HOTGAUGE_ROOT / "HotGauge"))

from HotGauge.power.traces import JSONFilePowerTrace  # type: ignore
from HotGauge.thermal.ICE import ICETransientSim, ICESimConfig, get_stack_template  # type: ignore
from HotGauge.thermal.floorplan import get_flp_info  # type: ignore
from HotGauge.thermal.thermal import run_thermal_sims_with_node_dict  # type: ignore
from HotGauge.thermal.workloads import block_powers_trace_to_DICE  # type: ignore


HEATSINK_MODEL = "HS483"
HEATSINK_ARGS = "6000"


def parse_core_mapping(value: str) -> dict[int, int]:
    text = value.strip()

    def parse_int_list(part: str) -> list[int]:
        items = [piece.strip() for piece in part.split(",") if piece.strip()]
        if not items:
            raise click.BadParameter(f"Invalid '{value}': empty core list.")
        try:
            cores = [int(item) for item in items]
        except ValueError as exc:
            raise click.BadParameter(
                f"Invalid '{value}': core lists must be comma-separated integers."
            ) from exc
        for core in cores:
            if core < 0:
                raise click.BadParameter(f"Invalid '{value}': cores must be >= 0.")
        return cores

    if "->" in text:
        left, right = text.split("->", 1)
        try:
            source = int(left.strip())
        except ValueError as exc:
            raise click.BadParameter(
                f"Invalid '{value}': left side of '->' must be an integer source core."
            ) from exc
        if source < 0:
            raise click.BadParameter(f"Invalid '{value}': source core must be >= 0.")
        targets = parse_int_list(right.strip())
        return {target: source for target in dict.fromkeys(targets)}

    cores = list(dict.fromkeys(parse_int_list(text)))
    return {core: core for core in cores}


def load_workloads(
    trace_file: str | None,
    metadata_file: str | None,
    workload_manifest: str | None,
) -> list[dict[str, str]]:
    single_mode = trace_file is not None or metadata_file is not None
    manifest_mode = workload_manifest is not None

    if manifest_mode and single_mode:
        raise click.UsageError(
            "Use either --workload-manifest OR (--trace-file AND --metadata-file), not both."
        )

    if manifest_mode:
        manifest_path = Path(workload_manifest).expanduser().resolve()
        try:
            data = json.loads(manifest_path.read_text())
        except Exception as exc:
            raise click.ClickException(
                f"Failed to read manifest JSON: {manifest_path}\n{exc}"
            ) from exc

        if not isinstance(data, list) or not data:
            raise click.UsageError("--workload-manifest must be a non-empty JSON list.")

        workloads: list[dict[str, str]] = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise click.UsageError(f"Manifest entry {index} is not an object.")
            if "trace" not in item or "meta" not in item:
                raise click.UsageError(
                    f"Manifest entry {index} must include keys 'trace' and 'meta'."
                )
            workloads.append(
                {
                    "trace": str(Path(item["trace"]).expanduser().resolve()),
                    "meta": str(Path(item["meta"]).expanduser().resolve()),
                }
            )
        return workloads

    if (trace_file is None) != (metadata_file is None):
        raise click.UsageError(
            "If using single-file mode, you must provide BOTH --trace-file and --metadata-file."
        )
    if trace_file is None or metadata_file is None:
        raise click.UsageError(
            "Provide either --workload-manifest OR (--trace-file AND --metadata-file)."
        )

    return [{"trace": trace_file, "meta": metadata_file}]


def resolve_flp_template_path(flp_template: str, flp_base_dir: str | None) -> Path:
    template_path = Path(flp_template).expanduser()
    if template_path.is_absolute():
        resolved = template_path.resolve()
        if not resolved.exists():
            raise click.ClickException(f"FLP template path does not exist: {resolved}")
        return resolved

    search_dirs: list[Path] = []
    if flp_base_dir:
        search_dirs.append(Path(flp_base_dir).expanduser().resolve())

    search_dirs.extend(
        [
            Path.cwd(),
            default_hotgauge_root() / "examples" / "template" / "floorplans" / "outputs",
        ]
    )

    for directory in search_dirs:
        matches = glob.glob(str(directory / flp_template))
        if matches:
            return Path(matches[0]).resolve()

    raise click.ClickException(
        f"Could not locate flp template '{flp_template}'. "
        "Use --flp-base-dir or pass an absolute --flp-template path."
    )


def tech_node(node_str: str) -> int:
    return int(node_str.strip("nm"))


def frequency(freq_str: str) -> float:
    return float(freq_str.strip("GHz"))


SIM_DIR_STRUCTURE = [
    ("interval_ns", float),
    ("workload", str),
    ("tech_node", tech_node),
    ("frequency", frequency),
    ("cores_str", str),
    ("warmup", str),
]


def sim_info_to_sim_dir(sim_info: dict) -> list[str]:
    return [str(sim_info[key]) for key, _ in SIM_DIR_STRUCTURE]


@click.command()
@click.option("--tstack-path", required=True, type=click.Path(file_okay=True))
@click.option(
    "--flp-template",
    required=True,
    type=str,
    help="FLP template filename, glob, or absolute path",
)
@click.option(
    "--flp-base-dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Optional directory used to resolve a relative --flp-template",
)
@click.option("--trace-file", type=click.Path(exists=True, dir_okay=False))
@click.option("--metadata-file", type=click.Path(exists=True, dir_okay=False))
@click.option("--workload-manifest", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--core-mapping",
    type=str,
    default=None,
    help=(
        "Either identity active cores '0,2,6' or a single-source mapping "
        "'0->2,6'."
    ),
)
def main(
    tstack_path: str,
    flp_template: str,
    flp_base_dir: str | None,
    trace_file: str | None,
    metadata_file: str | None,
    workload_manifest: str | None,
    core_mapping: str | None,
) -> None:
    workloads = load_workloads(trace_file, metadata_file, workload_manifest)

    raw_workload_traces: dict[str, dict] = {}
    for workload in workloads:
        with open(workload["meta"], "r") as handle:
            raw_workload_traces[workload["trace"]] = json.load(handle)

    flp_template_path = resolve_flp_template_path(flp_template, flp_base_dir)
    warmup_labels = ["idle_00"]
    tdata_info = {(str(flp_template_path), warmup_labels[0]): tstack_path}

    stack_template = get_stack_template(f"skylake_{HEATSINK_MODEL}")
    thermal_sims: dict[str, list] = defaultdict(list)

    core_sources = parse_core_mapping(core_mapping) if core_mapping else {0: 0}
    target_cores = tuple(sorted(core_sources.keys()))
    cores_list_str = "_".join(map(str, target_cores))
    cores_str = f"core_{cores_list_str}" if len(target_cores) == 1 else f"cores_{cores_list_str}"

    base_dir = Path.cwd()
    output_dir = base_dir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    for trace_path, metadata in tqdm(raw_workload_traces.items()):
        workload_trace = JSONFilePowerTrace(trace_path, metadata["interval_ns"] * 1e-9)

        for (template_path, warmup_label), initial_temp in tdata_info.items():
            flp_info = get_flp_info(template_path)
            if metadata["tech_node"] != flp_info["node_nm"]:
                continue

            sim_config = ICESimConfig(
                initial_temp=initial_temp,
                plugin_args=HEATSINK_ARGS,
                output_list=[
                    ICETransientSim.DIE_TMAP_OUTPUT,
                    ICETransientSim.DIE_TFLP_OUTPUT,
                ],
            )

            sim_info = metadata.copy()
            sim_info.update({"cores_str": cores_str, "warmup": warmup_label})
            sim_dir = output_dir / "sims" / Path(*sim_info_to_sim_dir(sim_info))

            sim_trace = block_powers_trace_to_DICE(
                workload_trace,
                template_path,
                metadata["tech_node"],
                core_sources=core_sources,
            )

            sim = ICETransientSim(
                stack_template,
                template_path,
                sim_trace,
                sim_config,
                str(sim_dir),
            )
            thermal_sims[metadata["tech_node"]].append(sim)

    run_thermal_sims_with_node_dict(thermal_sims)
    print("Simulation finished.")


if __name__ == "__main__":
    main()
