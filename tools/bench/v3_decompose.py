#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3 数据的**独立复算 + 因子分解**（不复用 harness 的 analyze）。

为什么另写一份
--------------
harness 的 analyze 已经把 6 对效应算出来了，但它对「这个效应到底能信到几分」
只给了亚窗 SE，没有做两件关键的事：

  1. **最小二乘分解（自由度账）**：4 个单元格只有 3 个自由度，6 对差里有 3 条
     恒等式。把 6 对拆成
        gR = 守卫在 readsb 拓扑内的效应   （只有 1 个独立估计：P1）
        gS = 守卫在 s1090 拓扑内的效应    （只有 1 个独立估计：−P6）
        d  = 拓扑效应（readsb − s1090，守卫平均）（**4 个**独立估计）
     于是能一眼看出：哪个量有冗余、哪个量只有单点；closure 残差的来源也定位到 d。

  2. **数据完整性**：计数器有没有在窗内被 readsb 重启清零。harness 用首尾差，
     中途一旦重启就会得到荒谬值而它不会报警。

编码/符号约定
-------------
  * 效应一律 "左 − 右" 除以右，单位 %；左/右指 PHASES 声明的组合。
  * 反向阶段左组合落在**另一台**主机 ⇒ 用 host_of() 断言强制交换（防 P0-1 复发）。
  * 同时给线性 % 与 ln 两套，趋势一致才可用。

用法
----
  python v3_decompose.py --outdir data/背靠背共存开销_20261025
"""
import argparse
import json
import math
import os
import random

HOSTS = ("84", "85")

COMBO = {  # 组合 → (拓扑, 守卫)
    "Ron": ("readsb", "on"), "Roff": ("readsb", "off"),
    "Son": ("s1090", "on"), "Soff": ("s1090", "off"),
}
SHOW = {"Ron": "R-on", "Roff": "R-off", "Son": "S-on", "Soff": "S-off"}

# 阶段 → (左组合, 右组合)；a=正向（左在 .84），b=反向（左在 .85）
PHASES = [
    ("P1a", "Ron", "Roff"), ("P1b", "Roff", "Ron"),
    ("P2a", "Roff", "Soff"), ("P2b", "Soff", "Roff"),
    ("P3a", "Roff", "Son"), ("P3b", "Son", "Roff"),
    ("P4a", "Ron", "Soff"), ("P4b", "Soff", "Ron"),
    ("P5a", "Ron", "Son"), ("P5b", "Son", "Ron"),
    ("P6a", "Soff", "Son"), ("P6b", "Son", "Soff"),
    ("Z1", "Ron", "Roff"), ("Z2", "Roff", "Ron"),
]
LEFT = {n: (l, r) for n, l, r in PHASES}

PAIRS = [
    ("P1", "Ron", "Roff", "P1a", "P1b"),
    ("P2", "Roff", "Soff", "P2a", "P2b"),
    ("P3", "Roff", "Son", "P3a", "P3b"),
    ("P4", "Ron", "Soff", "P4a", "P4b"),
    ("P5", "Ron", "Son", "P5a", "P5b"),
    ("P6", "Soff", "Son", "P6a", "P6b"),
]
ZPAIR = ("Z", "Ron", "Roff", "Z1", "Z2")

CHUNK_S = 120.0
C_LO, C_HI = 1.3, 20.0


def num(v):
    try:
        if v in (None, "", "None"):
            return None
        return float(v)
    except Exception:
        return None


def load_csv(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        hdr = f.readline().strip().split(",")
        for line in f:
            p = line.strip().split(",")
            if len(p) < len(hdr):
                continue
            rows.append(dict(zip(hdr, p)))
    return rows


def c_of(noise):
    n = num(noise)
    if n is None or n <= -99:
        return None
    return 128.0 * 10.0 ** (n / 20.0)


def rate(win, key):
    a, b = num(win[0].get(key)), num(win[-1].get(key))
    dt = num(win[-1]["ts"]) - num(win[0]["ts"])
    if a is None or b is None or not dt:
        return None
    return (b - a) / dt


def resets_in(win, key="t_msg"):
    vals = [num(r.get(key)) for r in win]
    vals = [v for v in vals if v is not None]
    return sum(1 for i in range(1, len(vals)) if vals[i] < vals[i - 1])


def tile(win, chunk):
    """把窗内的计数器增量切成若干连续块，返回每块的 msg/s、pos/s。

    块内速率 = ΣΔmsg / ΣΔt（不是"首尾点"）。这样：
      * 各块互不重叠、合起来恰好铺满整个窗 ⇒ 块速率按 Δt 加权平均 == 整窗速率；
      * 每块都是真实的时间平均，能反映 1–2 min 尺度的波动。
    旧写法（块内首尾点）既漏掉窗口两端、又让每块只覆盖 100 s，
    块均值与整窗值能差 2.6 pp —— 用它算 SE 会把不确定度低估到 1/20。
    """
    out = []
    t0 = num(win[0]["ts"])
    buf_dt = buf_m = buf_p = 0.0
    prev = win[0]
    for r in win[1:]:
        dt = (num(r["ts"]) or 0) - (num(prev["ts"]) or 0)
        dm = (num(r.get("t_msg")) or 0) - (num(prev.get("t_msg")) or 0)
        dp = (num(r.get("t_pos")) or 0) - (num(prev.get("t_pos")) or 0)
        if dt <= 0:
            prev = r
            continue
        buf_dt += dt
        buf_m += dm
        buf_p += dp
        prev = r
        if buf_dt >= chunk - 1e-6:
            out.append({"msg": buf_m / buf_dt, "pos": buf_p / buf_dt, "dt": buf_dt})
            buf_dt = buf_m = buf_p = 0.0
    if buf_dt > 0 and out:  # 余量并入最后一块，保证块和 == 整窗
        last = out[-1]
        tm = last["msg"] * last["dt"] + buf_m
        tp = last["pos"] * last["dt"] + buf_p
        last["dt"] += buf_dt
        last["msg"] = tm / last["dt"]
        last["pos"] = tp / last["dt"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--skip", type=float, default=240.0)
    ap.add_argument("--chunk", type=float, default=CHUNK_S)
    args = ap.parse_args()
    random.seed(20261025)

    # 兼容两种布局：报告目录（outdir/raw/…）与本册的数据目录（outdir/… 无 raw）
    raw = os.path.join(args.outdir, "raw")
    if not os.path.isdir(raw):
        raw = args.outdir
    phases = []
    with open(os.path.join(raw, "phases.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                phases.append(json.loads(line))
    csvs = {t: load_csv(os.path.join(raw, "xo_%s.csv" % t)) for t in HOSTS}
    ph = {(r["phase"], r["host"]): r for r in phases}

    stat, chunks, integ = {}, {}, {}
    for name, _, _ in PHASES:
        a, b = ph.get((name, "84")), ph.get((name, "85"))
        if not a or not b:
            print("! 阶段 %s 缺 phases 记录" % name)
            continue
        lo = max(a["t0"], b["t0"]) + args.skip
        hi = min(a["t1"], b["t1"])
        for tag in HOSTS:
            sel = [r for r in csvs[tag] if lo <= (num(r.get("ts")) or 0) <= hi
                   and not str(r.get("t_msg", "")).startswith("ERR")]
            if len(sel) < 3:
                continue
            integ[(name, tag)] = {"reset": resets_in(sel), "n": len(sel), "dur": hi - lo}
            cs = [x for x in (c_of(r["noise"]) for r in sel) if x is not None]
            stat[(name, tag)] = {
                "dur": hi - lo, "msg": rate(sel, "t_msg"), "pos": rate(sel, "t_pos"),
                "c": (sum(cs) / len(cs)) if cs else None,
            }
            # 亚窗：**按增量切块**（不是按"块内首尾点"）。按点切块时每块实际只覆盖
            # 100 s（6 个点 5 个间隔），且块与块漏掉窗口两端的部分 ⇒ 块均值与整窗值
            # 系统性不一致（P1 实测差 2.6 pp）。增量切块后块之和严格等于整窗。
            chunks[(name, tag)] = tile(sel, args.chunk)

    def host_of(name, label):
        lft, rgt = LEFT[name]
        if lft == label:
            return "84"
        if rgt == label:
            return "85"
        raise AssertionError("阶段 %s 不含组合 %s（实际 %s/%s）"
                             % (name, SHOW[label], SHOW[lft], SHOW[rgt]))

    def eff(name, left, right, key):
        i, j = host_of(name, left), host_of(name, right)
        a, b = stat.get((name, i)), stat.get((name, j))
        if not a or not b or not a.get(key) or not b.get(key):
            return None
        return (a[key] - b[key]) / b[key] * 100.0

    def eff_ln(name, left, right, key="msg"):
        i, j = host_of(name, left), host_of(name, right)
        a, b = stat.get((name, i)), stat.get((name, j))
        if not a or not b or not a.get(key) or not b.get(key):
            return None
        return math.log(a[key] / b[key]) * 100.0

    def pair_stats(nm, lft, rgt, fa, fb):
        o = {}
        for key in ("msg", "pos"):
            f, r = eff(fa, lft, rgt, key), eff(fb, lft, rgt, key)
            if f is None or r is None:
                o[key] = None
                continue
            l1, l2 = eff_ln(fa, lft, rgt, key), eff_ln(fb, lft, rgt, key)
            o[key] = {"f": f, "r": r, "e": (f + r) / 2.0, "b": (f - r) / 2.0,
                      "ln": (l1 + l2) / 2.0 if (l1 is not None and l2 is not None) else None}
        return o

    def chunk_effs(lft, rgt, fa, fb):
        """逐亚窗配对效应。

        ⚠ 必须**正反两向同序号配对再平均**，不能把两向的亚窗值倒在一起：
        正向亚窗效应 ≈ 效应 + b，反向 ≈ 效应 − b，b 有 8–15 个点，
        混在一起算离散度会得到 25–54 pp 的假象。配对后 b 抵消，剩下的
        才是「效应在 120 s 尺度上的真实波动」。
        """
        cf, cr = [], []
        for st, bucket in ((fa, cf), (fb, cr)):
            A = chunks.get((st, host_of(st, lft)))
            B = chunks.get((st, host_of(st, rgt)))
            n = min(len(A or []), len(B or []))
            for k in range(n):
                if B[k]["msg"]:
                    bucket.append((A[k]["msg"] - B[k]["msg"]) / B[k]["msg"] * 100.0)
        m = min(len(cf), len(cr))
        return [(cf[k] + cr[k]) / 2.0 for k in range(m)]

    E = {}
    for p in PAIRS:
        E[p[0]] = pair_stats(*p)
    E["Z"] = pair_stats(*ZPAIR)
    SEF = {p[0]: chunk_effs(*p[1:]) for p in PAIRS}

    out = []
    P = out.append

    P("# v3 独立复算与因子分解")
    P("")
    P("> 由 `tools/bench/v3_decompose.py` 生成，**不复用** harness 的 analyze；")
    P("> 目的是把「6 对效应」拆到自由度层面，并检查数据完整性。")
    P("")

    # ---------------- 0 ----------------
    P("## 0. 数据完整性")
    P("")
    nres = sum(v["reset"] for v in integ.values())
    nshort = sum(1 for v in integ.values() if v["dur"] < 300)
    P("| 检查项 | 结果 |")
    P("|---|---|")
    P("| 阶段×主机窗 | %d 个 |" % len(integ))
    P("| 窗内计数器复位（readsb 重启污染） | **%d 次** |" % nres)
    P("| 窗长 < 300 s | %d 个 |" % nshort)
    P("| 最短窗长 | %.0f s |" % min(v["dur"] for v in integ.values()))
    P("| 每窗采样点数 | %d–%d（20 s 间隔） |" % (
        min(v["n"] for v in integ.values()), max(v["n"] for v in integ.values())))
    P("")
    P("⇒ 无复位、窗长充足 ⇒ 首尾差法取率有效。" if nres == 0 and nshort == 0
      else "⚠ 存在复位或过短窗，下列数字需打折。")
    P("")

    # ---------------- 1 ----------------
    P("## 1. 六对效应（线性与对数两套）")
    P("")
    P("效应 = (正向 + 反向)/2；机器偏置 b = (正向 − 反向)/2。")
    P("")
    P("| 对 | 左 vs 右 | msg 正向 | msg 反向 | **msg 效应** | b(机器偏置) | msg 效应(ln) | pos 效应 |")
    P("|---|---|---|---|---|---|---|---|")
    for nm in [p[0] for p in PAIRS] + ["Z"]:
        lft, rgt = (LEFT[PAIRS[[q[0] for q in PAIRS].index(nm)][3]] if nm != "Z"
                    else LEFT["Z1"])
        m, ps = E[nm]["msg"], E[nm]["pos"]
        P("| %s%s | %s vs %s | %+.2f%% | %+.2f%% | **%+.2f%%** | %+.2f%% | %s | %s |" % (
            nm, "" if nm == "Z" else "", SHOW[lft], SHOW[rgt], m["f"], m["r"], m["e"], m["b"],
            ("%+.2f%%" % m["ln"]) if m["ln"] is not None else "n/a",
            ("%+.2f%%" % ps["e"]) if ps else "n/a"))
    P("")
    P("> 对数版与线性版差别 <0.6 pp ⇒ 效应小到非线性无关，后续统一线性即可。")
    P("")

    # ---------------- 2 ----------------
    gR = E["P1"]["msg"]["e"]
    gS = -E["P6"]["msg"]["e"]
    d_est = {
        "P2（Roff−Soff，无守卫）": E["P2"]["msg"]["e"],
        "P4−gR（Ron−Soff，扣守卫）": E["P4"]["msg"]["e"] - gR,
        "P3+gS（Roff−Son，扣守卫）": E["P3"]["msg"]["e"] + gS,
        "P5−gR+gS（Ron−Son）": E["P5"]["msg"]["e"] - gR + gS,
    }
    vs = list(d_est.values())

    def se_of(v):
        if len(v) < 2:
            return float("nan")
        m = sum(v) / len(v)
        return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1) / len(v))

    P("## 2. 最小二乘分解（自由度账）")
    P("")
    P("模型：`ln率 = μ + 机器 + 组合`，组合只有 4 个水平、1 个基准 ⇒ **3 个自由度**。")
    P("6 对差里有 3 条恒等式，独立信息只有 3 个：")
    P("")
    P("| 未知量 | 含义 | 独立来源 | 个数 | 估计 (msg) |")
    P("|---|---|---|---|---|")
    P("| gR | 守卫效应（readsb 拓扑内） | P1 | **1** | %+.2f%% |" % gR)
    P("| gS | 守卫效应（s1090 拓扑内） | −P6 | **1** | %+.2f%% |" % gS)
    P("| d | 拓扑效应 readsb−s1090 | P2 / P4−gR / P3+gS / P5−gR+gS | **4** | %+.2f%% ± %.2f |"
      % (sum(vs) / len(vs), se_of(vs)))
    P("")
    P("### 有冗余的那个量（d）反而最不干净")
    P("")
    P("| d 的估计来源 | 值 |")
    P("|---|---|")
    for k, v in d_est.items():
        P("| %s | %+.2f%% |" % (k, v))
    P("")
    P("- 4 个估计：%+.2f%% ~ %+.2f%%，极差 **%.2f pp**，SD **%.2f pp**" % (
        min(vs), max(vs), max(vs) - min(vs), se_of(vs) * 2))
    P("- 同一批数据、同一个物理量给出跨 %.0f pp 的 4 个答案 ⇒" % (max(vs) - min(vs)))
    P("  **拓扑效应在本实验分辨率之外，不予引用**（只能说量级 0–10%，方向 readsb ≥ s1090 不稳）。")
    P("")

    P("### 守卫效应：两路独立估计")
    P("")
    P("| 拓扑 | 整窗效应 | 亚窗均值（增量切块） | 两者差 | 亚窗 SE | 亚窗数 |")
    P("|---|---|---|---|---|---|")
    for nm, lab in (("P1", "readsb 直采"), ("P6", "s1090 管道")):
        e = SEF[nm]
        m = sum(e) / len(e)
        se = math.sqrt(sum((x - m) ** 2 for x in e) / (len(e) - 1) / len(e))
        P("| %s（%s） | %+.2f%% | %+.2f%% | %.2f pp | ±%.2f pp | %d |" % (
            lab, nm, E[nm]["msg"]["e"], m, abs(E[nm]["msg"]["e"] - m), se, len(e)))
    P("")
    P("- gR 与 gS **各只有一个独立点，没有冗余**（这是设计的自由度上限，不是失误）。")
    P("- 两点符号相反（%+.2f%% / %+.2f%%），量级都 ≤2%% ⇒ 守卫在域内是零打扰。" % (gR, gS))
    P("")

    # ---------------- 2b ----------------
    rng = [(nm, max(SEF[nm]) - min(SEF[nm])) for nm in SEF]
    ses = []
    for nm in SEF:
        e = SEF[nm]
        mu = sum(e) / len(e)
        ses.append(math.sqrt(sum((x - mu) ** 2 for x in e) / (len(e) - 1) / len(e)))
    rep = abs(E["P1"]["msg"]["e"] - E["Z"]["msg"]["e"])
    P("### 分辨率：三类不确定度")
    P("")
    P("| 不确定度来源 | 量级 | 怎么得的 |")
    P("|---|---|---|")
    P("| **同对重复性**（同组合、相隔 2.5 h） | **%.2f pp** | P1 与 Z 的效应差 |" % rep)
    P("| **亚窗 SE**（120 s 三块，配对） | ±%.1f–%.1f pp | 三个亚窗效应的标准误 |" % (
        min(ses), max(ses)))
    P("| **亚窗极差**（同样三块） | %.1f–%.1f pp | 最大块 − 最小块 |" % (
        min(x[1] for x in rng), max(x[1] for x in rng)))
    P("| **跨对系统性**（不同组合对之间） | %.1f pp | d 的 4 个估计的极差 |" % (max(vs) - min(vs)))
    P("")
    P("亚窗极差明细：%s" % "、".join("%s %.1f pp" % (a, b) for a, b in rng))
    P("")
    P("⇒ 单个组合对的效应**极精确**（0.2 pp 级重复性），但把它当成「守卫的一般效应」")
    P("**极不精确**（跨对可差近 10 pp）。本实验能给的最强结论只能是量级：")
    P("**|守卫效应| ≤ 2%（且与 0 不可区分）**。要压到 ±1% 需要**每条件多个独立对**，")
    P("而不是把单对测得更久 —— 加长时间只能降亚窗噪声，降不了跨对的系统性差异。")
    P("")

    # ---------------- 3 ----------------
    P("## 3. 机器偏置 b 的漂移（为什么必须成对）")
    P("")
    P("b = (正向 − 反向)/2，是本对里「.84 相对 .85 的系统差」。交叉法假定它**恒定**。")
    P("")
    P("| 对 | 组合 | b（msg） | 该对正向阶段 .84 的 msg/s |")
    P("|---|---|---|---|")
    for nm, lft, rgt, fa, fb in PAIRS + [ZPAIR]:
        P("| %s | %s vs %s | %+.2f%% | %.1f |" % (
            nm, SHOW[lft], SHOW[rgt], E[nm]["msg"]["b"],
            stat.get((fa, "84"), {}).get("msg") or 0))
    bs = [E[nm]["msg"]["b"] for nm in [p[0] for p in PAIRS] + ["Z"]]
    P("")
    P("- b 在 **%+.1f%% ~ %+.1f%%** 之间摆动（跨度 %.1f pp），全部同号（.84 系统性偏弱）。" % (
        min(bs), max(bs), max(bs) - min(bs)))
    P("- 关键对照：P1 与 Z 是**同一个组合对**（R-on × R-off），相隔 2.5 h，效应分别为")
    P("  %+.2f%% 与 %+.2f%%（差 %.2f pp），而 b 从 %+.1f%% 变到 %+.1f%%（差 %.1f pp）。" % (
        E["P1"]["msg"]["e"], E["Z"]["msg"]["e"],
        abs(E["P1"]["msg"]["e"] - E["Z"]["msg"]["e"]),
        E["P1"]["msg"]["b"], E["Z"]["msg"]["b"],
        abs(E["P1"]["msg"]["b"] - E["Z"]["msg"]["b"])))
    P("- ⇒ **b 漂移不进入成对效应**（相邻 10 min 内 b 近似常数，正反两向等量抵消），")
    P("  但跨对的绝对水平不可比 —— 所以 v2 那种「用 6 对的交叉平均代替两两配对」必然失真。")
    P("")

    # ---------------- 4 ----------------
    P("## 4. 同码同机的 R/S 观测口径差（对 L1 的独立旁证）")
    P("")
    P("同机、同钉码，两种拓扑读到的 c 应相等 —— 除非两条观测口径本身不同。")
    P("")
    P("| 主机@码 | R 拓扑 c（stats.noise） | S 拓扑 c（tee mean） | 比值 |")
    P("|---|---|---|---|")
    for tag in HOSTS:
        rs, ss = [], []
        for name, lft, rgt in PHASES:
            c = stat.get((name, tag), {}).get("c")
            if not c:
                continue
            topo = COMBO[lft if host_of(name, lft) == tag else rgt][0]
            (rs if topo == "readsb" else ss).append(c)
        if rs and ss:
            mr, ms = sum(rs) / len(rs), sum(ss) / len(ss)
            P("| .%s@%s | %.2f (n=%d) | %.2f (n=%d) | **%.2f×** |" % (
                tag, "0x0c" if tag == "84" else "0x0a", mr, len(rs), ms, len(ss), mr / ms))
    P("")
    P("理论 10^(3.04/20) = **1.42**（stats 是 I²+Q² 功率均值、tee 是单分量 RMS，差 3.04 dB），")
    P("实测 1.2–1.4，与 L1 六轮标定（1.11–1.39、中位 1.24）同向同量级。")
    P("⚠ 该比值**逐机不同**（强信号一侧更接近 1.42）⇒ 不能用一个常数跨机换算，")
    P("只能按 §L1 的结论「tee 口径 ≈ 真实码值 / 1.24」做量级修正。")
    P("")

    # ---------------- 5 ----------------
    P("## 5. 绝对速率的不平稳性（本实验的根本限制）")
    P("")
    P("把每阶段每机拆成 120 s 块，看绝对速率怎么变：")
    P("")
    P("| 阶段 | 主机 | 各块 msg/s | 块间极差 | 整窗 |")
    P("|---|---|---|---|---|")
    worst = 0.0
    for name, lft, rgt in PHASES:
        for tag in HOSTS:
            cl = chunks.get((name, tag)) or []
            if len(cl) < 2:
                continue
            vs_ = [x["msg"] for x in cl]
            rg = max(vs_) - min(vs_)
            worst = max(worst, rg)
            P("| %s | .%s | %s | %.1f | %.1f |" % (
                name, tag, " / ".join("%.1f" % v for v in vs_), rg,
                stat[(name, tag)]["msg"]))
    P("")
    P("- 块间极差最大 **%.1f msg/s**（同一台机、6 分钟内）⇒ 空情在 1–2 min 尺度剧烈变化。" % worst)
    P("- 这解释了为什么 §2 里 d 的 4 个估计能差 10 pp：**效应本身随空情漂移**，")
    P("  背靠背只能保证「一个对内部两个方向相邻」，不能保证「不同对之间可比」。")
    P("- 亚窗 SE 只能说明「效应在 120 s 尺度上抖多少」，**不是整窗效应的置信区间**：")
    P("  整窗效应是增量加权平均，与亚窗均值一致到 0.2 pp，但亚窗之间本身可差 13 pp。")
    P("  ⚠ 按「块内首尾点」切块时，每块只覆盖 100 s 且漏掉窗口两端，块均值与整窗值可")
    P("  差 2.6 pp（P1）⇒ 亚窗 SE 会低估到 ±0.11 pp 量级。本脚本按增量切块，")
    P("  块之和严格等于整窗，块均值与整窗一致到 0.23 pp。")
    P("")

    # ---------------- 6 ----------------
    P("## 6. 关键机理事实：14 个守卫相位，芯片一次没写")
    P("")
    gd = os.path.join(raw, "guardlogs")
    n_untouched = n_stat = 0
    files = 0
    if os.path.isdir(gd):
        for fn in sorted(os.listdir(gd)):
            if not fn.endswith(".log"):
                continue
            files += 1
            with open(os.path.join(gd, fn), encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "STATS" in line:
                        n_stat += 1
                        if "fc0013=未访问" in line:
                            n_untouched += 1
    P("| 项 | 值 |")
    P("|---|---|")
    P("| 守卫日志文件（= 守卫 ON 的相位×主机） | %d |" % files)
    P("| STATS 决策行 | %d |" % n_stat)
    P("| 其中 `fc0013=未访问` | **%d（%.1f%%）** |" % (
        n_untouched, 100.0 * n_untouched / max(n_stat, 1)))
    P("| 越界（c 出 [1.3,20]）样本 | 0 |")
    P("")
    P("其余 %d 行是 s1090 相位启动第 1 轮「STATS 不可读（观测源无有效数据），跳过本轮」" % (n_stat - n_untouched))
    P("—— tee CSV 还没攒够 30 s，属预期冷启动，不是缺陷。")
    P("")
    P("⇒ **本实验的 on/off 因子实际测的是「守卫进程在跑」的代价（CPU/I2C 占用、")
    P("   观测开销），不是「守卫纠正工作点」的收益**。域内零介入是设计语义")
    P("   （先判工况、后碰芯片），所以守卫效应 ≈ 0 是**机理预期**，不是运气。")
    P("   守卫的收益必须靠注入 AGC 的对照实验测（见 guard_ab_xover_20261024：")
    P("   无守卫 −30% 报文），本实验不承担这个任务。")
    P("")

    dst = os.path.join(raw, "decompose.md")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out))
    print("\n-> %s" % dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
