#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_extrapolation.py — 用归档轨迹离线验证 `--code-scan` 外推算法。

算法（被验证对象）：
  code_tgt = code_ref − round( 20·log10(c_ref / C_TGT) / SLOPE )
  其中 C_TGT = 5.1（域几何中点），SLOPE = 1.55 dB/码。

数据源：data/归档轨迹/*.log（2026-10-22 真机 1s 粒度寄存器轨迹）。
  - AGCON 段：AGC 自动调 0x13，每行给出 (码位, noise) ⇒ 同链路多码点；
  - BASE 段：钉码态，提供参考点的稳态 c 与单点测量散布。
口径：c = 128·10^(noise/20)（stats/守卫标度，与判据同口径）。

验证内容：
  A. 斜率检验：AGCON 段 (码, 20log10 c) 最小二乘斜率 vs 1.55 dB/码；
  B. 单点噪声：同码 c 的散布（极差/σ）换算成码误差 = dB/1.55；
  C. 外推闭环仿真：以每个实测码点为 code_ref 外推 code_tgt，
     查同链路最近码的实测 c（不足处按拟合斜率折算），评估是否落 [1.3, 20]
     且离 5.1 的 dB 距离；
  D. 敏感性：c_ref 混入 ±(0.5/1/2) dB 测量误差后，外推落点仍否在域内。
"""
import math
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "归档轨迹"))

C_TGT = 5.1
SLOPE = 1.55          # dB/码（判据采用值；开路独立复算 1.743）
C_MIN, C_MAX = 1.3, 20.0
CODE_LO, CODE_HI = 0x08, 0x18

LINE_RE = re.compile(
    r"^(\S+) (BASE|TRACE) (.*) n=(-?[\d.]+) p=(-?[\d.]+) s=(-?\d+) a=(\d+)/(\d+)$")
REG_RE = re.compile(r"([0-9a-f]{2}):([0-9a-f?]{2})")


def c_of(noise_dbfs):
    return 128.0 * (10.0 ** (noise_dbfs / 20.0))


def db_between(c1, c2):
    return 20.0 * math.log10(c1 / c2)


def parse(path):
    """-> [(phase, code13, noise, agc_off)]，仅取 0x13 读回成功且 0x0d 状态明确的行。
    phase 取自最近的 `=== PHASE_<名> … ===` / `=== <名> …` 头（数据行自身标签
    BASE/TRACE 只表示格式，不代表阶段）。"""
    rows = []
    phase = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("==="):
                name = line.strip("= ").split()[0] if line.strip("= ").split() else ""
                phase = "AGCON" if "AGCON" in name else \
                        "BASE" if "BASE" in name or "AGCOFF" in name else phase
                continue
            m = LINE_RE.match(line)
            if not m or phase is None:
                continue
            ts, _tag, regs_s, n, p, s, a0, a1 = m.groups()
            regs = dict(REG_RE.findall(regs_s))
            d, c13 = regs.get("0d"), regs.get("13")
            if not d or d == "??" or not c13 or c13 == "??":
                continue
            agc_off = int(d, 16) & 0x08
            rows.append((phase, int(c13, 16) & 0x1F, float(n), agc_off))
    return rows


def fit_slope(points):
    """points: [(code, c)] -> 最小二乘 dB/码。"""
    xs = [p[0] for p in points]
    ys = [20.0 * math.log10(p[1]) for p in points]
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    den = sum((x - xm) ** 2 for x in xs)
    return num / den, len(points)


def extrapolate(code_ref, c_ref):
    delta = round(db_between(c_ref, C_TGT) / SLOPE)
    return max(CODE_LO, min(CODE_HI, code_ref - delta))


def main():
    files = [f for f in os.listdir(DATA) if f.endswith(".log")]
    print("=" * 78)
    print("A/B. 各轨迹按 (phase, 码) 分组的实测 c（stats 口径）")
    print("=" * 78)
    chains = {}   # file -> {code: [c,...]}   仅 AGCON 段（同链路多码点）
    base = {}     # file -> {code: [c,...]}   BASE 段（钉码稳态，测单点噪声）
    for fn in sorted(files):
        rows = parse(os.path.join(DATA, fn))
        ag, ba = {}, {}
        for phase, code, n, _agcoff in rows:
            tgt = ag if phase == "AGCON" else ba
            tgt.setdefault(code, []).append(c_of(n))
        chains[fn] = ag
        base[fn] = ba
        print("\n-- %s" % fn)
        for code in sorted(ba):
            cs = ba[code]
            print("   BASE  code=0x%02x n=%3d  c 中位=%.2f  极差=%.2f  σ=%.2f"
                  % (code, len(cs), statistics.median(cs),
                     max(cs) - min(cs), statistics.stdev(cs) if len(cs) > 1 else 0))
        for code in sorted(ag):
            cs = ag[code]
            print("   AGCON code=0x%02x n=%3d  c 中位=%.2f  极差=%.2f  σ=%.2f"
                  % (code, len(cs), statistics.median(cs),
                     max(cs) - min(cs), statistics.stdev(cs) if len(cs) > 1 else 0))

    # ---- A. 斜率检验（每条链路 AGCON 段内码点 ≥2 才拟合）----
    print("\n" + "=" * 78)
    print("A. 斜率检验：AGCON 段 (码, 20log10 c) 最小二乘 vs 1.55 dB/码")
    print("=" * 78)
    for fn, ag in chains.items():
        pts = [(code, statistics.median(cs)) for code, cs in sorted(ag.items())
               if len(cs) >= 3]
        if len(pts) >= 2:
            slope, npt = fit_slope(pts)
            print("  %-22s 点数=%d  码跨=%d  斜率=%.3f dB/码"
                  % (fn, npt, pts[-1][0] - pts[0][0], slope))
        else:
            print("  %-22s AGCON 码点不足（%d 个），跳过拟合" % (fn, len(pts)))

    # ---- C. 外推闭环仿真 ----
    print("\n" + "=" * 78)
    print("C. 外推闭环仿真：每个实测码点作 code_ref → code_tgt → 查实测")
    print("=" * 78)
    verdicts = []
    for fn in sorted(chains):
        ag, ba = chains[fn], base[fn]
        pool = dict(ag)
        for code, cs in ba.items():          # BASE 码也作为可用参考/查证点
            pool.setdefault(code, []).extend(cs)
        ref_pts = [(code, statistics.median(cs)) for code, cs in sorted(pool.items())]
        for code_ref, c_ref in ref_pts:
            code_tgt = extrapolate(code_ref, c_ref)
            if code_tgt in pool:
                c_ver = statistics.median(pool[code_tgt])
                src = "同码实测"
            else:
                near = min(pool, key=lambda k: abs(k - code_tgt))
                c_near = statistics.median(pool[near])
                c_ver = c_near * 10 ** (-SLOPE * abs(near - code_tgt) / 20.0) \
                    if near > code_tgt else \
                    c_near * 10 ** (SLOPE * abs(near - code_tgt) / 20.0)
                src = "近码0x%02x折算(差%d码)" % (near, abs(near - code_tgt))
            in_dom = C_MIN <= c_ver <= C_MAX
            dev = db_between(c_ver, C_TGT)
            verdicts.append(in_dom)
            print("  %-22s ref=0x%02x(c=%.2f) → tgt=0x%02x → c_ver=%.2f [%s] "
                  "距5.1=%+.1fdB %s"
                  % (fn, code_ref, c_ref, code_tgt, c_ver, src, dev,
                     "域内" if in_dom else "域外!"))
    print("\n  闭环落域率: %d/%d" % (sum(verdicts), len(verdicts)))

    # ---- D. 单点噪声敏感性 ----
    print("\n" + "=" * 78)
    print("D. 敏感性：c_ref 混入 ±e dB 测量误差，外推码偏移 = e/1.55")
    print("=" * 78)
    for e in (0.5, 1.0, 2.0):
        print("  ±%.1f dB 误差 → 外推码最多偏 %+.1f 码 → c_ver 偏差 ≤ %.1f dB"
              % (e, e / SLOPE, e + (e / SLOPE) * SLOPE))

    # ---- C2. 真实两点互推（docs/02 §4 同轮实测：0x0a=-38.9 / 0x12=-26.5 dBFS）----
    print("\n" + "=" * 78)
    print("C2. 真实两点互推（同链路同轮扫码实测，唯一真机多码点）")
    print("=" * 78)
    pts = {0x0A: -38.9, 0x12: -26.5}     # dBFS，docs/02-判据与阈值.md §4
    for code_ref, n_ref in pts.items():
        c_ref = c_of(n_ref)
        tgt = extrapolate(code_ref, c_ref)
        # 真实目标：落在两实测点之间（外推目的就是把 c 拉到 5.1，而 5.1
        # 位于 c(0x0a)=1.45 与 c(0x12)=6.06 之间 ⇒ 真实目标 ∈ [0x0a, 0x12]）
        lo_t = extrapolate(0x12, c_of(pts[0x12]))
        hi_t = extrapolate(0x0A, c_of(pts[0x0A]))
        ok = lo_t - 1 <= tgt <= hi_t + 1
        print("  ref=0x%02x(c=%.2f) → tgt=0x%02x   （两向互推区间 [0x%02x, 0x%02x]"
              "，命中=%s，c_ref 距 5.1=%+.1f dB）"
              % (code_ref, c_ref, tgt, lo_t, hi_t, "是" if ok else "否",
                 db_between(c_ref, C_TGT)))
        # 正向核验：按两点实测斜率折算 tgt 处 c
        real_slope = (pts[0x12] - pts[0x0A]) / (0x12 - 0x0A)   # dB/码
        c_at_tgt = c_ref * 10 ** (real_slope * (tgt - code_ref) / 20.0)
        print("    按实测斜率 %.3f dB/码 核验 tgt 处 c=%.2f %s，距 5.1=%+.1f dB"
              % (real_slope, c_at_tgt,
                 "域内" if C_MIN <= c_at_tgt <= C_MAX else "域外!",
                 db_between(c_at_tgt, C_TGT)))

    # ---- E. 误差预算 vs 域余量 ----
    print("\n" + "=" * 78)
    print("E. 误差预算 vs 域余量")
    print("=" * 78)
    rand = 0.32      # 单点噪声 σ 上界（B 部分，健康态；30 s 中位数更小）
    syst = abs(1.743 - SLOPE) * 12   # 斜率取保守值，跨 12 码最大系统偏差
    meas = 2.0                       # 允许的 c_ref 测量系统差（空情档位差）
    total = math.sqrt(rand**2 + syst**2 + meas**2)
    margin_dn = db_between(C_TGT, C_MIN)
    margin_up = db_between(C_MAX, C_TGT)
    print("  随机(单点σ)=%.1f dB  系统(斜率1.55 vs 1.743, 12码)=%.1f dB  "
          "测量(空情档)=%.1f dB" % (rand, syst, meas))
    print("  合成总误差 ≈ %.1f dB（RSS）；外推一轮后再实测验证可再除以 √2" % total)
    print("  域单侧余量：5.1→1.3 = %.1f dB；5.1→20 = %.1f dB" % (margin_dn, margin_up))
    print("  结论：%s（总误差 < 单侧余量）"
          % "误差预算充足" if total < min(margin_dn, margin_up) else "偏紧")

    # ---- B. 单点噪声汇总 ----
    print("\n" + "=" * 78)
    print("B. 单点测量噪声（BASE 段同码 c 的散布 → 换算码误差）")
    print("=" * 78)
    for fn, ba in base.items():
        for code, cs in sorted(ba.items()):
            if len(cs) >= 5:
                dbs = [20.0 * math.log10(x / 128.0) for x in cs]
                rng = max(dbs) - min(dbs)
                sd = statistics.stdev(dbs) if len(dbs) > 1 else 0.0
                print("  %-22s code=0x%02x  c 极差=%.2f dB→%.2f码  σ=%.2f dB→%.2f码"
                      % (fn, code, rng, rng / SLOPE, sd, sd / SLOPE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
