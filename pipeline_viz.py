#!/usr/bin/env python3
"""Ibex pipeline diagram visualizer.

Reads an Ibex tracer log (trace_core_00000000.log) and generates a
self-contained HTML pipeline diagram: every retired instruction as a row,
cycle-accurate IF / ID-EX / WB cells, a plain-English explanation of what
each instruction did (with real register values), stall annotations,
function labels from an objdump disassembly, and a summary bar.

Usage:
  python3 pipeline_viz.py --trace trace_core_00000000.log \
      [--dis add_test.dis] [--highlight 284] [--out pipeline_diagram.html]
"""

import argparse
import html
import re
import sys

# ---------------------------------------------------------------- ABI names

ABI = {0: "zero", 1: "ra", 2: "sp", 3: "gp", 4: "tp",
       5: "t0", 6: "t1", 7: "t2", 8: "s0", 9: "s1"}
ABI.update({10 + i: f"a{i}" for i in range(8)})       # x10-x17
ABI.update({18 + i: f"s{2 + i}" for i in range(10)})  # x18-x27
ABI.update({28 + i: f"t{3 + i}" for i in range(4)})   # x28-x31


def reg_name(xname):
    """'x15' -> 'x15(a5)'"""
    n = int(xname[1:])
    return f"{xname}({ABI[n]})"


def fmt_val(v):
    """Format a value as hex, plus decimal when it is small enough to read."""
    sv = v - (1 << 32) if v >= (1 << 31) else v
    if -4096 < sv < 4096:
        return f"{sv} (0x{v:x})"
    return f"0x{v:08x}"


# Memory-mapped peripherals of the simple_system
MMIO = {0x20000: "ASCII output (simulated UART)",
        0x20008: "simulator halt register"}

# ------------------------------------------------------------- trace parsing

REG_TOKEN = re.compile(
    r"(x\d+)([:=])(0x[0-9a-fA-F]+)|"
    r"(PA):(0x[0-9a-fA-F]+)|"
    r"(store|load):(0x[0-9a-fA-F]+)")


def parse_trace(path):
    entries = []
    with open(path) as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("Time"):
                continue
            fields = [x.strip() for x in line.split("\t")]
            if len(fields) < 5:
                continue
            try:
                t = int(fields[0])
                cyc = int(fields[1])
            except ValueError:
                continue
            pc = int(fields[2], 16)
            insn_hex = fields[3]
            mnem = fields[4]
            rest = "\t".join(fields[5:])

            reads, writes, mem = [], [], None
            for m in REG_TOKEN.finditer(rest):
                if m.group(1):                       # register token
                    reg, op, val = m.group(1), m.group(2), int(m.group(3), 16)
                    (reads if op == ":" else writes).append((reg, val))
                elif m.group(4):                     # PA
                    mem = mem or {}
                    mem["pa"] = int(m.group(5), 16)
                else:                                # store / load
                    mem = mem or {}
                    mem["type"] = m.group(6)
                    mem["val"] = int(m.group(7), 16)

            operands = REG_TOKEN.sub("", rest).strip()
            entries.append({
                "time": t, "cycle": cyc, "pc": pc, "hex": insn_hex,
                "mnem": mnem, "ops": operands, "reads": reads,
                "writes": writes, "mem": mem, "raw": line.strip(),
                "size": 2 if len(insn_hex) <= 4 else 4,
            })
    return entries


# ------------------------------------------------------- function name table

def parse_dis(path):
    """objdump -d output -> sorted [(addr, name)] of function symbols."""
    funcs = []
    pat = re.compile(r"^([0-9a-fA-F]+)\s+<([^>]+)>:")
    try:
        with open(path) as f:
            for line in f:
                m = pat.match(line)
                if m:
                    funcs.append((int(m.group(1), 16), m.group(2)))
    except OSError:
        return []
    funcs.sort()
    return funcs


def func_for(pc, funcs):
    name = "?"
    for addr, fname in funcs:
        if addr <= pc:
            name = fname
        else:
            break
    return name


# -------------------------------------------------------------- categories

BRANCHES = {"beq", "bne", "blt", "bge", "bltu", "bgeu",
            "c.beqz", "c.bnez", "beqz", "bnez"}
JUMPS = {"jal", "jalr", "j", "jr", "ret", "c.j", "c.jal", "c.jr", "c.jalr"}
SYSTEM = {"csrrw", "csrrs", "csrrc", "csrrwi", "csrrsi", "csrrci",
          "csrw", "csrr", "csrs", "csrc", "wfi", "ecall", "ebreak",
          "fence", "fence.i", "mret"}


def category(e):
    m = e["mnem"]
    if m in BRANCHES or m in JUMPS:
        return "branch"
    if e["mem"] is not None:
        return "mem"
    if m in SYSTEM:
        return "sys"
    return "alu"


CAT_LABEL = {"alu": "ALU", "mem": "load/store",
             "branch": "branch/jump", "sys": "CSR/system"}

# ------------------------------------------------------------- explanations


def explain(e, nxt):
    """Plain-English description of what the instruction did, with values."""
    m = e["mnem"]
    reads = dict(e["reads"])
    writes = dict(e["writes"])
    mem = e["mem"]

    def w_first():
        return next(iter(e["writes"]), (None, None))

    # --- constants / moves -------------------------------------------------
    if m in ("c.li", "li"):
        r, v = w_first()
        return f"Load constant: {reg_name(r)} = {fmt_val(v)}"
    if m == "lui":
        r, v = w_first()
        return f"Load upper immediate: {reg_name(r)} = {fmt_val(v)}"
    if m == "auipc":
        r, v = w_first()
        return f"PC-relative address: {reg_name(r)} = PC + offset = {fmt_val(v)}"
    if m in ("c.mv", "mv"):
        r, v = w_first()
        src = [n for n, _ in e["reads"] if n != "x0"]
        s = f" (copy of {reg_name(src[-1])})" if src else ""
        return f"Copy register: {reg_name(r)} = {fmt_val(v)}{s}"

    # --- loads / stores ----------------------------------------------------
    if mem and mem.get("type") == "load":
        r, v = w_first()
        where = MMIO.get(mem["pa"], f"mem[0x{mem['pa']:x}]")
        return f"Load: {reg_name(r)} ← {where} = {fmt_val(v)}"
    if mem and mem.get("type") == "store":
        v = mem["val"]
        where = MMIO.get(mem["pa"], f"mem[0x{mem['pa']:x}]")
        extra = ""
        if mem["pa"] == 0x20000 and 32 <= v < 127:
            extra = f" → prints '{chr(v)}'"
        elif mem["pa"] == 0x20000 and v == 10:
            extra = " → prints newline"
        return f"Store: {where} ← {fmt_val(v)}{extra}"

    # --- branches / jumps --------------------------------------------------
    if m in BRANCHES:
        taken = None
        if nxt is not None:
            taken = nxt["pc"] != e["pc"] + e["size"]
        vals = ", ".join(f"{reg_name(r)}={fmt_val(v)}" for r, v in e["reads"])
        verdict = "" if taken is None else (" → TAKEN" if taken else " → not taken")
        return f"Branch ({m}): compare {vals}{verdict}"
    if m in JUMPS:
        r, v = w_first()
        link = f", return address saved in {reg_name(r)}" if r else ""
        if m in ("c.jr", "jr", "ret", "jalr") and e["reads"]:
            tr, tv = e["reads"][0]
            return f"Jump to address in {reg_name(tr)} = 0x{tv:x}{link}"
        target = e["ops"].split(",")[-1].strip() if e["ops"] else "?"
        kind = "Function call" if r else "Jump"
        return f"{kind} → {target}{link}"

    # --- CSR / system ------------------------------------------------------
    if m.startswith("csr"):
        note = ""
        if "mcountinhibit" in e["ops"]:
            rv = e["reads"][0][1] if e["reads"] else None
            if rv == 0:
                note = " (enables performance counters)"
            elif rv is not None:
                note = " (disables performance counters)"
        return f"CSR access: {e['ops']}{note}"
    if m == "wfi":
        return "Wait for interrupt — core sleeps until an interrupt fires"
    if m in ("ecall", "ebreak", "mret", "fence", "fence.i"):
        return f"System instruction: {m}"

    # --- generic ALU -------------------------------------------------------
    r, v = w_first()
    if r is not None:
        ins = ", ".join(f"{reg_name(n)}={fmt_val(val)}" for n, val in e["reads"])
        op = m.replace("c.", "").upper()
        src = f" from {ins}" if ins else ""
        return f"{op}: {reg_name(r)} = {fmt_val(v)}{src}"
    return f"{e['mnem']} {e['ops']}"


# ------------------------------------------------------------ stall reasons

def stall_reason(prev, cur, gap):
    if prev is None:
        return f"{gap}-cycle gap"
    if prev["mnem"] in BRANCHES or prev["mnem"] in JUMPS:
        return f"branch/jump recovery — pipeline flushed, refetching from 0x{cur['pc']:x} ({gap} cycles)"
    if prev["mem"] and prev["mem"].get("type") == "load":
        loaded = {r for r, _ in prev["writes"]}
        used = {r for r, _ in cur["reads"]}
        if loaded & used:
            return f"load-use hazard — waiting for {', '.join(sorted(loaded & used))} ({gap} cycles)"
        return f"memory access latency ({gap} cycles)"
    if prev["mem"]:
        return f"memory access latency ({gap} cycles)"
    if prev["mnem"] == "wfi":
        return f"core slept in WFI until interrupt ({gap} cycles)"
    return f"stall ({gap} cycles)"


# ----------------------------------------------------------------- HTML out

CSS = """
body { font-family: 'Segoe UI', Arial, sans-serif; margin: 16px; background:#fafafa; color:#222; }
h1 { font-size: 20px; }
.summary { background:#fff; border:1px solid #ddd; border-radius:8px; padding:10px 16px;
           margin-bottom:12px; display:flex; gap:28px; flex-wrap:wrap; }
.summary b { font-size: 18px; display:block; }
.summary span { font-size: 12px; color:#666; }
.legend { margin: 8px 0 12px; font-size: 12px; }
.legend span { padding: 2px 8px; border-radius:4px; margin-right:8px; }
.wrap { overflow-x:auto; border:1px solid #ccc; background:#fff; }
table { border-collapse: collapse; font-size: 12px; }
th, td { padding: 2px 5px; white-space: nowrap; }
thead th { position: sticky; top:0; background:#333; color:#fff; font-weight:normal;
           font-size:10px; min-width:20px; text-align:center; z-index:3; }
.info { position: sticky; background:#fff; z-index:2; border-right:1px solid #eee; }
.c0 { left:0;     min-width:34px;  max-width:34px;  color:#999; text-align:right;}
.c1 { left:44px;  min-width:88px;  max-width:88px;  color:#555; overflow:hidden; }
.c2 { left:142px; min-width:200px; max-width:200px; font-family:Consolas,monospace; overflow:hidden;}
.c3 { left:352px; min-width:380px; max-width:380px; overflow:hidden; text-overflow:ellipsis;
      border-right:2px solid #999; }
tr:nth-child(even) .info { background:#f6f6f6; }
.stage { text-align:center; font-size:10px; font-weight:bold; min-width:20px; }
.IF  { background:#cce5ff; color:#1d4f86; }
.EX  { background:#ffe9b3; color:#7a5200; }
.WB  { background:#d4edda; color:#1f6b33; }
.stall { background:#ececec; color:#777; font-size:10px; font-style:italic; text-align:left;
         border:1px dashed #bbb; }
.hl .info, .hl td.stage { outline:2px solid #e6b800; background:#fff3b0 !important; }
.cat-alu    { border-left:4px solid #2e9e4f; }
.cat-mem    { border-left:4px solid #2b6cb0; }
.cat-branch { border-left:4px solid #dd7711; }
.cat-sys    { border-left:4px solid #888; }
.fn-strip0 .c1 { background:#eef4fb; }
.fn-strip1 .c1 { background:#fbf4ee; }
"""


def build_html(entries, funcs, highlight):
    total_instr = len(entries)
    first_cyc = entries[0]["cycle"]
    last_cyc = entries[-1]["cycle"]
    total_cyc = last_cyc - first_cyc + 1
    cpi = total_cyc / total_instr if total_instr else 0

    cats = {"alu": 0, "mem": 0, "branch": 0, "sys": 0}
    for e in entries:
        cats[category(e)] += 1

    stalls, stall_cycles = 0, 0
    for i in range(1, len(entries)):
        gap = entries[i]["cycle"] - entries[i - 1]["cycle"] - 1
        if gap > 0:
            stalls += 1
            stall_cycles += gap

    min_cycle = max(0, first_cyc - 2)
    cycles = list(range(min_cycle, last_cyc + 1))

    out = []
    out.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    out.append("<title>Ibex MaxPerf pipeline diagram</title>")
    out.append(f"<style>{CSS}</style></head><body>")
    out.append("<h1>Ibex pipeline diagram — MaxPerf (3-stage: IF → ID/EX → WB)</h1>")

    out.append("<div class='summary'>")
    out.append(f"<div><b>{total_instr}</b><span>instructions retired</span></div>")
    out.append(f"<div><b>{total_cyc}</b><span>cycles (first→last retirement)</span></div>")
    out.append(f"<div><b>{cpi:.2f}</b><span>CPI</span></div>")
    out.append(f"<div><b>{stalls}</b><span>stalls ({stall_cycles} lost cycles)</span></div>")
    out.append(f"<div><b>{cats['alu']}</b><span>ALU ops</span></div>")
    out.append(f"<div><b>{cats['mem']}</b><span>loads/stores</span></div>")
    out.append(f"<div><b>{cats['branch']}</b><span>branches/jumps</span></div>")
    out.append(f"<div><b>{cats['sys']}</b><span>CSR/system</span></div>")
    out.append("</div>")

    out.append("<div class='legend'>"
               "<span class='IF'>IF — fetch</span>"
               "<span class='EX'>ID/EX — decode &amp; execute</span>"
               "<span class='WB'>WB — writeback (instruction retires)</span>"
               "<span class='stall'>stall</span>"
               " &nbsp; row border colour = instruction type: "
               "<span style='color:#2e9e4f'>ALU</span> "
               "<span style='color:#2b6cb0'>load/store</span> "
               "<span style='color:#dd7711'>branch/jump</span> "
               "<span style='color:#888'>CSR/system</span>"
               "</div>")

    out.append("<div class='wrap'><table>")

    # header
    out.append("<thead><tr>")
    out.append("<th class='info c0'>#</th><th class='info c1'>function</th>")
    out.append("<th class='info c2'>instruction</th><th class='info c3'>what happened</th>")
    for c in cycles:
        out.append(f"<th>{c if c % 5 == 0 else ''}</th>")
    out.append("</tr></thead><tbody>")

    ncols = len(cycles)
    fn_prev, strip = None, 0

    for i, e in enumerate(entries):
        nxt = entries[i + 1] if i + 1 < len(entries) else None
        prev = entries[i - 1] if i > 0 else None

        # stall annotation row
        gap = e["cycle"] - prev["cycle"] - 1 if prev else 0
        if gap > 0:
            reason = stall_reason(prev, e, gap)
            lead = prev["cycle"] + 1 - min_cycle
            out.append("<tr>")
            out.append("<td class='info c0'></td><td class='info c1'></td>"
                       "<td class='info c2'></td>"
                       f"<td class='info c3' style='color:#999;font-style:italic'>⏸ {html.escape(reason)}</td>")
            if lead > 0:
                out.append(f"<td colspan='{lead}'></td>")
            out.append(f"<td class='stall' colspan='{gap}'>⏸</td>")
            tail = ncols - lead - gap
            if tail > 0:
                out.append(f"<td colspan='{tail}'></td>")
            out.append("</tr>")

        fn = func_for(e["pc"], funcs)
        if fn != fn_prev:
            strip ^= 1
            fn_prev = fn

        cat = category(e)
        desc = explain(e, nxt)
        hl = " hl" if e["cycle"] == highlight else ""
        tip = html.escape(
            f"cycle {e['cycle']}  time {e['time']}\n"
            f"PC 0x{e['pc']:08x}   encoding {e['hex']}\n"
            f"{e['mnem']} {e['ops']}\n"
            f"reads:  {', '.join(f'{r}=0x{v:x}' for r, v in e['reads']) or '-'}\n"
            f"writes: {', '.join(f'{r}=0x{v:x}' for r, v in e['writes']) or '-'}"
            + (f"\nmemory: {e['mem']['type']} 0x{e['mem']['val']:x} @ 0x{e['mem']['pa']:x}"
               if e['mem'] and 'type' in e['mem'] else ""))

        out.append(f"<tr class='fn-strip{strip}{hl}' title=\"{tip}\">")
        out.append(f"<td class='info c0'>{i + 1}</td>")
        out.append(f"<td class='info c1'>{html.escape(fn)}</td>")
        out.append(f"<td class='info c2 cat-{cat}'>"
                   f"{html.escape(e['mnem'])} {html.escape(e['ops'])}</td>")
        out.append(f"<td class='info c3'>{html.escape(desc)}</td>")

        if_col = e["cycle"] - 2 - min_cycle
        if if_col > 0:
            out.append(f"<td colspan='{if_col}'></td>")
        out.append("<td class='stage IF' title='fetching instruction from "
                   f"0x{e['pc']:08x}'>IF</td>")
        out.append("<td class='stage EX' title='decode + execute: "
                   f"{html.escape(e['mnem'])}'>EX</td>")
        out.append("<td class='stage WB' title='writeback: "
                   + html.escape(", ".join(f"{r}=0x{v:x}" for r, v in e["writes"]) or "no register write")
                   + "'>WB</td>")
        tail = ncols - if_col - 3
        if tail > 0:
            out.append(f"<td colspan='{tail}'></td>")
        out.append("</tr>")

    out.append("</tbody></table></div>")
    out.append("<p style='font-size:11px;color:#888'>Stage timing reconstructed from the "
               "retirement trace: an instruction retiring at cycle N occupied IF at N-2, "
               "ID/EX at N-1, WB at N. Hover any row for raw trace detail.</p>")
    out.append("</body></html>")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--dis", help="objdump -d output for function names")
    ap.add_argument("--highlight", type=int, default=None,
                    help="retirement cycle to highlight (optional)")
    ap.add_argument("--out", default="pipeline_diagram.html")
    args = ap.parse_args()

    entries = parse_trace(args.trace)
    if not entries:
        sys.exit(f"no instructions parsed from {args.trace}")
    funcs = parse_dis(args.dis) if args.dis else []

    html_text = build_html(entries, funcs, args.highlight)
    with open(args.out, "w") as f:
        f.write(html_text)
    print(f"{len(entries)} instructions -> {args.out}")


if __name__ == "__main__":
    main()
