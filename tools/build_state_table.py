#!/usr/bin/env python3
"""build_state_table.py — 从归档的 1s 粒度轨迹统计各工作点的实测分布。

用途：为 FC0013 守卫（fc0013_guard.py）确定判据阈值，不靠拍脑袋。
数据源：reg_trace_D84/D85.log、conv_A/B.log（每行 = 22 寄存器 + n/p/s/a）。

字段口径（见 reg_trace.sh）：
  n = readsb noise(dBFS)   p = peak_signal(dBFS)
  s = strong_signals       a = accepted[0]/accepted[1]
  0d: bit3=1 → AGC OFF(0x09)；bit3=0 → AGC ON(0x01)
  13: IF 增益码

控制量：c_rms = 128 * 10^(noise/20)  —— 8bit 满量程码值（readsb 标度）
"""
import os
import sys
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
# 归档 1s 轨迹（reg_trace_D84/D85、conv_A/B），随项目分发在 data/归档轨迹/
DATA_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "归档轨迹"))


def parse(path):
    """返回 [(phase, ts, regs{}, n, p, s, acc_now, acc_total)]"""
    out = []
    phase = "?"
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("=== PHASE_AGCON") or line.startswith("=== AGCON"):
                phase = "AGCON"
                continue
            if line.startswith("=== PHASE_") or line.startswith("=== AGCOFF"):
                phase = "BASE"
                continue
            tok = line.split()
            if len(tok) < 6 or tok[1] not in ("BASE", "TRACE"):
                continue
            ts = tok[0]
            regs = OrderedDict()
            bad = False
            for pair in tok[2:24]:
                if ":" not in pair:
                    bad = True
                    break
                r, v = pair.split(":")
                try:
                    regs[int(r, 16)] = int(v, 16)
                except ValueError:
                    bad = True
                    break
            if bad:
                continue
            kv = {}
            for t in tok[24:]:
                if "=" in t:
                    k, v = t.split("=", 1)
                    kv[k] = v
            try:
                n = float(kv["n"])
                p = float(kv["p"])
                s = int(kv["s"])
                acc = [int(x) for x in kv["a"].split("/")]
            except (KeyError, ValueError):
                continue
            out.append((phase, ts, regs, n, p, s, acc))
    return out


def crs(n):
    return 128.0 * (10.0 ** (n / 20.0))


def stat(vals):
    if not vals:
        return "n/a"
    v = sorted(vals)
    return "min=%.2f p50=%.2f max=%.2f" % (v[0], v[len(v) // 2], v[-1])


def main():
    files = [
        ("D84", "reg_trace_D84.log"),
        ("D85", "reg_trace_D85.log"),
        ("CONVA", "conv_A.log"),
        ("CONVB", "conv_B.log"),
    ]
    for tag, fn in files:
        p = os.path.join(DATA_DIR, fn)
        if not os.path.exists(p):
            print("MISSING", fn)
            continue
        rows = parse(p)
        print("=" * 78)
        print("%s  (%s)  rows=%d" % (tag, fn, len(rows)))
        for phase in ("BASE", "AGCON"):
            sub = [r for r in rows if r[0] == phase]
            if not sub:
                continue
            ns = [r[3] for r in sub]
            ps = [r[4] for r in sub]
            cs = [crs(r[3]) for r in sub]
            ss = [r[5] for r in sub]
            ac = [r[6][0] for r in sub]
            d13 = sorted(set(r[2].get(0x13, -1) for r in sub))
            d0d = sorted(set(r[2].get(0x0D, -1) for r in sub))
            span = "%s..%s" % (sub[0][1], sub[-1][1])
            print("  [%s] %s  n=%d" % (phase, span, len(sub)))
            print("      0d=%s  13=%s" % (d0d, [hex(x) for x in d13]))
            print("      noise  %s" % stat(ns))
            print("      c_rms  %s" % stat(cs))
            print("      peak   %s   hd(min)=%.1f" % (stat(ps), min([-x for x in ps])))
            print("      strong %s" % stat(ss))
            print("      acc/min %s" % stat(ac))


if __name__ == "__main__":
    main()
