# How it works

The explorer is a Python generator that merges three independent records of one
simulation into a single self-contained HTML page.

## Inputs

| Input | Produced by | What it knows |
|-------|------------|---------------|
| `trace_core_00000000.log` | Ibex's RTL tracer | every retired instruction: cycle, PC, encoding, register reads/writes, memory accesses |
| `sim.fst` | Verilator (`-t`) | every signal in the design, every cycle |
| `objdump -dl` output | RISC-V binutils | function symbols and source file:line for every PC |

## Pipeline timing model

Ibex's tracer logs **retirements**. For the 3-stage configuration
(`WritebackStage=1`), an instruction retiring at cycle N occupied:

```
IF (fetch)            cycle N-2
ID/EX (decode+execute) cycle N-1
WB (writeback)         cycle N
```

Occupancy is inverted from the same map: at cycle C, the WB stage holds the
instruction retiring at C, ID/EX holds the one retiring at C+1, IF the one retiring
at C+2. A missing entry is a bubble (stall) — and the controller's real stall
signals (`stall_ld_hz`, `stall_branch`, ...) say why.

## Waveform extraction

1. `fst2vcd` converts the FST to VCD (the only non-Python dependency)
2. A single-pass VCD parser tracks ~40 selected signals, resolved by ranked
   name-suffix matching against the full hierarchy (preferring `u_ibex_core.*`)
3. On every **rising edge of the core clock**, all tracked values are snapshotted

## Cycle calibration — and the bug the tool caught itself

Mapping "trace cycle N" to "waveform snapshot K" started as simple index arithmetic
anchored at the first trace entry (plus a one-edge shift, verified instruction by
instruction). That gave a perfect 170/170 cross-check on a simple test... and
**228/631 on a test that sleeps**.

The cause: Ibex **gates the core clock during `wfi` sleep**. The tracer's cycle
counter keeps counting wall-clock cycles (verified: `time == 2*cycle + 8` for every
trace line), but the core clock — and therefore the snapshot stream — skips the
sleep. Index arithmetic drifted by exactly the slept cycles.

The fix maps by **time** instead of index: cycle C's snapshot is the latest core-clock
posedge at or before `t(C) - 3`, found by binary search. During sleep this naturally
returns the last pre-sleep snapshot — physically correct, since the gated core holds
its state frozen. Result: 631/631.

## Self-verification

The trace and the waveform are produced by different mechanisms, so they can
cross-check each other. For **every** instruction with a register write, the
generator asserts that at its retire cycle the waveform's writeback port shows
`rf_we_wb=1`, the same register index and the same value. The score is printed at
generation time and embedded as a badge in the page. Any mismatch is listed.

This is the same philosophy as dual-record checking in silicon verification
(RTL-vs-ISS cosimulation), applied to a visualization tool: if the picture can lie,
it's worse than no picture.

## The decoder card

The bit-field view decodes the instruction word in JavaScript using the standard
RISC-V formats (R/I/S/B/U/J, selected by opcode). For compressed instructions the
page shows the **real expanded 32-bit word** taken from `instr_rdata_id` on the wires
(verified: `c.add x8,x15` = `0x943e` expands to `add x8,x8,x15` = `0x00f40433`), and
the ALU command (`alu_operator_ex`) decoded through the `alu_op_e` enum parsed from
`ibex_pkg.sv` (with a baked-in fallback table).

## Interrupt detection

An instruction is marked as an interrupt entry when its PC is non-sequential, the
previous instruction was not a branch/jump, and the PC lands inside
`[mtvec, mtvec+128)` — the vectored handler table. On the demo: 6 timer interrupts,
all entering at `mtvec + 0x1c`, which is exactly the RISC-V vector slot for machine
timer interrupts (cause 7). `mepc` on those cycles points back at the interrupted
code.

## Output

Everything — instruction list with plain-English explanations, per-cycle signal
arrays, source files, ABI tables — is embedded as JSON in one HTML file with inline
CSS/JS and an inline SVG diagram. No external resources, so the file can be emailed,
hosted anywhere, or opened from disk.
