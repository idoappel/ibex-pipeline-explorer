# Ibex Pipeline Explorer

[![selftest](https://github.com/idoappel/ibex-pipeline-explorer/actions/workflows/ci.yml/badge.svg)](https://github.com/idoappel/ibex-pipeline-explorer/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**Watch your C program run through silicon.**

This tool merges three artifacts from an [Ibex RISC-V](https://github.com/lowRISC/ibex)
simulation — the instruction trace, the FST waveform, and a block diagram of the
3-stage pipeline — into **one self-contained interactive HTML page**. No server, no
install: the generated file works in any browser, offline, forever.

It is built for someone who has never seen a pipeline or a waveform. Every cycle is
narrated in plain English, every hardware block explains itself, and every value shown
is the real electrical signal recorded from the simulation.

## Live demos

| Demo | What to look at |
|------|-----------------|
| **[add_test](https://idoappel.github.io/ibex-pipeline-explorer/add_test.html)** | The minimal walkthrough: watch one `c.add` travel FETCH → EXECUTE → SAVE, with the ALU computing 10 + 20 = 30 on the real wires |
| **[hello_test](https://idoappel.github.io/ibex-pipeline-explorer/hello_test.html)** | The showcase: timer interrupts hijacking the pipeline (red stripes on the minimap), `wfi` sleep with the core clock gated off, console output typing itself |
| **[fib_test](https://idoappel.github.io/ibex-pipeline-explorer/fib_test.html)** | Loop-heavy code: fibonacci + array sums — branches, data dependencies and load-use hazards in the wild |

Start with the **[guided tour](GUIDE.md)** — it deep-links into the demos at the exact
interesting cycles.

## What you get

- **Three zoom levels.** Level 1: your program (C source tracking, live register file,
  console output). Level 2: the pipeline (3-stage diagram, occupancy, stalls, cycle
  narration). Level 3: inside the blocks — click the decoder and see the instruction's
  bit-fields sliced and colored, the real 16→32-bit expansion of compressed
  instructions, the OBI bus handshakes, the controller's actual stall signals.
- **A narration engine.** Every cycle gets a story: *"3 things are happening at the same
  time — this is pipelining... ⚡ INTERRUPT! The timer fired and hijacked the pipeline:
  the core saved its place in mepc and jumped to the handler."*
- **Self-verification on every run.** The trace and the waveform are two independent
  records of the same events. The generator cross-checks **every register write**
  between them and prints the score (the badge in the page header). During development
  this caught a real bug: Ibex gates the core clock during `wfi` sleep, which silently
  skewed cycle-to-waveform alignment — the cross-check went from 228/631 to 631/631
  after switching to time-based calibration. See [HOW-IT-WORKS.md](HOW-IT-WORKS.md).
- **Shareable.** Single HTML file with everything embedded; URL hashes deep-link to an
  exact instruction and cycle.

## Quickstart (no hardware tools needed)

```bash
git clone https://github.com/idoappel/ibex-pipeline-explorer
cd ibex-pipeline-explorer
sudo apt install gtkwave        # for fst2vcd
./selftest.sh                   # regenerates an explorer from sample/ and verifies it
python3 pipeline_explorer.py --trace sample/trace.log --fst sample/sim.fst \
  --dis sample/disasm.txt --out my_explorer.html
```

`sample/` contains pre-recorded artifacts from a real simulation, so this works with
nothing but Python 3 and `fst2vcd`.

## Full flow (against an Ibex checkout)

Drop `explore.sh`, `pipeline_explorer.py` and `pipeline_viz.py` into the root of an
[Ibex](https://github.com/lowRISC/ibex) checkout with a built `ibex_simple_system`
Verilator binary (see Ibex's simple_system docs), then:

```bash
./explore.sh hello_test        # any test under examples/sw/simple_system/
```

This builds the firmware, runs the simulation with tracing, and generates
`explorer_runs/hello_test/explorer.html` plus a static pipeline table. Tool paths can
be overridden with `VERILATOR_BIN`, `RISCV_BIN` and `PYTHON_BIN`.

## Limitations

- Timing model assumes the 3-stage (`WritebackStage=1`) Ibex configuration, e.g. the
  `maxperf` config; 2-stage configs would need a different stage mapping
- The memory map and console are those of Ibex's `simple_system` testbench
- Interrupt detection covers vectored-mode entries near `mtvec`

## Credits

- [Ibex](https://github.com/lowRISC/ibex) by lowRISC (Apache-2.0); the sample and demo
  pages embed Ibex's example test programs
- Built with [Claude Code](https://claude.com/claude-code) as an exercise in agentic
  AI for silicon engineering workflows

## License

Apache-2.0 — see [LICENSE](LICENSE).
