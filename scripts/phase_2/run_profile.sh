#!/bin/bash

# QEMU profiler wrapper for the Mayfew Phase 2 workflow.
#
# Usage:
#   ./run_profile.sh --option <0|1|2|3> --out <output_path> [--interval <n>] \
#       [--qemu-bin <path>] [--plugin-dir <path>] -- <executable> [args...]
#
# Options:
#   0: Basic block statistics (hotblocks plugin)
#   1: Basic block vectors for SimPoint (bbv plugin)
#   2: Extended BBV with instruction mix (bbv_extended plugin)
#   3: Dynamic CFG in DOT format (cfg_trace plugin)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHASE_DIR="$(cd "${SCRIPT_DIR}" && pwd)"
SCRIPTS_DIR="$(cd "${PHASE_DIR}/.." && pwd)"
MAYFEW_ROOT="$(cd "${SCRIPTS_DIR}/.." && pwd)"
PARENT_DIR="$(cd "${MAYFEW_ROOT}/.." && pwd)"

DEFAULT_QEMU_BIN="${PARENT_DIR}/coop-precommit/profilers/scripts/qemu/build/qemu-x86_64"
DEFAULT_PLUGIN_DIR="${PARENT_DIR}/coop-precommit/profilers/scripts/qemu/build/contrib/plugins"

OPTION=""
OUTPUT=""
INTERVAL="100000000"
EXECUTABLE=""
QEMU_BIN="${MAYFEW_QEMU_BIN:-$DEFAULT_QEMU_BIN}"
PLUGIN_DIR="${MAYFEW_PLUGIN_DIR:-$DEFAULT_PLUGIN_DIR}"
EXEC_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --option)
            OPTION="$2"
            shift 2
            ;;
        --out)
            OUTPUT="$2"
            shift 2
            ;;
        --interval)
            INTERVAL="$2"
            shift 2
            ;;
        --qemu-bin)
            QEMU_BIN="$2"
            shift 2
            ;;
        --plugin-dir)
            PLUGIN_DIR="$2"
            shift 2
            ;;
        --)
            shift
            EXECUTABLE="$1"
            shift
            EXEC_ARGS=("$@")
            break
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 --option <0|1|2|3> --out <output_path> [--interval <n>] [--qemu-bin <path>] [--plugin-dir <path>] -- <executable> [args...]"
            exit 1
            ;;
    esac
done

if [[ -z "$OPTION" ]]; then
    echo "Error: --option is required (0 for BB, 1 for BBV, 2 for extended BBV, 3 for CFG)"
    exit 1
fi

if [[ -z "$OUTPUT" ]]; then
    echo "Error: --out is required"
    exit 1
fi

if [[ -z "$EXECUTABLE" ]]; then
    echo "Error: executable is required after --"
    exit 1
fi

if [[ ! -f "$EXECUTABLE" ]]; then
    RESOLVED="$(command -v "$EXECUTABLE" 2>/dev/null || true)"
    if [[ -n "$RESOLVED" ]]; then
        EXECUTABLE="$RESOLVED"
    else
        echo "Error: executable not found: $EXECUTABLE"
        exit 1
    fi
fi

if [[ ! -x "$QEMU_BIN" ]]; then
    echo "Error: QEMU binary not found at $QEMU_BIN"
    echo "Hint: pass --qemu-bin or set MAYFEW_QEMU_BIN if your qemu build lives elsewhere."
    exit 1
fi

if [[ ! -d "$PLUGIN_DIR" ]]; then
    echo "Error: QEMU plugin directory not found at $PLUGIN_DIR"
    echo "Hint: pass --plugin-dir or set MAYFEW_PLUGIN_DIR if your plugins live elsewhere."
    exit 1
fi

case "$OPTION" in
    0)
        PLUGIN="${PLUGIN_DIR}/libhotblocks.so"
        if [[ ! -f "$PLUGIN" ]]; then
            echo "Error: hotblocks plugin not found at $PLUGIN"
            exit 1
        fi
        echo "Running BB profiler (hotblocks)..."
        echo "QEMU:   $QEMU_BIN"
        echo "Plugin: $PLUGIN"
        echo "Output: $OUTPUT"
        "$QEMU_BIN" \
            -d plugin \
            -D "$OUTPUT" \
            -plugin "${PLUGIN},inline=true" \
            "$EXECUTABLE" "${EXEC_ARGS[@]}" > /dev/null 2>&1
        echo "Done. Results written to $OUTPUT"
        ;;
    1)
        PLUGIN="${PLUGIN_DIR}/libbbv.so"
        if [[ ! -f "$PLUGIN" ]]; then
            echo "Error: bbv plugin not found at $PLUGIN"
            exit 1
        fi
        echo "Running BBV profiler..."
        echo "QEMU:     $QEMU_BIN"
        echo "Plugin:   $PLUGIN"
        echo "Output:   ${OUTPUT}.0.bb"
        echo "Interval: $INTERVAL instructions"
        "$QEMU_BIN" \
            -d plugin \
            -plugin "${PLUGIN},outfile=${OUTPUT},interval=${INTERVAL}" \
            "$EXECUTABLE" "${EXEC_ARGS[@]}"
        echo "Done. Results written to ${OUTPUT}.0.bb"
        ;;
    2)
        PLUGIN="${PLUGIN_DIR}/libbbv_extended.so"
        if [[ ! -f "$PLUGIN" ]]; then
            echo "Error: bbv_extended plugin not found at $PLUGIN"
            exit 1
        fi
        echo "Running extended BBV profiler (with instruction mix)..."
        echo "QEMU:     $QEMU_BIN"
        echo "Plugin:   $PLUGIN"
        echo "Output:   ${OUTPUT}.0.bb"
        echo "Interval: $INTERVAL instructions"
        "$QEMU_BIN" \
            -d plugin \
            -plugin "${PLUGIN},outfile=${OUTPUT},interval=${INTERVAL}" \
            "$EXECUTABLE" "${EXEC_ARGS[@]}"
        echo "Done. Results written to ${OUTPUT}.0.bb"
        echo "Format: T:bb_index:exec_count:C#:M#:B# (C=compute, M=memory, B=branch)"
        ;;
    3)
        PLUGIN="${PLUGIN_DIR}/libcfg_trace.so"
        if [[ ! -f "$PLUGIN" ]]; then
            echo "Error: cfg_trace plugin not found at $PLUGIN"
            exit 1
        fi
        echo "Running CFG trace profiler..."
        echo "QEMU:   $QEMU_BIN"
        echo "Plugin: $PLUGIN"
        echo "Output: ${OUTPUT}.0.dot"
        "$QEMU_BIN" \
            -d plugin \
            -plugin "${PLUGIN},outfile=${OUTPUT}" \
            "$EXECUTABLE" "${EXEC_ARGS[@]}"
        echo "Done. Results written to ${OUTPUT}.0.dot"
        echo "Visualize: dot -Tpng ${OUTPUT}.0.dot -o ${OUTPUT}.png"
        ;;
    *)
        echo "Error: Invalid option $OPTION (use 0 for BB, 1 for BBV, 2 for extended BBV, 3 for CFG)"
        exit 1
        ;;
esac
