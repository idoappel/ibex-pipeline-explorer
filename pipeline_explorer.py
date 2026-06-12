#!/usr/bin/env python3
"""Interactive Ibex pipeline explorer.

Merges three artifacts into one self-contained, beginner-friendly HTML page:
  1. the instruction trace (trace_core_00000000.log)  -> what ran, explained
  2. the waveform (sim.fst, via fst2vcd)              -> real signal values
  3. a block diagram of the MaxPerf 3-stage pipeline  -> where things happen

Click an instruction -> see its journey through FETCH / EXECUTE / SAVE with
the actual hardware signal values at every cycle, plus a plain-English
narration of everything the core is doing at that moment.

Usage:
  python3 pipeline_explorer.py --trace trace_core_00000000.log \
      --fst sim.fst [--dis /tmp/add_test.dis] [--out pipeline_explorer.html]
"""

import argparse
import bisect
import json
import os
import re
import subprocess
import sys

from pipeline_viz import (parse_trace, parse_dis, func_for, explain,
                          category, ABI)

# ----------------------------------------------------------- wanted signals

# key -> ranked list of full-name suffixes (first hit wins, prefer core scope)
WANTED = {
    "clk":       ["u_ibex_core.clk_i"],
    "pc_if":     ["u_ibex_core.pc_if"],
    "pc_id":     ["u_ibex_core.pc_id"],
    "instr":     ["u_ibex_core.instr_rdata_id"],
    "ivalid":    ["u_ibex_core.instr_valid_id"],
    "rf_a":      ["u_ibex_core.rf_rdata_a", "id_stage_i.rf_rdata_a_i"],
    "rf_b":      ["u_ibex_core.rf_rdata_b", "id_stage_i.rf_rdata_b_i"],
    "alu_a":     ["ex_block_i.alu_operand_a_i"],
    "alu_b":     ["ex_block_i.alu_operand_b_i"],
    "alu_r":     ["ex_block_i.alu_result"],
    "we":        ["u_ibex_core.rf_we_wb"],
    "waddr":     ["u_ibex_top.rf_waddr_wb", "wb_stage_i.rf_waddr_wb_o"],
    "wdata":     ["u_ibex_core.rf_wdata_wb"],
    "dreq":      ["u_ibex_core.data_req_o", "u_ibex_top.data_req_o"],
    "dwe":       ["u_ibex_core.data_we_o", "u_ibex_top.data_we_o"],
    "daddr":     ["u_ibex_core.data_addr_o", "u_ibex_top.data_addr_o"],
    "dwdata":    ["u_ibex_core.data_wdata_o", "u_ibex_top.data_wdata_o"],
    "drdata":    ["u_ibex_core.data_rdata_i", "u_ibex_top.data_rdata_i"],
    "drvalid":   ["u_ibex_core.data_rvalid_i", "u_ibex_top.data_rvalid_i"],
    # --- block drill-down internals ---
    "alu_op":    ["u_ibex_core.alu_operator_ex"],
    "is_comp":   ["u_ibex_core.instr_is_compressed_id"],
    "decomp":    ["instr_decompressed"],
    "ireq":      ["u_ibex_top.instr_req_o", "u_ibex_core.instr_req_o"],
    "ignt":      ["u_ibex_top.instr_gnt_i", "u_ibex_core.instr_gnt_i"],
    "irvalid":   ["u_ibex_top.instr_rvalid_i", "u_ibex_core.instr_rvalid_i"],
    "iaddr":     ["u_ibex_top.instr_addr_o", "u_ibex_core.instr_addr_o"],
    "dgnt":      ["u_ibex_core.data_gnt_i", "u_ibex_top.data_gnt_i"],
    "brdec":     ["branch_decision"],
    "pcset":     ["u_ibex_core.pc_set"],
    "st_id":     ["stall_id"],
    "st_ldhz":   ["stall_ld_hz"],
    "st_mem":    ["stall_mem"],
    "st_mdiv":   ["stall_multdiv"],
    "st_br":     ["stall_branch"],
    "st_jmp":    ["stall_jump"],
    # --- interrupts / CSRs (stage 5) ---
    "irq_pend":  ["u_ibex_core.irq_pending", "irq_pending_o"],
    "irq_timer": ["u_ibex_top.irq_timer_i", "irq_timer_i"],
    "mie":       ["csr_mstatus_mie"],
    "mepc":      ["u_ibex_core.csr_mepc", "csr_mepc_i"],
    "mtvec":     ["u_ibex_core.csr_mtvec", "csr_mtvec_i"],
}

# ------------------------------------------------------------- VCD handling


def fst_to_vcd(fst, vcd):
    if (not os.path.exists(vcd)
            or os.path.getmtime(vcd) < os.path.getmtime(fst)):
        subprocess.run(["fst2vcd", fst, "-o", vcd], check=True,
                       stdout=subprocess.DEVNULL)


def parse_vcd(path):
    """Return (snap_times, snapshots) where snapshots[i] is a dict
    key->int|None sampled just after the i-th rising clock edge."""
    # --- header: collect (fullname, id) ---
    names = []
    scope = []
    f = open(path)
    for line in f:
        s = line.strip()
        if s.startswith("$scope"):
            scope.append(s.split()[2])
        elif s.startswith("$upscope"):
            if scope:
                scope.pop()
        elif s.startswith("$var"):
            p = s.split()
            names.append((".".join(scope + [p[4]]), p[3]))
        elif s.startswith("$enddefinitions"):
            break

    # --- resolve wanted keys to vcd ids ---
    resolved, missing = {}, []
    for key, cands in WANTED.items():
        hit = None
        for cand in cands:
            # bare names must match the full final path component, so that
            # e.g. "stall_id" cannot accidentally match "...mystall_id"
            suffix = cand if "." in cand else "." + cand
            matches = [(full, vid) for full, vid in names
                       if full.endswith(suffix)]
            if matches:
                matches.sort(key=lambda m: len(m[0]))
                hit = matches[0]
                break
        if hit:
            resolved[key] = hit[1]
        else:
            missing.append(key)
    clk_id = resolved.pop("clk", None)
    if clk_id is None:
        sys.exit("clock signal not found in VCD")
    track = set(resolved.values()) | {clk_id}

    # --- body: snapshot at every rising clk edge ---
    cur = {}
    snap_times, snaps = [], []
    block_t = None
    clk_old = 0

    def end_block():
        nonlocal clk_old
        clk_new = cur.get(clk_id) or 0
        if block_t is not None and clk_old == 0 and clk_new == 1:
            snap_times.append(block_t)
            snaps.append({k: cur.get(vid) for k, vid in resolved.items()})
        clk_old = clk_new

    for line in f:
        line = line.strip()
        if not line:
            continue
        c0 = line[0]
        if c0 == "#":
            end_block()
            block_t = int(line[1:])
        elif c0 == "b":
            val, vid = line[1:].split()
            if vid in track:
                cur[vid] = None if ("x" in val or "z" in val) else int(val, 2)
        elif c0 in "01xz":
            vid = line[1:]
            if vid in track:
                cur[vid] = None if c0 in "xz" else int(c0)
        # $dumpvars / $end wrappers fall through harmlessly
    end_block()
    f.close()
    return snap_times, snaps, missing, resolved.keys()


# ------------------------------------------------------- source line info


def parse_lineinfo(dis_path):
    """objdump -dl output -> (pc2line {pc:(fileidx,line)}, [filepaths])."""
    pc2line, files, fidx = {}, [], {}
    cur = None
    pat_src = re.compile(r"^(/[^:\s]+):(\d+)")
    pat_ins = re.compile(r"^\s+([0-9a-fA-F]+):")
    try:
        with open(dis_path) as f:
            for line in f:
                m = pat_src.match(line)
                if m:
                    p, ln = m.group(1), int(m.group(2))
                    if p not in fidx:
                        fidx[p] = len(files)
                        files.append(p)
                    cur = (fidx[p], ln)
                    continue
                m = pat_ins.match(line)
                if m and cur:
                    pc2line[int(m.group(1), 16)] = cur
    except OSError:
        pass
    return pc2line, files


# alu_op_e names in enum order, parsed from the Ibex master ibex_pkg.sv;
# used when no local Ibex checkout is available to parse live.
ALU_OP_FALLBACK = [
    "ADD", "SUB", "XOR", "OR", "AND", "XNOR", "ORN", "ANDN", "SRA", "SRL",
    "SLL", "SRO", "SLO", "ROR", "ROL", "GREV", "GORC", "SHFL", "UNSHFL",
    "XPERM_N", "XPERM_B", "XPERM_H", "SH1ADD", "SH2ADD", "SH3ADD", "LT",
    "LTU", "GE", "GEU", "EQ", "NE", "MIN", "MINU", "MAX", "MAXU", "PACK",
    "PACKU", "PACKH", "SEXTB", "SEXTH", "CLZ", "CTZ", "CPOP", "SLT", "SLTU",
    "CMOV", "CMIX", "FSL", "FSR", "BSET", "BCLR", "BINV", "BEXT",
    "BCOMPRESS", "BDECOMPRESS", "BFP", "CLMUL", "CLMULR", "CLMULH",
    "CRC32_B", "CRC32C_B", "CRC32_H", "CRC32C_H", "CRC32_W", "CRC32C_W"]


def parse_alu_ops(pkg_path):
    """ibex_pkg.sv alu_op_e enum (plain ordered list) -> {value: name}.
    Falls back to the baked-in table when no Ibex checkout is present."""
    fallback = dict(enumerate(ALU_OP_FALLBACK))
    try:
        text = open(pkg_path).read()
    except OSError:
        return fallback
    m = re.search(r"typedef enum logic \[6:0\]\s*\{(.*?)\}\s*alu_op_e",
                  text, re.S)
    if not m:
        return fallback
    return {i: n for i, n in enumerate(re.findall(r"\bALU_(\w+)",
                                                  m.group(1)))}


def load_sources(files):
    """Read each referenced source file (if it exists) for embedding."""
    out = []
    for p in files:
        try:
            with open(p, errors="replace") as f:
                lines = f.read().split("\n")[:800]
        except OSError:
            lines = None
        out.append({"name": os.path.basename(p), "lines": lines})
    return out


# ------------------------------------------------------------------- build


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--fst", required=True)
    ap.add_argument("--dis")
    ap.add_argument("--vcd", default="/tmp/sim.vcd")
    ap.add_argument("--out", default="pipeline_explorer.html")
    args = ap.parse_args()

    entries = parse_trace(args.trace)
    if not entries:
        sys.exit("no instructions parsed")
    funcs = parse_dis(args.dis) if args.dis else []
    pc2line, src_paths = parse_lineinfo(args.dis) if args.dis else ({}, [])
    srcs = load_sources(src_paths)
    n_src = sum(1 for s in srcs if s["lines"] is not None)
    print(f"source files embedded: {n_src}/{len(srcs)}; "
          f"{len(pc2line)} instructions have line info")

    fst_to_vcd(args.fst, args.vcd)
    snap_times, snaps, missing, found = parse_vcd(args.vcd)
    print(f"signals found: {', '.join(sorted(found))}")
    if missing:
        print(f"signals MISSING: {', '.join(missing)}")

    # --- calibrate: map cycles to snapshots by TIME, not by index. ---
    # The testbench clock ticks every 2 time units and the tracer's cycle
    # counter is never gated (verified: time == 2*cycle + 8 for all entries),
    # but the CORE clock is gated during wfi sleep, so snapshot indices skip
    # cycles. For cycle C the matching posedge is at (t0-3) + 2*(C-c0): the
    # -3 encodes the verified one-edge shift between the WB-stage state and
    # the tracer's retirement timestamp (170/170 writes matched on add_test).
    # During sleep, bisect returns the last pre-sleep snapshot — physically
    # correct, since the gated core holds its state frozen.
    c0, t0 = entries[0]["cycle"], entries[0]["time"]

    base = max(0, entries[0]["cycle"] - 4)
    maxc = entries[-1]["cycle"] + 2
    keys = sorted(found)
    sig = {k: [] for k in keys}
    for cyc in range(base, maxc + 1):
        target = (t0 - 3) + 2 * (cyc - c0)
        k_idx = bisect.bisect_right(snap_times, target) - 1
        ok = k_idx >= 0
        for k in keys:
            sig[k].append(snaps[k_idx][k] if ok else None)

    # --- self-verification: cross-check every trace register write against
    #     the waveform's writeback port at the retire cycle. Two independent
    #     records of the same events; if they agree, the tool isn't lying. ---
    checked = passed = 0
    mismatches = []
    if all(k in sig for k in ("we", "waddr", "wdata")):
        for e in entries:
            wr = [(r, v) for r, v in e["writes"] if r != "x0"]
            if not wr:
                continue
            reg, val = wr[0]
            idx = e["cycle"] - base
            if not (0 <= idx < len(sig["we"])):
                continue
            checked += 1
            ok = (sig["we"][idx] == 1
                  and sig["waddr"][idx] == int(reg[1:])
                  and sig["wdata"][idx] == val)
            if ok:
                passed += 1
            elif len(mismatches) < 10:
                wd = sig["wdata"][idx]
                mismatches.append(
                    f"  cycle {e['cycle']} {e['mnem']} {e['ops']}: trace says "
                    f"{reg}={val:#x}, waveform says we={sig['we'][idx]} "
                    f"waddr={sig['waddr'][idx]} "
                    f"wdata={'x' if wd is None else hex(wd)}")
    print(f"cross-check: {passed}/{checked} register writes match the waveform"
          + (" -- PASS" if passed == checked else " -- MISMATCHES:"))
    for m in mismatches:
        print(m)

    # --- instruction JSON ---
    instrs = []
    for i, e in enumerate(entries):
        nxt = entries[i + 1] if i + 1 < len(entries) else None
        instrs.append({
            "i": i, "c": e["cycle"], "pc": e["pc"], "hex": e["hex"],
            "mn": e["mnem"], "ops": e["ops"],
            "fn": func_for(e["pc"], funcs), "cat": category(e),
            "d": explain(e, nxt),
            "rd": e["reads"], "wr": e["writes"],
            "mem": e["mem"],
        })
    # default selection: first instruction inside main(), else first overall
    default_idx = next((x["i"] for x in instrs if x["fn"] == "main"), 0)

    pkg = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "rtl", "ibex_pkg.sv")
    aluops = parse_alu_ops(pkg)
    print(f"ALU operator names: {len(aluops)} parsed from ibex_pkg.sv")

    meta = {"base": base, "maxc": maxc, "defaultIdx": default_idx,
            "verify": {"checked": checked, "passed": passed},
            "aluops": aluops,
            "abi": {i: ABI[i] for i in range(32)}}

    p2l = [[pc, f, l] for pc, (f, l) in pc2line.items()]
    html = (TEMPLATE
            .replace("__INSTRS__", json.dumps(instrs))
            .replace("__SIGS__", json.dumps(sig))
            .replace("__SRC__", json.dumps(srcs))
            .replace("__P2L__", json.dumps(p2l))
            .replace("__META__", json.dumps(meta)))
    with open(args.out, "w") as fh:
        fh.write(html)
    print(f"{len(instrs)} instructions, {len(keys)} signals, "
          f"cycles {base}..{maxc} -> {args.out}")


# =========================================================== HTML template

TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Ibex pipeline explorer</title>
<style>
 body{font-family:'Segoe UI',Arial,sans-serif;margin:0;background:#f4f5f7;color:#222}
 header{background:#1d3557;color:#fff;padding:10px 18px}
 header h1{font-size:17px;margin:0}
 header p{margin:2px 0 0;font-size:12px;color:#cfd8ea}
 details.intro{margin:10px 14px;background:#fffbe8;border:1px solid #e6d98a;
   border-radius:8px;padding:8px 14px;font-size:13px;line-height:1.5}
 details.intro summary{cursor:pointer;font-weight:600}
 .cols{display:flex;gap:10px;padding:0 14px 14px}
 #listcol{width:300px;min-width:300px}
 #listcol input,#listcol select{width:100%;box-sizing:border-box;margin-bottom:4px;
   padding:4px 8px;font-size:12px;border:1px solid #ccc;border-radius:6px}
 #list{max-height:74vh;overflow-y:auto;background:#fff;
   border:1px solid #ddd;border-radius:8px}
 #mini{width:100%;height:26px;cursor:pointer;background:#fff;border:1px solid #ddd;
   border-radius:6px;display:block}
 .irow{padding:5px 9px;border-bottom:1px solid #f0f0f0;cursor:pointer;font-size:12px}
 .irow:hover{background:#eef4ff}
 .irow.sel{background:#dbe9ff;border-left:4px solid #1d6ed8}
 .irow .top{font-family:Consolas,monospace}
 .irow .fn{color:#999;font-size:10px;margin-right:6px}
 .irow .dsc{color:#777;font-size:10px;overflow:hidden;text-overflow:ellipsis;
   white-space:nowrap}
 .cat-alu{border-left:4px solid #2e9e4f}.cat-mem{border-left:4px solid #2b6cb0}
 .cat-branch{border-left:4px solid #dd7711}.cat-sys{border-left:4px solid #888}
 #main{flex:1;min-width:0}
 #journey{background:#fff;border:1px solid #ddd;border-radius:8px;padding:8px 14px;
   font-size:13px;margin-bottom:8px}
 #journey b{color:#1d3557}
 .jstep{display:inline-block;padding:3px 10px;border-radius:14px;margin:2px 4px 2px 0;
   cursor:pointer;font-size:12px;border:1px solid #ccc;background:#fafafa}
 .jstep.on{background:#ffe066;border-color:#d4a900;font-weight:600}
 #controls{margin:6px 0;display:flex;align-items:center;gap:8px;font-size:13px}
 #controls button{padding:4px 12px;font-size:13px;cursor:pointer;border-radius:6px;
   border:1px solid #bbb;background:#fff}
 #controls button:hover{background:#eef}
 #cyclelabel{font-weight:700;font-size:15px;min-width:90px;text-align:center}
 svg{background:#fff;border:1px solid #ddd;border-radius:8px;width:100%;height:auto}
 .stage-box{fill:#f8f9fb;stroke:#c6ccd8;stroke-width:1.5;rx:10}
 .stage-on-IF{stroke:#1d6ed8;stroke-width:3.5;fill:#eaf3ff}
 .stage-on-EX{stroke:#d4a900;stroke-width:3.5;fill:#fffae0}
 .stage-on-WB{stroke:#2e9e4f;stroke-width:3.5;fill:#ebf8ef}
 .blk{fill:#fff;stroke:#8a93a6;stroke-width:1.2;rx:6;cursor:help}
 .blk:hover{stroke:#1d6ed8;stroke-width:2}
 .ttl{font-size:12px;font-weight:700;fill:#1d3557}
 .sub{font-size:9.5px;fill:#888}
 .lbl{font-size:10px;fill:#555}
 .val{font-size:10.5px;font-family:Consolas,monospace;fill:#0a58ca;font-weight:600}
 .chip{font-size:10.5px;font-family:Consolas,monospace;font-weight:700;fill:#333}
 .chipsub{font-size:9px;fill:#999}
 .wire{stroke:#8a93a6;stroke-width:1.6;fill:none;marker-end:url(#arr)}
 #narr{background:#fffbe8;border:1px solid #e6d98a;border-radius:8px;
   padding:10px 14px;font-size:13.5px;line-height:1.55;margin:8px 0;min-height:64px}
 #blockinfo{font-size:12px;color:#555;background:#eef4ff;border-radius:6px;
   padding:6px 10px;margin:6px 0;display:none}
 #wavewrap{background:#fff;border:1px solid #ddd;border-radius:8px;padding:8px;
   overflow-x:auto}
 #wavewrap h3{font-size:12px;margin:0 0 6px;color:#555}
 table.wave{border-collapse:collapse;font-size:10.5px;font-family:Consolas,monospace}
 table.wave th{font-family:'Segoe UI';font-weight:400;font-size:10.5px;text-align:left;
   padding:1px 8px 1px 2px;color:#444;white-space:nowrap}
 table.wave td{border:1px solid #eee;padding:1px 6px;text-align:center;min-width:52px;
   color:#333;white-space:nowrap}
 table.wave td.chg{border-left:2px solid #d4a900;background:#fffdf2}
 table.wave td.cur{background:#dbe9ff;font-weight:700}
 table.wave thead td{cursor:pointer;background:#f3f3f3;font-weight:600}
 table.wave thead td.cur{background:#1d6ed8;color:#fff}
 #side{width:370px;min-width:370px}
 .panel{background:#fff;border:1px solid #ddd;border-radius:8px;padding:8px 10px;
   margin-bottom:10px}
 .panel h3{font-size:12px;margin:0 0 6px;color:#1d3557}
 .phint{font-size:10px;color:#999;margin-top:4px}
 #srccode{max-height:240px;overflow:auto;font-family:Consolas,monospace;font-size:11px;
   background:#fafbfc;border:1px solid #eee}
 .sline{white-space:pre;padding:0 4px;cursor:pointer}
 .sline:hover{background:#eef4ff}
 .sline .ln{color:#bbb;display:inline-block;width:30px;text-align:right;margin-right:8px}
 .sline.cur{background:#fff3b0}
 .sline.selline{outline:1.5px solid #1d6ed8;outline-offset:-1.5px}
 #regs{display:grid;grid-template-columns:repeat(4,1fr);gap:3px}
 .reg{border:1px solid #eee;border-radius:4px;padding:1px 4px;font-size:10px;
   cursor:pointer;background:#fafafa;overflow:hidden}
 .reg .rn{color:#888;display:block;font-size:9px}
 .reg .rv{font-family:Consolas,monospace;font-weight:600;white-space:nowrap}
 .reg.wr{background:#d4edda;border-color:#2e9e4f}
 .reg.rd{background:#dbe9ff;border-color:#1d6ed8}
 .reg:hover{border-color:#1d6ed8}
 #cons{background:#101418;color:#7cfc8a;font-family:Consolas,monospace;font-size:12px;
   min-height:56px;max-height:130px;overflow-y:auto;padding:6px 8px;border-radius:6px;
   white-space:pre-wrap;word-break:break-all}
 #card{display:none;position:absolute;top:8px;right:8px;width:460px;max-height:94%;
   overflow-y:auto;background:#fff;border:2px solid #1d6ed8;border-radius:10px;
   box-shadow:0 10px 34px rgba(20,40,80,.35);padding:10px 12px;z-index:10;font-size:12px}
 #cardhead{display:flex;justify-content:space-between;align-items:center;
   margin-bottom:4px;color:#1d3557}
 #cardclose{cursor:pointer;color:#999;font-size:15px;padding:0 4px}
 #cardclose:hover{color:#b02a37}
 #cardexpl{color:#666;font-size:11.5px;line-height:1.45;margin-bottom:8px;
   background:#f5f8ff;border-radius:6px;padding:6px 8px}
 #cardbody{margin-bottom:8px;line-height:1.5}
 #cardbody .bitrow{display:flex;margin:6px 0 2px;font-family:Consolas,monospace}
 #cardbody .bf{padding:3px 4px;font-size:10px;text-align:center;color:#222;
   border:1px solid rgba(0,0,0,.25);overflow:hidden;white-space:nowrap}
 #cardbody .bfl{font-size:9px;display:block;color:rgba(0,0,0,.6)}
 svg.dimmed .stage-box,svg.dimmed .blkg:not(.spot){opacity:.35}
 @keyframes vflash{0%{fill:#c43e00}100%{fill:#0a58ca}}
 text.flash,tspan.flash{animation:vflash .55s}
</style></head><body>
<header><h1>Ibex pipeline explorer — watch your program run through the chip</h1>
<p>MaxPerf configuration · 3-stage pipeline · real signal values from the simulation waveform
<span id="vbadge" style="margin-left:14px;padding:2px 10px;border-radius:10px;font-size:11px"></span></p></header>

<details class="intro" open><summary>What am I looking at? (click to hide)</summary>
<p>A processor runs your program one <b>instruction</b> at a time — tiny commands like
"add these two numbers" or "fetch that value from memory". To go faster, the chip works like an
<b>assembly line with 3 stations</b>: while one instruction is being <b>executed</b>, the next one
is already being <b>fetched</b> from memory, and the previous one is <b>saving its result</b>.
So up to 3 instructions are inside the chip at once. Each tick of this assembly line is one
<b>clock cycle</b>.</p>
<p><b>Registers</b> are 32 tiny storage slots inside the CPU (named x0–x31). Almost every
instruction reads one or two of them and writes one back.</p>
<p><b>How to use this page:</b> click any instruction on the left, then use ▶ or the step
buttons to walk it through the three stations. The diagram shows the <i>actual electrical
signal values</i> recorded from the simulation at every cycle.</p>
</details>

<div class="cols">
 <div id="listcol">
  <input id="search" placeholder="search: mnemonic, register, PC, function…"/>
  <select id="fnfilter"><option value="">all functions</option></select>
  <div id="list"></div>
 </div>
 <div id="main">
  <canvas id="mini" height="26"></canvas>
  <div class="phint" style="margin:1px 0 6px">the whole run, one stripe per instruction —
   green ALU · blue memory · orange branch · gray system · dark ticks = function boundaries ·
   ▾ = your position · click anywhere to jump</div>
  <div id="journey"></div>
  <div id="controls">
    <button id="bprev">⟨ prev cycle</button>
    <span id="cyclelabel"></span>
    <button id="bnext">next cycle ⟩</button>
    <button id="bplay">▶ play</button>
    <span style="color:#999;font-size:11px">step through time and watch the assembly line move</span>
  </div>
  <div id="diagwrap" style="position:relative">
  <svg viewBox="0 0 980 480" id="diag">
   <defs><marker id="arr" markerWidth="9" markerHeight="9" refX="7" refY="3.5" orient="auto">
     <path d="M0,0 L8,3.5 L0,7 z" fill="#8a93a6"/></marker></defs>

   <rect id="st_if" class="stage-box" x="15" y="52" width="235" height="340" rx="10"/>
   <rect id="st_ex" class="stage-box" x="270" y="52" width="430" height="400" rx="10"/>
   <rect id="st_wb" class="stage-box" x="720" y="52" width="245" height="280" rx="10"/>

   <text class="ttl" x="30" y="74">1 · FETCH</text>
   <text class="sub" x="30" y="87">reads the next instruction from memory</text>
   <text class="ttl" x="285" y="74">2 · DECODE + EXECUTE</text>
   <text class="sub" x="285" y="87">figures out the command and computes it</text>
   <text class="ttl" x="735" y="74">3 · SAVE RESULT</text>
   <text class="sub" x="735" y="87">writes the answer into a register</text>

   <text class="chip" id="occ_if" x="30" y="105">—</text>
   <text class="chipsub" id="occ_if2" x="30" y="117"></text>
   <text class="chip" id="occ_ex" x="285" y="105">—</text>
   <text class="chipsub" id="occ_ex2" x="285" y="117"></text>
   <text class="chip" id="occ_wb" x="735" y="105">—</text>
   <text class="chipsub" id="occ_wb2" x="735" y="117"></text>

   <g class="blkg" data-info="pc">
    <rect class="blk" x="35" y="130" width="195" height="78"/>
    <text class="ttl" x="45" y="148" style="font-size:11px">Program Counter (PC)</text>
    <text class="sub" x="45" y="160">the chip's bookmark</text>
    <text class="lbl" x="45" y="178">address being read:</text>
    <text class="val" id="v_pc_if" x="45" y="192">—</text>
   </g>
   <g class="blkg" data-info="prefetch">
    <rect class="blk" x="35" y="228" width="195" height="90"/>
    <text class="ttl" x="45" y="246" style="font-size:11px">Prefetch buffer</text>
    <text class="sub" x="45" y="258">waiting room for instructions</text>
    <text class="lbl" x="45" y="276">raw bits handed to decoder:</text>
    <text class="val" id="v_instr" x="45" y="290">—</text>
    <text class="lbl" x="45" y="306">valid? <tspan class="val" id="v_ivalid">—</tspan></text>
   </g>
   <path class="wire" d="M132 208 L132 228"/>
   <path class="wire" d="M230 268 C 262 268, 256 166, 290 166"/>
   <text class="sub" x="234" y="222">instruction</text>

   <g class="blkg" data-info="decoder">
    <rect class="blk" x="290" y="130" width="180" height="72"/>
    <text class="ttl" x="300" y="148" style="font-size:11px">Decoder</text>
    <text class="sub" x="300" y="160">understands the command</text>
    <text class="lbl" x="300" y="178">decoding:</text>
    <text class="val" id="d_mnem" x="300" y="192">—</text>
   </g>
   <g class="blkg" data-info="ctrl">
    <rect class="blk" x="290" y="342" width="180" height="92"/>
    <text class="ttl" x="300" y="360" style="font-size:11px">Pipeline controller</text>
    <text class="sub" x="300" y="372">traffic police of the assembly line</text>
    <text class="lbl" x="300" y="390">stalled? <tspan class="val" id="v_st_id">—</tspan></text>
    <text class="lbl" x="300" y="406">reason: <tspan class="val" id="v_streason">—</tspan></text>
    <text class="lbl" x="300" y="422">redirecting PC? <tspan class="val" id="v_pcset">—</tspan></text>
   </g>
   <g class="blkg" data-info="regfile">
    <rect class="blk" x="290" y="222" width="180" height="100"/>
    <text class="ttl" x="300" y="240" style="font-size:11px">Register file</text>
    <text class="sub" x="300" y="252">32 fast storage slots</text>
    <text class="lbl" x="300" y="270">value read A:</text>
    <text class="val" id="v_rf_a" x="300" y="283">—</text>
    <text class="lbl" x="300" y="299">value read B:</text>
    <text class="val" id="v_rf_b" x="300" y="312">—</text>
   </g>
   <g class="blkg" data-info="alu">
    <rect class="blk" x="515" y="150" width="165" height="112"/>
    <text class="ttl" x="525" y="168" style="font-size:11px">ALU — the calculator</text>
    <text class="lbl" x="525" y="186">input A: <tspan class="val" id="v_alu_a">—</tspan></text>
    <text class="lbl" x="525" y="203">input B: <tspan class="val" id="v_alu_b">—</tspan></text>
    <text class="lbl" x="525" y="226">result:</text>
    <text class="val" id="v_alu_r" x="525" y="241" style="font-size:12px">—</text>
   </g>
   <g class="blkg" data-info="lsu" id="g_lsu">
    <rect class="blk" x="515" y="290" width="165" height="140"/>
    <text class="ttl" x="525" y="308" style="font-size:11px">Load/Store unit</text>
    <text class="sub" x="525" y="320">talks to main memory</text>
    <text class="lbl" x="525" y="338">accessing memory? <tspan class="val" id="v_dreq">—</tspan></text>
    <text class="lbl" x="525" y="354">address: <tspan class="val" id="v_daddr">—</tspan></text>
    <text class="lbl" x="525" y="370">writing: <tspan class="val" id="v_dwdata">—</tspan></text>
    <text class="lbl" x="525" y="386">read back: <tspan class="val" id="v_drdata">—</tspan></text>
    <text class="lbl" x="525" y="402">read arrived? <tspan class="val" id="v_drvalid">—</tspan></text>
   </g>
   <path class="wire" d="M470 166 C 495 166, 492 172, 515 176"/>
   <text class="sub" x="473" y="158">command</text>
   <path class="wire" d="M470 272 L515 200"/>
   <text class="sub" x="473" y="282">values</text>
   <path class="wire" d="M470 290 L515 330"/>
   <path class="wire" d="M680 205 L735 205"/>
   <text class="sub" x="683" y="198">result</text>
   <path class="wire" d="M680 330 C 708 330, 712 248, 738 245"/>
   <text class="sub" x="686" y="324">loaded data</text>

   <g class="blkg" data-info="wb">
    <rect class="blk" x="740" y="130" width="205" height="130"/>
    <text class="ttl" x="750" y="148" style="font-size:11px">Writeback</text>
    <text class="sub" x="750" y="160">the final step</text>
    <text class="lbl" x="750" y="180">saving a result? <tspan class="val" id="v_we">—</tspan></text>
    <text class="lbl" x="750" y="200">into register:</text>
    <text class="val" id="v_waddr" x="750" y="214">—</text>
    <text class="lbl" x="750" y="232">value being saved:</text>
    <text class="val" id="v_wdata" x="750" y="246" style="font-size:12px">—</text>
   </g>
   <path class="wire" d="M790 260 C 790 472, 420 474, 378 326"/>
   <text class="sub" x="560" y="474">result loops back into a register slot</text>
  </svg>
  <div id="card">
   <div id="cardhead"><b id="cardtitle"></b>
    <span id="cardclose" title="close (Esc)">✕</span></div>
   <div id="cardexpl"></div>
   <div id="cardbody"></div>
   <div id="cardwave"></div>
  </div>
  </div>
  <div id="blockinfo"></div>
  <div id="narr"></div>
  <div id="wavewrap"><h3>Signal timeline — the same data you'd see in a waveform viewer,
   zoomed to the cycles around your instruction (click a cycle number to jump there)</h3>
   <div id="wave"></div></div>
 </div>
 <div id="side">
  <div class="panel"><h3>Your C code — <span id="srcfile" style="color:#999;font-weight:400">?</span></h3>
   <div id="srccode"><span style="color:#999;font-size:11px">no source info for this instruction</span></div>
   <div class="phint">yellow = line in execute right now · blue outline = your selected
    instruction's line · click a line to jump to its first instruction</div></div>
  <div class="panel"><h3>Registers — the chip's 32 storage slots</h3>
   <div id="regs"></div>
   <div class="phint">green = written this cycle · blue = read by the executing
    instruction · click a register to jump to whoever last wrote it</div></div>
  <div class="panel"><h3>Console — what the program has printed so far</h3>
   <div id="cons"></div></div>
  <div class="panel"><h3>Interrupts &amp; special registers (CSRs)</h3>
   <table id="csrs" style="font-size:11px;width:100%"></table>
   <div class="phint">CSRs are the chip's settings and bookkeeping registers —
    they control interrupts and remember where to return to</div></div>
  <div class="panel"><h3>Memory map — where in the address space is the chip looking?</h3>
   <div id="memmap" style="display:flex;gap:3px"></div>
   <div id="memtxt" class="phint"></div></div>
 </div>
</div>

<script>
const INSTRS=__INSTRS__, SIGS=__SIGS__, META=__META__;
const SRC=__SRC__, P2L_ARR=__P2L__;
const ABI=META.abi;
const ROLE={zero:"always 0",ra:"return address",sp:"stack pointer",gp:"global ptr",
 tp:"thread ptr",s0:"saved",s1:"saved"};
for(let i=0;i<8;i++)ROLE["a"+i]="argument/result";
for(let i=2;i<12;i++)ROLE["s"+i]="saved";
for(let i=0;i<7;i++)ROLE["t"+i]="temporary";
const BLOCKINFO={
 pc:"Program Counter (PC) — the chip's bookmark. It holds the memory address of the instruction being read right now. After each instruction it moves forward (or jumps, for branches).",
 prefetch:"Prefetch buffer — a small waiting room. The chip reads instructions from memory a little ahead of time so the assembly line never has to wait for memory.",
 decoder:"Decoder — looks at the raw 16/32 bits of the instruction and figures out what it means: which operation (add? load? branch?) and which registers are involved.",
 regfile:"Register file — 32 tiny, ultra-fast storage slots inside the CPU (x0..x31). Reading is instant. Almost every instruction reads 1-2 registers and writes one back.",
 alu:"ALU (Arithmetic Logic Unit) — the chip's calculator. Adds, subtracts, compares, shifts. It computes one result per cycle from inputs A and B.",
 lsu:"Load/Store Unit — the chip's hands into main memory (RAM). Loads bring values from memory into a register; stores send register values out to memory. This block lights up only when the current instruction touches memory.",
 wb:"Writeback — the final assembly-line station. The computed (or loaded) value is saved into the register file, so later instructions can use it.",
 ctrl:"Pipeline controller — the traffic police. Every cycle it decides whether the assembly line can advance. If a result isn't ready (a load still in flight, a branch not yet decided), it freezes the line for a cycle: a stall. The signals here are the REAL stall reasons from inside the chip."};

const byCycle={}; INSTRS.forEach(e=>byCycle[e.c]=e);
let sel=INSTRS[META.defaultIdx], C=sel.c-2, timer=null;

// interrupt-entry detection: a non-sequential PC change that wasn't caused
// by a branch/jump and lands inside the handler region (mtvec .. mtvec+128)
INSTRS.forEach((e,i)=>{
 if(!i)return;
 const p=INSTRS[i-1];
 if(e.pc===p.pc+(p.hex.length<=4?2:4))return;
 if(p.cat==="branch")return;
 const tv=svRaw("mtvec",e.c);
 if(tv!=null&&e.pc>=tv&&e.pc<tv+128)e.irq=true;
});
function svRaw(key,c){const a=SIGS[key];if(!a)return null;
 const i=c-META.base;return(i>=0&&i<a.length)?a[i]:null;}

function sv(key,c){const a=SIGS[key];if(!a)return null;const i=c-META.base;
 return (i>=0&&i<a.length)?a[i]:null;}
function hx(v,w){return v==null?"—":"0x"+v.toString(16).padStart(w||1,"0");}
function fv(v){if(v==null)return"—";
 const s=v>0x7fffffff?v-0x100000000:v;
 if(s>-4096&&s<4096)return s+"  (0x"+v.toString(16)+")";
 return"0x"+v.toString(16).padStart(8,"0");}
function fbit(v,yes,no){return v==null?"—":(v?(yes||"1 (yes)"):(no||"0 (no)"));}
function regname(n){const a=ABI[n];return"x"+n+" ("+a+(ROLE[a]?" — "+ROLE[a]:"")+")";}
function esc(t){return t.replace(/&/g,"&amp;").replace(/</g,"&lt;");}

// ------------------------------------------------ instruction list
const list=document.getElementById("list");
INSTRS.forEach(e=>{
 const d=document.createElement("div");
 d.className="irow cat-"+e.cat; d.id="ir"+e.i;
 d.innerHTML="<div class='top'><span class='fn'>#"+(e.i+1)+" · "+esc(e.fn)+"</span>"+
   (e.irq?"<span style='color:#d63300;font-weight:700'>⚡ </span>":"")+
   esc(e.mn+" "+e.ops)+"</div><div class='dsc'>"+
   (e.irq?"INTERRUPT ENTRY — the timer hijacked the pipeline here. ":"")+esc(e.d)+"</div>";
 d.title="retires at cycle "+e.c+"\nPC 0x"+e.pc.toString(16);
 d.onclick=()=>{select(e); setCycle(e.c-2);};
 list.appendChild(d);
});

function select(e){
 sel=e;
 document.querySelectorAll(".irow.sel").forEach(x=>x.classList.remove("sel"));
 const row=document.getElementById("ir"+e.i);
 row.classList.add("sel");
 row.scrollIntoView({block:"nearest"});
 renderJourney(); drawMini(); saveHash();
}

// ------------------------------------------------ minimap
const mini=document.getElementById("mini");
function drawMini(){
 const W=mini.width=mini.clientWidth||900,H=mini.height;
 const ctx=mini.getContext("2d");ctx.clearRect(0,0,W,H);
 const col={alu:"#2e9e4f",mem:"#2b6cb0",branch:"#dd7711",sys:"#999"};
 const n=INSTRS.length;
 for(let i=0;i<n;i++){ctx.fillStyle=col[INSTRS[i].cat];
  ctx.fillRect(i*W/n,7,Math.max(1,W/n),H-9);}
 ctx.fillStyle="#333";
 for(let i=1;i<n;i++)if(INSTRS[i].fn!==INSTRS[i-1].fn)ctx.fillRect(i*W/n,3,1,H-3);
 ctx.fillStyle="#d63300";
 for(let i=0;i<n;i++)if(INSTRS[i].irq)ctx.fillRect(i*W/n-1,0,3,H);
 const x=sel.i*W/n;
 ctx.fillStyle="#e6b800";
 ctx.beginPath();ctx.moveTo(x-5,0);ctx.lineTo(x+5,0);ctx.lineTo(x,8);ctx.fill();}
function miniIdx(ev){const r=mini.getBoundingClientRect();
 return Math.max(0,Math.min(INSTRS.length-1,
  Math.floor((ev.clientX-r.left)/r.width*INSTRS.length)));}
mini.onclick=ev=>{const e=INSTRS[miniIdx(ev)];select(e);setCycle(e.c-2);};
mini.onmousemove=ev=>{const e=INSTRS[miniIdx(ev)];
 mini.title="#"+(e.i+1)+" in "+e.fn+"(): "+e.mn+" "+e.ops+"\n"+e.d;};
window.addEventListener("resize",drawMini);

// ------------------------------------------------ filter + search
const fnSel=document.getElementById("fnfilter");
[...new Set(INSTRS.map(e=>e.fn))].forEach(f=>{
 const o=document.createElement("option");o.value=f;
 o.textContent=f+"()";fnSel.appendChild(o);});
const hay=INSTRS.map(e=>(e.mn+" "+e.ops+" "+e.fn+" "+e.pc.toString(16)+" "+
 e.rd.concat(e.wr).map(w=>w[0]).join(" ")).toLowerCase());
function applyFilter(){
 const q=document.getElementById("search").value.trim().toLowerCase();
 const fn=fnSel.value;
 INSTRS.forEach(e=>{
  const ok=(!fn||e.fn===fn)&&(!q||hay[e.i].includes(q));
  document.getElementById("ir"+e.i).style.display=ok?"":"none";});}
document.getElementById("search").oninput=applyFilter;
fnSel.onchange=applyFilter;

// ------------------------------------------------ shareable URL state
let hashLock=false;
function saveHash(){if(hashLock)return;
 history.replaceState(null,"","#i="+sel.i+"&c="+C);}
function loadHash(){
 const m=location.hash.match(/i=(\d+)/),mc=location.hash.match(/c=(\d+)/);
 if(m){const e=INSTRS[Math.min(INSTRS.length-1,+m[1])];
  if(e){hashLock=true;select(e);
   setCycle(mc?+mc[1]:e.c-2);hashLock=false;return true;}}
 return false;}

function stageOf(c){ // which stage is the SELECTED instruction in at cycle c
 if(c===sel.c-2)return"IF"; if(c===sel.c-1)return"EX"; if(c===sel.c)return"WB";
 return null;}

function renderJourney(){
 const e=sel;
 let step3;
 if(e.wr.length)step3="result <b>"+fv(e.wr[0][1])+"</b> saved into "+regname(+e.wr[0][0].slice(1));
 else if(e.mem&&e.mem.type==="store")step3="data sent to memory — nothing to save in a register";
 else step3="no register result (branches/stores don't produce one)";
 const j=document.getElementById("journey");
 j.innerHTML="<b>You selected:</b> <span style='font-family:Consolas,monospace'>"+
  esc(e.mn+" "+e.ops)+"</span> — "+esc(e.d)+"<br>"+
  "<span class='jstep' id='js0'>1 · FETCHED from memory at cycle "+(e.c-2)+"</span> →"+
  "<span class='jstep' id='js1'>2 · DECODED &amp; EXECUTED at cycle "+(e.c-1)+"</span> →"+
  "<span class='jstep' id='js2'>3 · "+step3+" at cycle "+e.c+"</span>";
 document.getElementById("js0").onclick=()=>setCycle(e.c-2);
 document.getElementById("js1").onclick=()=>setCycle(e.c-1);
 document.getElementById("js2").onclick=()=>setCycle(e.c);
}

// ------------------------------------------------ narration
function describeStage(e,stage){
 if(!e)return null;
 const nm="<b style='font-family:Consolas,monospace'>"+esc(e.mn+" "+e.ops)+"</b>";
 if(stage==="IF")
  return"The <b>Fetch unit</b> is reading instruction "+nm+
   " from memory address 0x"+(e.pc).toString(16)+".";
 if(stage==="EX"){
  if(e.cat==="alu")return"The <b>ALU</b> (the chip's calculator) is working on "+nm+": "+esc(e.d)+".";
  if(e.cat==="mem")return"The <b>Load/Store unit</b> is talking to memory for "+nm+": "+esc(e.d)+".";
  if(e.cat==="branch")return"The chip is deciding whether to jump: "+nm+" — "+esc(e.d)+".";
  return"Executing "+nm+": "+esc(e.d)+".";}
 if(stage==="WB"){
  if(e.wr.length)return"Instruction "+nm+" is finishing: saving <b>"+fv(e.wr[0][1])+
   "</b> into register "+regname(+e.wr[0][0].slice(1))+".";
  return"Instruction "+nm+" is finishing (it has no register result to save).";}
}
function bubbleReason(c){
 // first ask the hardware itself: the controller's real stall signals
 const hw=[["st_ldhz","the next instruction needs a value that is still arriving from memory (a load-use hazard)"],
  ["st_mem","waiting for memory to answer"],
  ["st_br","a branch is being resolved"],
  ["st_jmp","a jump is being resolved"],
  ["st_mdiv","the multiplier/divider is still working"]];
 for(const[k,t]of hw)if(sv(k,c)===1||sv(k,c-1)===1)return t+" — this is the chip's own stall signal, not a guess";
 // fallback: infer from the most recent retired instruction
 for(let k=c;k>=c-6;k--){const p=byCycle[k];if(p){
  if(p.cat==="branch")return"the chip just jumped to a new address, so the instructions it had pre-fetched were thrown away (a 'pipeline flush')";
  if(p.mn==="wfi")return"the core is asleep (wfi) waiting for an interrupt";
  if(p.cat==="mem")return"waiting for memory to answer";
  return null;}}
 return null;}
function sleeping(c){ // true if the core is in a long wfi sleep around cycle c
 for(let k=c+1;k>=c-1;k--)if(byCycle[k])return false;
 for(let k=c;k>=Math.max(META.base,c-4000);k--){const p=byCycle[k];
  if(p)return p.mn==="wfi";}
 return false;}

function narrate(){
 const wb=byCycle[C],ex=byCycle[C+1],iff=byCycle[C+2];
 const parts=[];
 const sIF=describeStage(iff,"IF"),sEX=describeStage(ex,"EX"),sWB=describeStage(wb,"WB");
 const n=[sIF,sEX,sWB].filter(x=>x).length;
 let head="<b>Cycle "+C+":</b> ";
 if(n>1)head+=n+" things are happening at the same time — this is pipelining, the assembly line at work.";
 else if(n===1)head+="only one station is busy this cycle.";
 else if(sleeping(C))head+="the core is <b>asleep</b> (wfi — wait for interrupt). Its clock is "+
  "switched off to save power; it will wake the instant the timer interrupt fires.";
 else head+="the pipeline is empty this cycle.";
 parts.push(head);
 // interrupt story: any occupant that is an interrupt entry point
 const irqe=[iff,ex,wb].find(e=>e&&e.irq);
 if(irqe){const mepc=sv("mepc",C),mtv=sv("mtvec",C);
  parts.push("⚡ <b>INTERRUPT!</b> The timer fired and hijacked the pipeline: the core "+
   "dropped what it was doing, saved its place in <b>mepc</b>"+
   (mepc!=null?" (0x"+(mepc>>>0).toString(16)+")":"")+
   " and jumped to the handler"+(mtv!=null?" near <b>mtvec</b> 0x"+(mtv>>>0).toString(16):"")+
   ". When the handler finishes (mret), the core will resume exactly where it left off.");}
 const mret=[iff,ex,wb].find(e=>e&&e.mn==="mret");
 if(mret)parts.push("↩️ <b>mret</b> — the handler is done; the core jumps back to the "+
  "address saved in mepc and resumes the interrupted program.");
 if(sEX)parts.push("⚙️ "+sEX); else if(!sleeping(C)){
  const r=bubbleReason(C+1);
  parts.push("⚙️ The <b>Execute</b> station is empty (a 'bubble')"+(r?" — "+r+".":"."));}
 if(sIF)parts.push("📥 "+sIF);
 if(sWB)parts.push("💾 "+sWB);
 const here=stageOf(C);
 if(here)parts.push("📍 Your selected instruction is in the <b>"+
  ({IF:"FETCH",EX:"DECODE+EXECUTE",WB:"SAVE RESULT"})[here]+"</b> station right now (glowing box).");
 document.getElementById("narr").innerHTML=parts.join("<br>");
}

// ------------------------------------------------ diagram update
function put(id,t){const el=document.getElementById(id);
 if(el.textContent!==t){el.textContent=t;
  el.classList.remove("flash");el.getBoundingClientRect();el.classList.add("flash");}}
function setCycle(c){
 C=Math.max(META.base,Math.min(META.maxc,c));
 put("cyclelabel","cycle "+C);
 put("v_pc_if",hx(sv("pc_if",C),8));
 put("v_instr",hx(sv("instr",C),8));
 put("v_ivalid",fbit(sv("ivalid",C)));
 put("v_rf_a",fv(sv("rf_a",C)));
 put("v_rf_b",fv(sv("rf_b",C)));
 put("v_alu_a",fv(sv("alu_a",C)));
 put("v_alu_b",fv(sv("alu_b",C)));
 put("v_alu_r",fv(sv("alu_r",C)));
 put("v_we",fbit(sv("we",C),"1 (yes!)","0 (no)"));
 const wa=sv("waddr",C);
 put("v_waddr",wa==null?"—":regname(wa));
 put("v_wdata",fv(sv("wdata",C)));
 const dr=sv("dreq",C);
 put("v_dreq",fbit(dr,"1 (yes!)","0 (no)"));
 put("v_daddr",hx(sv("daddr",C),8));
 put("v_dwdata",hx(sv("dwdata",C),8));
 put("v_drdata",hx(sv("drdata",C),8));
 put("v_drvalid",fbit(sv("drvalid",C)));
 document.getElementById("g_lsu").style.opacity=(dr||sv("drvalid",C))?1:0.38;
 const ex=byCycle[C+1];
 put("d_mnem",ex?ex.mn+" "+ex.ops:"(nothing — bubble)");
 // pipeline controller block
 const stid=sv("st_id",C);
 put("v_st_id",fbit(stid,"1 (frozen)","0 (moving)"));
 const why=[["st_ldhz","load-use"],["st_mem","memory"],["st_br","branch"],
  ["st_jmp","jump"],["st_mdiv","mul/div"]]
  .filter(([k])=>sv(k,C)===1).map(([,t])=>t).join(", ");
 put("v_streason",stid?(why||"control"):"—");
 put("v_pcset",fbit(sv("pcset",C),"1 (jump!)","0"));
 if(openCard)renderCard();
 // occupancy chips
 const occ=(idTop,idSub,e)=>{put(idTop,e?e.mn+" "+e.ops:"— empty (bubble) —");
  put(idSub,e?"instruction #"+(e.i+1)+" · in "+e.fn+"()":"");};
 occ("occ_if","occ_if2",byCycle[C+2]);
 occ("occ_ex","occ_ex2",byCycle[C+1]);
 occ("occ_wb","occ_wb2",byCycle[C]);
 // glow
 const here=stageOf(C);
 ["st_if","st_ex","st_wb"].forEach(id=>document.getElementById(id).setAttribute("class","stage-box"));
 if(here==="IF")document.getElementById("st_if").setAttribute("class","stage-box stage-on-IF");
 if(here==="EX")document.getElementById("st_ex").setAttribute("class","stage-box stage-on-EX");
 if(here==="WB")document.getElementById("st_wb").setAttribute("class","stage-box stage-on-WB");
 narrate(); renderWave(); updateSide(); saveHash();
}

// ------------------------------------------------ signal timeline
const WAVEROWS=[
 ["address being fetched","pc_if",8],
 ["instr in execute — its address","pc_id",8],
 ["ALU input A","alu_a",0],["ALU input B","alu_b",0],["ALU result","alu_r",0],
 ["register write happening?","we",1],
 ["which register gets written","waddr",1],
 ["value being written","wdata",0],
 ["memory access happening?","dreq",1],
 ["memory address","daddr",8],
 ["data read from memory","drdata",8]];
function shortv(v,w){if(v==null)return"·";if(w===1)return""+v;
 if(w===8)return"0x"+v.toString(16);
 const s=v>0x7fffffff?v-0x100000000:v;
 return(s>-100000&&s<100000)?""+s:"0x"+v.toString(16);}
function buildWave(rows,lo,hi){
 let h="<table class='wave'><thead><tr><th>signal (plain name)</th>";
 for(let c=lo;c<=hi;c++)h+="<td class='"+(c===C?"cur":"")+"' onclick='setCycle("+c+")'>"+c+"</td>";
 h+="</tr></thead><tbody>";
 for(const[name,key,w]of rows){
  if(!SIGS[key])continue;
  h+="<tr><th>"+name+" <span style='color:#aaa'>("+key+")</span></th>";
  let prev=null;
  for(let c=lo;c<=hi;c++){const v=sv(key,c);
   const cls=(c===C?"cur ":"")+((c>lo&&v!==prev)?"chg":"");
   h+="<td class='"+cls+"'>"+shortv(v,w)+"</td>";prev=v;}
  h+="</tr>";}
 return h+"</tbody></table>";
}
function renderWave(){
 document.getElementById("wave").innerHTML=
  buildWave(WAVEROWS,Math.max(META.base,C-7),Math.min(META.maxc,C+4));
}

// ------------------------------------------------ program layer (Level 1)
// register state replay: cumulative snapshot after each retired instruction
const stateAfter=[];{let st=new Uint32Array(32);
 INSTRS.forEach(e=>{e.wr.forEach(w=>{const n=+w[0].slice(1);if(n)st[n]=w[1];});
  stateAfter.push(Uint32Array.from(st));});}
const retCycles=INSTRS.map(e=>e.c);
function lastRetIdx(c){let lo=0,hi=retCycles.length-1,r=-1;
 while(lo<=hi){const m=(lo+hi)>>1;if(retCycles[m]<=c){r=m;lo=m+1;}else hi=m-1;}return r;}
function regsAt(c){const i=lastRetIdx(c);return i<0?new Uint32Array(32):stateAfter[i];}
function lastWriter(n,c){for(let i=lastRetIdx(c);i>=0;i--)
 if(INSTRS[i].wr.some(w=>w[0]==="x"+n))return INSTRS[i];return null;}
function regShort(v){v=v>>>0;const s=v>0x7fffffff?v-0x100000000:v;
 return(s>-100000&&s<100000)?""+s:"0x"+v.toString(16);}
function regClick(n){const w=lastWriter(n,C);
 if(w){select(w);setCycle(w.c);}}
function updateRegs(){
 const st=regsAt(C),wb=byCycle[C],ex=byCycle[C+1];
 const wrs=new Set(wb?wb.wr.map(w=>+w[0].slice(1)):[]);
 const rds=new Set(ex?ex.rd.map(w=>+w[0].slice(1)):[]);
 let h="";
 for(let n=0;n<32;n++){
  const cls="reg"+(wrs.has(n)?" wr":"")+(rds.has(n)?" rd":"");
  h+="<div class='"+cls+"' onclick='regClick("+n+")' title='x"+n+" ("+ABI[n]+
   (ROLE[ABI[n]]?" — "+ROLE[ABI[n]]:"")+") = 0x"+(st[n]>>>0).toString(16)+
   "\nclick: jump to the instruction that last wrote it'>"+
   "<span class='rn'>x"+n+" "+ABI[n]+"</span><span class='rv'>"+regShort(st[n])+
   "</span></div>";}
 document.getElementById("regs").innerHTML=h;}

// C source panel
const P2L={};P2L_ARR.forEach(a=>P2L[a[0]]=[a[1],a[2]]);
let curSrcFile=-1;
function renderSrcFile(f){curSrcFile=f;
 const L=SRC[f].lines;let h="";
 for(let i=0;i<L.length;i++)
  h+="<div class='sline' id='sl"+(i+1)+"' onclick='srcClick("+(i+1)+")'>"+
   "<span class='ln'>"+(i+1)+"</span>"+esc(L[i])+"</div>";
 document.getElementById("srccode").innerHTML=h;
 document.getElementById("srcfile").textContent=SRC[f].name;}
function srcClick(l){
 const hit=INSTRS.find(e=>{const m=P2L[e.pc];
  return m&&m[0]===curSrcFile&&m[1]===l;});
 if(hit){select(hit);setCycle(hit.c-2);}}
function updateSrc(){
 const ex=byCycle[C+1];
 const exL=ex?P2L[ex.pc]:null, selL=P2L[sel.pc];
 const show=exL||selL;
 if(!show||!SRC[show[0]].lines)return;
 if(show[0]!==curSrcFile)renderSrcFile(show[0]);
 document.querySelectorAll(".sline.cur,.sline.selline")
  .forEach(x=>x.classList.remove("cur","selline"));
 if(exL&&exL[0]===curSrcFile){const el=document.getElementById("sl"+exL[1]);
  if(el){el.classList.add("cur");el.scrollIntoView({block:"nearest"});}}
 if(selL&&selL[0]===curSrcFile){const el=document.getElementById("sl"+selL[1]);
  if(el)el.classList.add("selline");}}

// console panel (stores to the UART address 0x20000)
const CONS=INSTRS.filter(e=>e.mem&&e.mem.type==="store"&&e.mem.pa===0x20000)
 .map(e=>[e.c,e.mem.val&0xff]);
function updateConsole(){let s="";
 for(const[c,v]of CONS){if(c>C)break;s+=String.fromCharCode(v);}
 const el=document.getElementById("cons");
 el.textContent=s+"▌";el.scrollTop=el.scrollHeight;}

// CSR / interrupt panel
function updateCsr(){
 const rows=[
  ["timer interrupt line","irq_timer",v=>fbit(v,"1 — RINGING","0 — quiet")],
  ["interrupt waiting to be served","irq_pend",v=>fbit(v,"1 — yes!","0 — no")],
  ["interrupts allowed (mstatus.MIE)","mie",v=>fbit(v,"1 — yes","0 — blocked")],
  ["handler address (mtvec)","mtvec",v=>hx(v==null?null:v>>>0,8)],
  ["saved return point (mepc)","mepc",v=>hx(v==null?null:v>>>0,8)]];
 let h="";
 for(const[name,key,f]of rows){
  if(!SIGS[key])continue;
  h+="<tr><td style='color:#555;padding:1px 6px 1px 0'>"+name+
   " <span style='color:#bbb'>("+key+")</span></td><td style='font-family:Consolas,monospace;font-weight:600' id='csr_"+key+"'>"+
   f(sv(key,C))+"</td></tr>";}
 document.getElementById("csrs").innerHTML=h;}

// memory map panel
const MEMREGIONS=[
 [0x100000,0x200000,"RAM — your program + data","#cde3ff"],
 [0x20000,0x20008,"console output","#d6f5d6"],
 [0x20008,0x20010,"simulator halt","#f3d1e3"],
 [0x30000,0x30010,"timer","#ffe9b3"]];
function updateMemmap(){
 const a=sv("daddr",C),act=sv("dreq",C)===1||sv("drvalid",C)===1;
 let h="",txt="the data bus is idle this cycle";
 for(const[lo2,hi2,name,col]of MEMREGIONS){
  const hit=act&&a!=null&&(a>>>0)>=lo2&&(a>>>0)<hi2;
  h+="<div style='flex:1;text-align:center;font-size:9.5px;padding:5px 2px;border-radius:5px;"+
   "background:"+(hit?"#1d6ed8":col)+";color:"+(hit?"#fff":"#333")+
   (hit?";font-weight:700":"")+"'>"+name+"</div>";
  if(hit)txt="accessing 0x"+(a>>>0).toString(16)+" — "+name;}
 document.getElementById("memmap").innerHTML=h;
 document.getElementById("memtxt").textContent=txt;}

function updateSide(){updateRegs();updateSrc();updateConsole();updateCsr();updateMemmap();}

// ------------------------------------------------ block drill-down cards
let openCard=null;
function bin(w,hi2,lo2){let s="";for(let b=hi2;b>=lo2;b--)s+=(w>>>b)&1;return s;}
function fieldbox(bits,label,color){
 return"<span class='bf' style='background:"+color+";flex:"+bits.length+
  "'>"+bits+"<span class='bfl'>"+label+"</span></span>";}
const FMT={0x33:["R","register-register ALU operation"],
 0x13:["I","ALU operation with a constant (immediate)"],
 0x03:["I","load from memory"],0x23:["S","store to memory"],
 0x63:["B","conditional branch"],0x37:["U","load upper immediate"],
 0x17:["U","PC-relative address"],0x6f:["J","jump and link"],
 0x67:["I","jump via register"],0x73:["SYS","system / CSR access"]};
const FC={op:"#cde3ff",rd:"#d6f5d6",f3:"#fff1c2",rs1:"#ffd9c2",
 rs2:"#e8d9ff",imm:"#f3d1e3",f7:"#e6e6e6"};
function decode32(w){
 const opc=w&0x7f,rd=(w>>>7)&31,rs1=(w>>>15)&31,rs2=(w>>>20)&31;
 const fi=FMT[opc]||["?","unrecognized format"];const fmt=fi[0];
 let r="<div class='bitrow'>";
 if(fmt==="R")r+=fieldbox(bin(w,31,25),"funct7",FC.f7)+fieldbox(bin(w,24,20),"rs2",FC.rs2)+
  fieldbox(bin(w,19,15),"rs1",FC.rs1)+fieldbox(bin(w,14,12),"funct3",FC.f3)+
  fieldbox(bin(w,11,7),"rd",FC.rd)+fieldbox(bin(w,6,0),"opcode",FC.op);
 else if(fmt==="I"||fmt==="SYS")r+=fieldbox(bin(w,31,20),"imm[11:0]",FC.imm)+
  fieldbox(bin(w,19,15),"rs1",FC.rs1)+fieldbox(bin(w,14,12),"funct3",FC.f3)+
  fieldbox(bin(w,11,7),"rd",FC.rd)+fieldbox(bin(w,6,0),"opcode",FC.op);
 else if(fmt==="S"||fmt==="B")r+=fieldbox(bin(w,31,25),"imm",FC.imm)+
  fieldbox(bin(w,24,20),"rs2",FC.rs2)+fieldbox(bin(w,19,15),"rs1",FC.rs1)+
  fieldbox(bin(w,14,12),"funct3",FC.f3)+fieldbox(bin(w,11,7),"imm",FC.imm)+
  fieldbox(bin(w,6,0),"opcode",FC.op);
 else if(fmt==="U"||fmt==="J")r+=fieldbox(bin(w,31,12),"imm[31:12]",FC.imm)+
  fieldbox(bin(w,11,7),"rd",FC.rd)+fieldbox(bin(w,6,0),"opcode",FC.op);
 else r+=fieldbox(bin(w,31,0),"raw bits",FC.f7);
 r+="</div><ul style='margin:4px 0;padding-left:18px'>";
 r+="<li>format <b>"+fmt+"</b> — "+fi[1]+"</li>";
 if(["R","I","SYS","U","J"].includes(fmt)&&!(fmt==="SYS"&&rd===0))
  r+="<li><span style='background:"+FC.rd+"'>rd</span> (destination) = "+regname(rd)+"</li>";
 if(["R","I","S","B","SYS"].includes(fmt))
  r+="<li><span style='background:"+FC.rs1+"'>rs1</span> (source A) = "+regname(rs1)+"</li>";
 if(["R","S","B"].includes(fmt))
  r+="<li><span style='background:"+FC.rs2+"'>rs2</span> (source B) = "+regname(rs2)+"</li>";
 if(fmt==="I"){let v=w>>>20;if(v&0x800)v-=4096;
  r+="<li><span style='background:"+FC.imm+"'>imm</span> (constant) = "+v+"</li>";}
 if(fmt==="S"){let v=((w>>>25)<<5)|((w>>>7)&31);if(v&0x800)v-=4096;
  r+="<li><span style='background:"+FC.imm+"'>imm</span> (address offset) = "+v+"</li>";}
 if(fmt==="B"||fmt==="J")
  r+="<li><span style='background:"+FC.imm+"'>imm</span> (jump distance) — its bits are "+
   "scrambled across the word; a hardware trick that keeps the wiring simple</li>";
 return r+"</ul>";}
function cardDecoder(){
 const e=byCycle[C+1];
 if(!e)return"<i>Nothing is in the decode/execute stage this cycle (a bubble).</i>";
 let h="<div style='font-family:Consolas,monospace'><b>"+esc(e.mn+" "+e.ops)+
  "</b> — encoding 0x"+e.hex+"</div>";
 if(e.hex.length<=4){
  h+="<p style='margin:6px 0'>This is a <b>compressed</b> (16-bit) instruction — "+
   "half-size to save program memory. The chip expands it to the full 32-bit form "+
   "before decoding. The expanded word on the wires right now:</p>";
  const d=sv("instr",C);
  if(d!=null)h+="<div style='font-family:Consolas,monospace;margin-bottom:2px'>0x"+
   (d>>>0).toString(16).padStart(8,"0")+"</div>"+decode32(d>>>0);
 }else h+=decode32(parseInt(e.hex,16)>>>0);
 const op=sv("alu_op",C);
 if(op!=null&&META.aluops[op]!=null)
  h+="<p>decoder's command to the ALU: <b>"+META.aluops[op]+"</b></p>";
 return h;}
function handshakeText(req,gnt,rv){
 if(req==null&&rv==null)return"";
 if(req&&gnt)return"📡 request accepted by memory THIS cycle";
 if(req)return"📡 asking memory, waiting for it to accept";
 if(rv)return"📨 data arriving from memory THIS cycle";
 return"bus idle";}
function cardFetch(){
 return"<p>The fetch unit talks to memory over a simple bus protocol (OBI): it raises "+
  "<b>req</b> to ask, memory answers <b>gnt</b> (accepted) and later <b>rvalid</b> "+
  "(here's your data).</p><p><b>"+
  handshakeText(sv("ireq",C),sv("ignt",C),sv("irvalid",C))+"</b></p>";}
function cardLsu(){
 const e=byCycle[C+1];
 let h="<p>Same bus protocol as fetch, but for data. Loads pull values in, stores push "+
  "values out.</p><p><b>"+handshakeText(sv("dreq",C),sv("dgnt",C),sv("drvalid",C))+"</b></p>";
 if(e&&e.mem)h+="<p>current instruction: "+esc(e.d)+"</p>";
 return h;}
function cardAlu(){
 const op=sv("alu_op",C),e=byCycle[C+1];
 let h="";
 if(op!=null&&META.aluops[op]!=null)
  h+="<p>command: <b>"+META.aluops[op]+"</b></p>";
 h+="<p style='font-family:Consolas,monospace'>"+fv(sv("alu_a",C))+" · "+
  fv(sv("alu_b",C))+" → <b>"+fv(sv("alu_r",C))+"</b></p>";
 if(e&&e.cat==="branch"){const b=sv("brdec",C);
  if(b!=null)h+="<p>branch decision: <b>"+(b?"TAKEN — the PC will jump":
   "not taken — carry on to the next instruction")+"</b></p>";}
 return h;}
function cardCtrl(){
 const rs=[["st_ldhz","waiting for a load result (load-use hazard)"],
  ["st_mem","waiting for memory to answer"],["st_br","resolving a branch"],
  ["st_jmp","resolving a jump"],["st_mdiv","multiply/divide still working"]];
 const on=rs.filter(([k])=>sv(k,C)===1).map(([,t])=>t);
 let h="<p>The controller decides every cycle whether the assembly line can move. "+
  "If something isn't ready, it freezes the line (a <b>stall</b>).</p>";
 h+=sv("st_id",C)?"<p>⛔ stalled right now: <b>"+(on.join(", ")||"control reason")+"</b></p>"
  :"<p>✅ not stalled — the line is moving</p>";
 if(sv("pcset",C))h+="<p>🔀 the PC is being redirected this cycle (jump, branch or interrupt)</p>";
 return h;}
const CARDS={
 pc:{title:"Program Counter / Fetch",body:cardFetch,
  sigs:[["address being fetched","pc_if",8],["asking memory? (req)","ireq",1],
   ["memory accepted? (gnt)","ignt",1],["instruction arrived? (rvalid)","irvalid",1],
   ["bus fetch address","iaddr",8]]},
 prefetch:{title:"Prefetch buffer",body:null,
  sigs:[["instr handed to decoder","instr",8],["valid?","ivalid",1],
   ["asking memory? (req)","ireq",1],["instruction arrived? (rvalid)","irvalid",1]]},
 decoder:{title:"Decoder — how bits become behavior",body:cardDecoder,
  sigs:[["raw instruction word","instr",8],["was it compressed?","is_comp",1],
   ["ALU command number","alu_op",1]]},
 regfile:{title:"Register file",body:null,
  sigs:[["read port A value","rf_a",0],["read port B value","rf_b",0],
   ["write happening?","we",1],["write register #","waddr",1],["write value","wdata",0]]},
 alu:{title:"ALU — the calculator",body:cardAlu,
  sigs:[["input A","alu_a",0],["input B","alu_b",0],["result","alu_r",0],
   ["branch taken?","brdec",1]]},
 lsu:{title:"Load/Store unit — the memory bus",body:cardLsu,
  sigs:[["asking memory? (req)","dreq",1],["accepted? (gnt)","dgnt",1],
   ["write? (we)","dwe",1],["address","daddr",8],["data out (store)","dwdata",8],
   ["data in (load)","drdata",8],["load arrived? (rvalid)","drvalid",1]]},
 wb:{title:"Writeback",body:null,
  sigs:[["write happening?","we",1],["which register","waddr",1],["value","wdata",0]]},
 ctrl:{title:"Pipeline controller",body:cardCtrl,
  sigs:[["stalled?","st_id",1],["load-use hazard","st_ldhz",1],
   ["memory wait","st_mem",1],["branch resolving","st_br",1],
   ["jump resolving","st_jmp",1],["mul/div busy","st_mdiv",1],
   ["PC redirected","pcset",1]]},
};
function renderCard(){
 const c=CARDS[openCard];if(!c)return;
 document.getElementById("cardtitle").textContent=c.title;
 document.getElementById("cardexpl").textContent=BLOCKINFO[openCard]||"";
 document.getElementById("cardbody").innerHTML=c.body?c.body():"";
 const lo=Math.max(META.base,C-5),hi=Math.min(META.maxc,C+4);
 document.getElementById("cardwave").innerHTML=
  "<div style='font-size:10px;color:#888;margin-bottom:2px'>this block's own signals "+
  "around now (a private little waveform):</div>"+buildWave(c.sigs,lo,hi);
 document.getElementById("card").style.display="block";
 document.getElementById("diag").classList.add("dimmed");
 document.querySelectorAll(".blkg.spot").forEach(g=>g.classList.remove("spot"));
 const g=document.querySelector(".blkg[data-info='"+openCard+"']");
 if(g)g.classList.add("spot");}
function closeCard(){openCard=null;
 document.getElementById("card").style.display="none";
 document.getElementById("diag").classList.remove("dimmed");
 document.querySelectorAll(".blkg.spot").forEach(g=>g.classList.remove("spot"));}
document.getElementById("cardclose").onclick=closeCard;
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeCard();});
document.querySelectorAll(".blkg").forEach(g=>{
 g.addEventListener("click",()=>{openCard=g.dataset.info;renderCard();});
 const t=document.createElementNS("http://www.w3.org/2000/svg","title");
 t.textContent=(BLOCKINFO[g.dataset.info]||"")+"\n(click to open this block)";
 g.appendChild(t);
});

// ------------------------------------------------ controls
document.getElementById("bprev").onclick=()=>setCycle(C-1);
document.getElementById("bnext").onclick=()=>setCycle(C+1);
document.getElementById("bplay").onclick=function(){
 if(timer){clearInterval(timer);timer=null;this.textContent="▶ play";return;}
 this.textContent="⏸ pause";
 timer=setInterval(()=>{if(C>=META.maxc){clearInterval(timer);timer=null;
   document.getElementById("bplay").textContent="▶ play";return;}
  setCycle(C+1);},800);};
document.addEventListener("keydown",e=>{
 if(e.key==="ArrowLeft")setCycle(C-1);
 if(e.key==="ArrowRight")setCycle(C+1);});

// verification badge
(function(){const v=META.verify,b=document.getElementById("vbadge");
 if(!v||!v.checked){b.style.display="none";return;}
 if(v.passed===v.checked){b.textContent="✔ self-verified: "+v.passed+"/"+v.checked+
   " register writes cross-checked against the waveform";
  b.style.background="#1e7e34";b.style.color="#fff";}
 else{b.textContent="⚠ "+v.passed+"/"+v.checked+" writes match the waveform — see generator output";
  b.style.background="#b02a37";b.style.color="#fff";}})();

if(!loadHash()){select(sel); setCycle(sel.c-2);}
</script></body></html>
"""

if __name__ == "__main__":
    main()
