# Guided tour — reading a pipeline with this tool

Each link below opens a live demo at an exact instruction and cycle (the URL hash
does the jumping). Use **⟨ prev / next ⟩** or the arrow keys to step one clock cycle
at a time, and **▶ play** to watch it run.

## 1. One ADD, start to finish (add_test)

Open: [the `c.add` in its EXECUTE cycle](https://idoappel.github.io/ibex-pipeline-explorer/add_test.html#i=136&c=283)

The C line is `int result = a + b;` with a=10, b=20 (kept honest with `volatile`).
What to look at:

- The **journey strip**: FETCHED at 282 → EXECUTED at 283 → result SAVED at 284
- The **ALU block**: input A = `10`, input B = `20`, result = `30` — these are the
  real wires, sampled from the simulation waveform
- Step to **cycle 284**: the Writeback block shows `30` going into register `x8 (s0)`,
  and x8 flashes green in the register panel
- While the ADD executes, the **Fetch station** is already reading the next
  instruction — three instructions in flight at once. That's the whole point of a
  pipeline.
- Click the **Decoder** block: the 16-bit compressed `c.add` (0x943e) expands to the
  full 32-bit `add x8,x8,x15` (0x00f40433), bit-fields colored and explained

## 2. A timer interrupt hijacks the pipeline (hello_test)

Open: [the first interrupt entry](https://idoappel.github.io/ibex-pipeline-explorer/hello_test.html#i=466&c=2700)

The program executed `wfi` (wait-for-interrupt) and went to sleep — the core clock is
literally gated off. What to look at:

- The narration: the core is **asleep**; step forward and the timer fires
- ⚡ **INTERRUPT**: the PC jumps to `mtvec + 0x1c` — the RISC-V vector slot for
  machine timer interrupts — and `mepc` holds the return address
- The **CSR panel** (right side): watch `irq_timer` ring, `mepc` capture the
  interrupted PC
- The **minimap**: six red stripes = six timer interrupts in this run. Click any of
  them.
- The **memory map**: inside the handler, the timer region lights up as the handler
  reprograms `mtimecmp`
- Find the `mret` at the end of the handler: the core jumps back exactly where it
  slept

## 3. Loops and hazards (fib_test)

Open: [fib(12) being computed](https://idoappel.github.io/ibex-pipeline-explorer/fib_test.html#i=289&c=516)

A fibonacci loop and an array sum. What to look at:

- The ALU computing the sequence live: step backwards through the loop iterations and
  watch `add x13,x9,x14` produce 1, 2, 3, 5, 8... up to 144 (0x90)
- [Cycle 282](https://idoappel.github.io/ibex-pipeline-explorer/fib_test.html#i=219&c=282):
  a **load-use hazard** — the Execute station freezes for a cycle because the next
  instruction needs a value still arriving from memory. The Pipeline-controller block
  shows the chip's own `stall_ld_hz` signal raised; this is not an inference.
- The **console** panel typing `fib(12) = 00000090 ... PASS` as you play

## Things to try anywhere

- Click any **register** to jump to the instruction that last wrote it
- Click a **C source line** to jump to its first instruction
- Click any **block** in the diagram for its internal signals and a private mini
  waveform
- Use the **function filter** to hide the startup/library noise and see only `main()`
- The green badge in the header is the run's self-verification: every register write
  cross-checked between the trace and the waveform
