#!/usr/bin/env bash
# One-command pipeline exploration for any simple_system test.
#   ./explore.sh add_test
#   ./explore.sh hello_test
# Builds the firmware, runs the Verilator simulation with tracing, and
# generates the interactive explorer + static pipeline table into
# explorer_runs/<test>/ so runs never overwrite each other.
set -euo pipefail

TEST=${1:?usage: ./explore.sh <test_name>   (a directory under examples/sw/simple_system/)}
cd "$(dirname "$0")"

# Tool locations -- override via environment if yours live elsewhere:
#   VERILATOR_BIN  dir containing verilator        (default /tools/verilator/v4.210/bin)
#   RISCV_BIN      dir containing riscv32-...-gcc  (default /tools/riscv/bin)
#   PYTHON_BIN     dir containing python3/fusesoc  (default $HOME/ibex-venv/bin)
for d in "${VERILATOR_BIN:-/tools/verilator/v4.210/bin}" \
         "${RISCV_BIN:-/tools/riscv/bin}" \
         "${PYTHON_BIN:-$HOME/ibex-venv/bin}"; do
  [ -d "$d" ] && export PATH="$d:$PATH"
done

SW=examples/sw/simple_system/$TEST
SIM=build/lowrisc_ibex_ibex_simple_system_0/sim-verilator/Vibex_simple_system
[ -d "$SW" ] || { echo "ERROR: no such test: $SW"; exit 1; }
[ -x "$SIM" ] || { echo "ERROR: simulation binary missing — build it first (see SIMULATION-SETUP.md)"; exit 1; }

echo "=== [1/4] building firmware: $TEST ==="
make -C "$SW"

RUN=explorer_runs/$TEST
mkdir -p "$RUN"

echo "=== [2/4] running simulation (with waveform tracing) ==="
"$SIM" -t --meminit=ram,"$SW/$TEST.elf"
mv sim.fst "$RUN/sim.fst"
mv trace_core_00000000.log "$RUN/trace.log"
[ -f ibex_simple_system.log ] && cp ibex_simple_system.log "$RUN/console.log"
[ -f ibex_simple_system_pcount.csv ] && cp ibex_simple_system_pcount.csv "$RUN/pcounts.csv"

echo "=== [3/4] disassembling ==="
riscv32-unknown-elf-objdump -dl "$SW/$TEST.elf" > "$RUN/disasm.txt"

echo "=== [4/4] generating explorer ==="
python3 pipeline_explorer.py --trace "$RUN/trace.log" --fst "$RUN/sim.fst" \
  --vcd "$RUN/sim.vcd" --dis "$RUN/disasm.txt" --out "$RUN/explorer.html"
python3 pipeline_viz.py --trace "$RUN/trace.log" --dis "$RUN/disasm.txt" \
  --out "$RUN/pipeline_table.html"

WIN=$(echo "$HOME/ibex/$RUN/explorer.html" | tr '/' '\\')
echo
echo "done. open in Windows:"
echo "  \\\\wsl.localhost\\Ubuntu$WIN"
