#!/usr/bin/env python3
"""
diagram_visualize.py

Render HotGauge temperature grids for one Mayfew interval run directory.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click
from tqdm import tqdm


def find_mayfew_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_hotgauge_root() -> Path:
    return find_mayfew_root().parent / "HotGauge"


HOTGAUGE_ROOT = default_hotgauge_root()
HG_PKG_PARENT = HOTGAUGE_ROOT / "HotGauge"
TSCA_PKG_PARENT = HOTGAUGE_ROOT / "ThermalSideChannelAnalysisTools"
GRID_THERMAL_MAP_PY = (
    TSCA_PKG_PARENT
    / "ThermalSideChannelAnalysisTools"
    / "grid_thermal_map.py"
)

sys.path.insert(0, str(HG_PKG_PARENT))
from HotGauge.thermal.ICE import load_3DICE_grid_file  # type: ignore


def resolve_run_dir(run_dir: str | None) -> Path:
    if run_dir is not None:
        return Path(run_dir).expanduser().resolve()
    return Path.cwd().resolve()


def run_cmd(cmd: list[str], cwd: Path, env: Optional[dict] = None) -> None:
    print("[cwd]", str(cwd))
    print("[cmd]", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), env=env, check=True)


def svg_to_png(svg_path: Path) -> Path:
    png_path = svg_path.with_suffix(".png")
    subprocess.run(["/usr/bin/convert", str(svg_path), str(png_path)], check=True)
    return png_path


def write_gridthermal_input(frame_2d, out_path: Path, *, one_indexed: bool = False) -> None:
    rows, cols = frame_2d.shape
    start_index = 1 if one_indexed else 0

    with out_path.open("w") as handle:
        idx = start_index
        for row in range(rows):
            for col in range(cols):
                handle.write(f"{idx}\t{float(frame_2d[row, col])}\n")
                idx += 1


@dataclass(frozen=True)
class FlpBlock:
    name: str
    x: float
    y: float
    w: float
    h: float


BLOCK_START_RE = re.compile(r"^\s*([A-Za-z0-9_.+\-]+)\s*:\s*$")
POS_RE = re.compile(r"^\s*position\s+([0-9.+\-eE]+)\s*,\s*([0-9.+\-eE]+)\s*;\s*$")
DIM_RE = re.compile(r"^\s*dimension\s+([0-9.+\-eE]+)\s*,\s*([0-9.+\-eE]+)\s*;\s*$")


def parse_hotgauge_flp(text: str) -> list[FlpBlock]:
    blocks: list[FlpBlock] = []
    current_name: str | None = None
    current_x: float | None = None
    current_y: float | None = None
    current_w: float | None = None
    current_h: float | None = None

    for line in text.splitlines():
        match = BLOCK_START_RE.match(line)
        if match:
            if None not in (current_name, current_x, current_y, current_w, current_h):
                blocks.append(FlpBlock(current_name, current_x, current_y, current_w, current_h))
            current_name = match.group(1)
            current_x = current_y = current_w = current_h = None
            continue

        if current_name is None:
            continue

        match = POS_RE.match(line)
        if match:
            current_x = float(match.group(1))
            current_y = float(match.group(2))
            continue

        match = DIM_RE.match(line)
        if match:
            current_w = float(match.group(1))
            current_h = float(match.group(2))
            continue

    if None not in (current_name, current_x, current_y, current_w, current_h):
        blocks.append(FlpBlock(current_name, current_x, current_y, current_w, current_h))

    if not blocks:
        raise ValueError("No valid floorplan blocks were found in IC.flp")
    return blocks


def write_grid_flp(blocks: list[FlpBlock], out_path: Path) -> None:
    with out_path.open("w") as handle:
        for block in blocks:
            handle.write(
                f"{block.name}\t{block.w:.6f}\t{block.h:.6f}\t{block.x:.6f}\t{block.y:.6f}\n"
            )


def convert_hotgauge_flp_to_grid_flp(in_path: Path, out_path: Path) -> None:
    blocks = parse_hotgauge_flp(in_path.read_text())
    write_grid_flp(blocks, out_path)


def mk_temp_viz(
    sim_dir: Path,
    *,
    num_threads: int,
    celsius: bool,
    one_indexed: bool,
    extra_grid_args: list[str],
    auto_convert_flp: bool,
) -> None:
    if not GRID_THERMAL_MAP_PY.exists():
        raise FileNotFoundError(f"grid_thermal_map.py not found at: {GRID_THERMAL_MAP_PY}")

    die_grid = sim_dir / "die_grid.temps"
    flp_file = sim_dir / "IC.flp"
    if not die_grid.exists():
        raise FileNotFoundError(f"Missing required input: {die_grid}")
    if not flp_file.exists():
        raise FileNotFoundError(f"Missing required input: {flp_file}")

    render_flp = flp_file
    if auto_convert_flp:
        converted_flp = sim_dir / "IC.grid.flp"
        convert_hotgauge_flp_to_grid_flp(flp_file, converted_flp)
        render_flp = converted_flp

    viz_dir = sim_dir / "viz"
    viz_dir.mkdir(parents=True, exist_ok=True)
    tmp_frames_dir = viz_dir / "tmp_frames"
    tmp_frames_dir.mkdir(parents=True, exist_ok=True)

    print("Loading die_grid.temps ...")
    trace = load_3DICE_grid_file(str(die_grid), convert_K_to_C=celsius)
    if trace.ndim == 2:
        trace = trace[None, :, :]
    timesteps, rows, cols = trace.shape
    print(f"Parsed grid trace shape: T={timesteps}, rows={rows}, cols={cols}")

    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(HG_PKG_PARENT)
        + ":"
        + str(TSCA_PKG_PARENT)
        + (":" + env["PYTHONPATH"] if "PYTHONPATH" in env else "")
    )

    def render_step(step: int) -> Path:
        frame_path = tmp_frames_dir / f"frame_{step:05d}.t"
        out_svg = viz_dir / f"temps_{step:05d}.svg"
        write_gridthermal_input(trace[step], frame_path, one_indexed=one_indexed)
        cmd = [
            sys.executable,
            str(GRID_THERMAL_MAP_PY),
            str(render_flp),
            str(frame_path),
            str(out_svg),
            "--input_type",
            "grid",
            "--rows",
            str(rows),
            "--cols",
            str(cols),
            *extra_grid_args,
        ]
        run_cmd(cmd, cwd=sim_dir, env=env)
        return out_svg

    rendered_svgs: list[Path] = []
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(render_step, step) for step in range(timesteps)]
        for future in tqdm(as_completed(futures), total=len(futures), desc="render SVG"):
            rendered_svgs.append(future.result())

    rendered_svgs.sort()

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(svg_to_png, svg) for svg in rendered_svgs]
        for _ in tqdm(as_completed(futures), total=len(futures), desc="convert PNG"):
            pass

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(viz_dir / "temps_%05d.png"),
        "-threads",
        str(min(num_threads, 16)),
        str(viz_dir / "temps.mp4"),
    ]
    run_cmd(ffmpeg_cmd, cwd=sim_dir)


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--metadata-file-name", required=True, type=str)
@click.option("--core-name", required=True, type=str)
@click.option(
    "--run-dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Interval run directory. Defaults to the current working directory.",
)
@click.option("--num-threads", type=int, default=32, show_default=True)
@click.option("--celsius/--kelvin", default=True, show_default=True)
@click.option("--one-indexed/--zero-indexed", default=False, show_default=True)
@click.option(
    "--grid-args",
    multiple=True,
    help="Pass-through args to grid_thermal_map.py. Use multiple times.",
)
@click.option(
    "--auto-convert-flp/--no-auto-convert-flp",
    default=True,
    show_default=True,
)
def main(
    metadata_file_name: str,
    core_name: str,
    run_dir: str | None,
    num_threads: int,
    celsius: bool,
    one_indexed: bool,
    grid_args: list[str],
    auto_convert_flp: bool,
) -> int:
    run_path = resolve_run_dir(run_dir)
    metadata_path = run_path / "Metadata" / metadata_file_name
    if not metadata_path.exists():
        print(f"ERROR: metadata file not found: {metadata_path}", file=sys.stderr)
        return 2

    with metadata_path.open("r") as handle:
        metadata = json.load(handle)

    sim_dir = (
        run_path
        / "outputs"
        / "sims"
        / str(metadata["interval_ns"])
        / str(metadata["workload"])
        / str(metadata["tech_node"])
        / str(metadata["frequency"])
        / core_name
        / "idle_00"
    )

    if not sim_dir.exists():
        print(f"ERROR: sim_dir not found: {sim_dir}", file=sys.stderr)
        return 2

    extra_grid_args: list[str] = []
    for arg in grid_args:
        extra_grid_args.extend(shlex.split(arg))

    try:
        mk_temp_viz(
            sim_dir=sim_dir,
            num_threads=num_threads,
            celsius=celsius,
            one_indexed=one_indexed,
            extra_grid_args=extra_grid_args,
            auto_convert_flp=auto_convert_flp,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print("Visualization complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
