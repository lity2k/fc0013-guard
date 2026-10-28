#!/usr/bin/env python3
"""replay_guard.py — 离线回放验证 fc0013_guard.py 的决策逻辑

两部分：
  A. 用归档的真实 1s 轨迹回放（reg_trace_D84/D85、conv_A/B），
     检查守卫在真实数据上会不会误动作。
     ★ 回放会**模拟写入落地**：守卫决定写 target ⇒ 后续样本的 0x13 按 target 走，
       直到日志里的 0x13 再次与模拟值不符（说明有外部写）。
     ★ 回放按新架构执行：**工况判据先算**，need=False ⇒ 不读寄存器、不动作；
       need=True ⇒ 才"读"寄存器（此时才可能看到 canary / AGC 状态）。
  B. 注入故障场景（第三方写 0x13 / 第三方开 AGC / 过驱动 / 欠驱动），
     检查守卫是否按预注册期望动作。

预注册要点
----------
1. **不介入 ⇒ 不访问 FC0013**：工况在可行域内时，守卫既不读也不写 FC0013。
   ⇒ canary（第三方写 0x13）与"第三方开 AGC"只在**工况越界**时才被采样。
2. 第三方写 0x13 ⇒ 增益改变 ⇒ c 随之改变 ⇒ 越界即被发现（多数情形自愈）；
   AGC 开启 ⇒ 底噪被压到 0.71..0.87 码 < C_MIN ⇒ 判欠驱动 ⇒ 介入 ⇒ 检出 AGC 开。
3. **不越界**的第三方写（改后 c 仍留在可行域）不再被纠正：它不违反守卫目标
   （工作点仍可用），按"目标函数优先"接受。
4. 工况判据的纯路径必须用 recorded == 当前码 单独测。
5. 升档天花板 = 码值硬上限 0x18（与空情、与机器都无关）；peak 只作饱和兜底，
   不作升档门限。降档目标由超出量算出（域的几何中点），不查任何标定值。

用法: python3 replay_guard.py
"""
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..")))   # 主脚本在项目根
from fc0013_guard import decide_band, decide_action, c_rms as crms     # noqa: E402
from build_state_table import parse                                    # noqa: E402

# 归档 1s 轨迹（reg_trace_D84/D85、conv_A/B），随项目分发在 data/归档轨迹/
DATA_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "归档轨迹"))

# 虚拟时间基准（样本间隔 1 s）：让 SETTLE 门禁在回放中真实生效
T0 = 1.0e9

CFG = {
    "84": {"cal": 0x0A, "c_min": 1.3, "c_max": 20.0, "raise_ceiling": 0x18},
    "85": {"cal": 0x11, "c_min": 1.3, "c_max": 20.0, "raise_ceiling": 0x18},
}


def step(d, i, n, peak, cfg, state, now, sim13):
    """跑一轮守卫决策。返回 (action, target, reason, kind, sim13)

    sim13 = 模拟写入落地后的 0x13（None = 未模拟）；本函数**只在 need 时**读它，
    复现"不介入就不读寄存器"的行为。
    """
    need, band_target, band_reason = decide_band(now, n, peak, cfg, state)
    if not need:
        return "NONE", None, band_reason, None, sim13
    if sim13 is not None and i != sim13:
        sim13 = None                    # 日志里的码变了 ⇒ 外部写
    cur = sim13 if sim13 is not None else i
    action, target, reason, kind = decide_action(
        d, cur, band_target, band_reason, state)
    if action != "NONE":
        state["recorded"] = target
        state["last_write"] = now
        sim13 = target                  # 写入落地
    return action, target, reason, kind, sim13


def replay(tag, fn, cfg, recorded, expect_in_base=None):
    """expect_in_base: BASE 段期望的动作集合（None = 不检查）"""
    p = os.path.join(DATA_DIR, fn)
    rows = parse(p)
    if not rows:
        print("  %-24s 无数据" % tag)
        return True
    state = {"recorded": recorded, "last_write": 0.0, "actions": [], "hist": []}
    sim_13 = None          # 模拟写入落地后的 0x13
    hist = {}
    base_actions = []
    all_actions = []
    targets = []
    for idx, (phase, ts, regs, n, peak, s, acc) in enumerate(rows):
        d = regs.get(0x0D, 0xFF)
        log_i = regs.get(0x13, 0xFF)
        now = T0 + idx
        # 与主循环同序：先把本样本 c_rms 记入窗口，再判工况
        state.setdefault("hist", []).append(crms(n))
        state["hist"] = state["hist"][-6:]
        action, target, reason, kind, sim_13 = step(
            d, log_i, n, peak, cfg, state, now, sim_13)
        hist[action] = hist.get(action, 0) + 1
        if action != "NONE":
            all_actions.append((idx, phase, action, target, reason, d, log_i, n))
        if phase == "BASE":
            base_actions.append(action)
            if action != "NONE":
                targets.append((action, target, reason))
    ok = True
    if expect_in_base is not None:
        got = set(base_actions)
        ok = (got <= expect_in_base)
        print("  %-24s rows=%-4d 全段=%s BASE=%s %s" % (
            tag, len(rows), hist, hist_base(base_actions),
            "PASS" if ok else "FAIL"))
    else:
        print("  %-24s rows=%-4d 全段=%s" % (tag, len(rows), hist))
    for t in targets[:5]:
        print("        %s tgt=%02x %s" % t)
    if len(targets) > 5:
        print("        ... 另有 %d 条动作" % (len(targets) - 5))
    for (idx, phase, action, target, reason, d, log_i, n) in all_actions[:8]:
        print("        #%-4d %-8s %-7s tgt=%-4s d=%02x 13=%02x n=%.1f c=%.2f | %s"
              % (idx, phase, action, ("-" if target is None else "%02x" % target),
                 d, log_i, n, crms(n), reason))
    if len(all_actions) > 8:
        print("        ... 另有 %d 条动作" % (len(all_actions) - 8))
    return ok


def hist_base(actions):
    h = {}
    for a in actions:
        h[a] = h.get(a, 0) + 1
    return h


def scenario(name, d, i, n, peak, cfg, recorded, expect_action, expect_kw=None,
             expect_target=None, hist=None):
    if hist is None:
        hist = [crms(n)] * 5          # 默认给 5 个稳定样本（覆盖一个完整窗口）
    state = {"recorded": recorded, "last_write": 0.0, "actions": [], "hist": list(hist)}
    action, target, reason, kind, _ = step(d, i, n, peak, cfg, state, T0, None)
    ok = (action == expect_action)
    if ok and expect_kw:
        ok = expect_kw in reason
    if ok and expect_target is not None:
        ok = (target == expect_target)
    print("  %-50s -> %-7s tgt=%-5s %-44s %s" % (
        name, action, ("-" if target is None else "%02x" % target),
        reason[:44], "PASS" if ok else "FAIL"))
    return ok


def check_idle_no_io():
    """专项：工况健康 ⇒ need=False ⇒ 守卫不读不写 FC0013。

    这里直接调 decide_band（纯 stats），返回值不含任何寄存器信息，
    即"不介入路径"上不存在 FC0013 访问。
    """
    ok = True
    for tag, cfg, recorded, n, peak in (
            (".84 健康 ⇒ 不介入", CFG["84"], 0x0A, -32.1, -1.9),
            (".85 健康 ⇒ 不介入", CFG["85"], 0x11, -21.1, -0.8),
    ):
        state = {"recorded": recorded, "last_write": 0.0, "actions": [],
                 "hist": [crms(n)] * 5}
        need, target, reason = decide_band(T0, n, peak, cfg, state)
        good = (need is False)
        ok &= good
        print("  %-50s -> need=%-5s %-40s %s" % (
            tag, need, reason[:40], "PASS" if good else "FAIL"))

    # 越界但无可写目标：仍需介入（读寄存器核对 AGC / 第三方写），但不一定写
    # 构造：欠驱动且已到硬上限 0x18 ⇒ 升不动
    state = {"recorded": 0x18, "last_write": 0.0, "actions": [],
             "hist": [crms(-45.1)] * 5}
    need, target, reason = decide_band(T0, -45.1, -35.0, CFG["85"], state)
    good = (need is True and target is None)
    ok &= good
    print("  %-50s -> need=%-5s tgt=%-5s %-30s %s" % (
        "越界但无目标码 ⇒ 介入核对、不写", need,
        ("-" if target is None else "%02x" % target), reason[:30],
        "PASS" if good else "FAIL"))
    return ok


def check_i2c_transport():
    """D. I2C 传输层离线自检：用假 libusb 核对控制传输序列。

    要抓的是红线：写必须**一次**传输 [reg, val] 两字节（拆两次 USB 会成功而
    I2C 静默失败，读回仍是旧值）。
    """
    import types
    import fc0013_guard as G

    REP_WV = (0x01 << 8) | 0x20

    class Fn(object):                 # 允许 ctypes 的 argtypes 赋值
        def __init__(self, f):
            self.f = f

        def __call__(self, *a):
            return self.f(*a)

    class Fake(object):
        def __init__(self, open_ok=True):
            self.open_ok = open_ok
            self.log = []
            self.reg = {0x0D: 0x09, 0x13: 0x0A, 0x14: 0x50}
            self.pending = None

        def _init(self, ctx):
            return 0

        def _open(self, ctx, vid, pid):
            self.log.append(("open",))
            return 99 if self.open_ok else None

        def _close(self, h):
            self.log.append(("close",))

        def _ctrl(self, h, rt, br, wv, wi, data, length, timeout):
            b = bytes(bytearray(data)[:length])
            if rt == 0x40:
                self.log.append(("out", wv, b.hex()))
                if wv == REP_WV:
                    pass                         # repeater 开/关
                elif len(b) == 2:
                    self.reg[b[0]] = b[1]          # 写：必须一次两字节
                else:
                    self.pending = b[0]
                return length
            for i in range(length):
                data[i] = self.reg.get(self.pending, 0)
            self.log.append(("in", wv, bytes(bytearray(data)[:length]).hex()))
            return length

    def mount(fake):
        ns = types.SimpleNamespace(
            libusb_init=Fn(fake._init),
            libusb_exit=Fn(lambda c: 0),
            libusb_open_device_with_vid_pid=Fn(fake._open),
            libusb_close=Fn(fake._close),
            libusb_control_transfer=Fn(fake._ctrl))
        old = G.ctypes.CDLL
        G.ctypes.CDLL = lambda name: ns
        return old

    ok = True
    # —— 1. set_code 的完整传输序列 ——
    fake = Fake()
    old = mount(fake)
    try:
        fc = G.Fc()
        good, out = fc.set_code(0x0A)
        seq = [e for e in fake.log if e[0] != "close" and len(e) == 3]
        want = [("out", REP_WV, "18"),     # rep_on
                ("out", 0xC6, "0d09"),      # 写 0x0d=09（关 AGC）
                ("out", 0xC6, "0d"), ("in", 0xC6, "09"),      # 读回校验
                ("out", 0xC6, "130a"),                      # 写 0x13=0a
                ("out", 0xC6, "13"), ("in", 0xC6, "0a"),     # 读回校验
                ("out", REP_WV, "10")]      # rep_off
        good = good and seq == want
        ok &= good
        print("  %-50s -> %s" % ("set_code 传输序列（关AGC→钉码→复读→关repeater）",
                                 "PASS" if good else "FAIL\n    got=%s" % (seq,)))
        # —— 2. 写必须一次 [reg,val] 两字节（红线）——
        # 拆分写的形态是"单字节 OUT 之后紧跟另一个 OUT"（没有 IN）；
        # 读寄存器也是单字节 OUT，但它后面必须紧跟 IN。据此区分。
        writes = [e for e in seq if e[0] == "out" and e[1] == 0xC6
                  and len(e[2]) == 4]
        bad_split = 0
        for i, e in enumerate(seq[:-1]):
            if (e[0] == "out" and e[1] == 0xC6 and len(e[2]) == 2
                    and seq[i + 1][0] != "in"):
                bad_split += 1
        good = (len(writes) == 2 and bad_split == 0)
        ok &= good
        print("  %-50s -> 写=%d 拆分违规=%d %s"
              % ("写一次 [reg,val] 两字节、无拆分", len(writes), bad_split,
                 "PASS" if good else "FAIL"))
        # —— 3. 值域白名单：0x0d 高 4 位非 0 ⇒ 读数不可信 ⇒ None ——
        fake.reg[0x0D] = 0xFF
        good = (fc.read_013() is None)
        ok &= good
        print("  %-50s -> %s" % ("白名单拦下 0d=ff（返回 None）",
                                 "PASS" if good else "FAIL"))
    finally:
        G.ctypes.CDLL = old

    # —— 4. 设备打不开 ⇒ 读返回 None、写拒绝执行 ——
    fake2 = Fake(open_ok=False)
    old = mount(fake2)
    try:
        fc2 = G.Fc()
        good = fc2.read_013() is None
        ok2, msg = fc2.set_code(0x0A)
        good = good and (not ok2)
        ok &= good
        print("  %-50s -> %s" % ("设备开不出 ⇒ 不读不写", "PASS" if good else "FAIL"))
    finally:
        G.ctypes.CDLL = old
    return ok


def check_universal():
    """普适性断言：脚本不得与特定机器绑定。

    ⚠ 核心断言：
      1. **绝不因"未知机器/读不回"拒绝启动** ⇒ resolve_code 永不返回 None；
         无 --cal + 无通道 ⇒ 落到普适默认码 DEFAULT_CODE。
      2. **判定与 cal 无关** ⇒ 同一 (c, cur) 下，把 cfg["cal"] 换成任意值，
         降档目标与升档天花板都不变（二者都不读 cal）。
    """
    import fc0013_guard as G
    from fc0013_guard import resolve_code, decide_band
    ok = True

    code, src, need_w = resolve_code(None, None)
    good = (code is not None and code == G.DEFAULT_CODE and need_w)
    ok &= good
    print("  %-50s -> code=%02x(%s) %s" % (
        "无 --cal + 通道不可用 ⇒ 默认码，不拒绝启动", code, src,
        "PASS" if good else "FAIL"))

    code, src, need_w = resolve_code("0x12", None)
    good = (code == 0x12 and not need_w)
    ok &= good
    print("  %-50s -> code=%02x(%s) %s" % (
        "显式 --cal 优先，且无需对齐写入", code, src,
        "PASS" if good else "FAIL"))

    # 冷启动默认码必须有物理依据：该档自身底噪 >= C_MIN（天线开路也不欠驱动）
    selfnoise = 0.736 * 10 ** ((G.DEFAULT_CODE - 0x0A) * G.DB_PER_CODE / 20.0)
    good = selfnoise >= G.C_MIN_DEFAULT
    ok &= good
    print("  %-50s -> %.3f 码 vs C_MIN %.2f %s" % (
        "默认码档自身底噪 >= C_MIN（开路也不欠驱动）", selfnoise,
        G.C_MIN_DEFAULT, "PASS" if good else "FAIL"))

    # 两机健康工作点必须都落在普适可行域内 ⇒ 常态零动作
    for tag, c in ((".84@0x0a", 3.33), (".85@0x11", 11.67)):
        good = (G.C_MIN_DEFAULT < c < G.C_MAX_DEFAULT)
        ok &= good
        print("  %-50s -> c=%.2f ∈ (%.2f, %.1f) %s" % (
            "健康工作点在普适可行域内 %s" % tag, c, G.C_MIN_DEFAULT,
            G.C_MAX_DEFAULT, "PASS" if good else "FAIL"))

    # 与 cal 无关：换 cal 不改判定
    st = {"recorded": 0x11, "last_write": 0.0, "actions": [],
          "hist": [24.0] * 5}          # c=24 > c_max ⇒ 过驱动
    base = {"cal": 0x11, "c_min": 1.3, "c_max": 20.0, "raise_ceiling": 0x18}
    _, t1, _ = decide_band(T0 + 1e6, 20 * math.log10(24.0 / 128), -30.0, base, st)
    _, t2, _ = decide_band(T0 + 1e6, 20 * math.log10(24.0 / 128), -30.0,
                           dict(base, cal=0x0A), st)
    good = (t1 is not None and t1 == t2)
    ok &= good
    print("  %-50s -> cal=11:%s cal=0a:%s %s" % (
        "降档目标与 cal 无关",
        "--" if t1 is None else "%02x" % t1,
        "--" if t2 is None else "%02x" % t2, "PASS" if good else "FAIL"))

    st2 = {"recorded": 0x0A, "last_write": 0.0, "actions": [], "hist": [1.0] * 5}
    _, t3, _ = decide_band(T0 + 1e6, 20 * math.log10(1.0 / 128), -30.0, base, st2)
    good = (t3 == 0x0B)                 # 欠驱动升一格，不被 cal=0x11 之外的天花板卡住
    ok &= good
    print("  %-50s -> %s %s" % (
        "欠驱动在未知机器上仍能升档",
        "--" if t3 is None else "%02x" % t3, "PASS" if good else "FAIL"))
    return ok


def check_observers():
    """观测源：`--mode` 决定唯一来源（readsb→stats，s1090→tee_power），互不回退。"""
    import json
    import random
    import tempfile
    from fc0013_guard import (read_stats, observe, OBS_OFFSET_DB, MODE_READSB,
                              MODE_S1090, TEE_MIN_ROWS, TEE_WINDOW_S)
    RS = MODE_READSB
    S1 = MODE_S1090

    d = tempfile.mkdtemp(prefix="obs_")
    p_stats = os.path.join(d, "stats.json")
    p_tee = os.path.join(d, "tee_power.csv")
    ok = True

    def chk(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print("  %-56s %s %s" % (name, "PASS" if cond else "FAIL", detail))

    def wr_tee(vals, dt_ms=200, age0_ms=0):
        """按时间倒序写 tee CSV：最后一行的 age = age0_ms。

        列序：epoch_ms, p10, p50, mean, peak, clip, nsub —— 守卫取的是 mean(第 4 列)。
        p50 故意写成不同值，用于证明脚本确实没在读 p50。
        """
        now_ms = time.time() * 1000.0
        n = len(vals)
        with open(p_tee, "w") as f:
            for k, v in enumerate(vals):
                age = age0_ms + (n - 1 - k) * dt_ms
                f.write("%.0f,-99,%s,%.2f,-99,0.0,120\n"
                        % (now_ms - age, ("%.2f" % (v - 6.0)), v))

    def wr_stats(local):
        with open(p_stats, "w") as f:
            json.dump({"last1min": {"local": local}}, f)

    # —— 形态① readsb 直采 ——
    wr_stats({"noise": -32.1, "peak_signal": -1.9, "strong_signals": 0,
              "accepted": [1, 2]})
    st = observe(RS, p_stats, p_tee)
    chk("① 直采：src=stats 且 noise 原样", st and st["src"] == "stats"
        and abs(st["noise"] + 32.1) < 1e-6, "noise=%s" % (st and st["noise"]))
    chk("① 直采：peak_valid=True", st and st["peak_valid"])

    # —— 形态② stream1090：net-only 下 local 为空 ——
    wr_stats({})
    chk("② net-only(local 空) ⇒ stats 源返回 None", read_stats(p_stats) is None)
    wr_tee([-42.0] * 20)
    st = observe(S1, p_stats, p_tee)
    chk("② s1090 模式读 tee（src=tee）", st and st["src"] == "tee")
    chk("② tee 取 mean 列（不是 p50；p50 被写成 mean−6）",
        st and abs(st["noise"] - (-42.0 + OBS_OFFSET_DB)) < 1e-6,
        "noise=%.3f" % (st and st["noise"]))
    chk("② tee 源不提供可比 peak（peak_valid=False）", st and not st["peak_valid"])
    c = crms(st["noise"])
    chk("② mean=−42 ⇒ 落在 C_MIN(1.3) 之下、判欠驱动", c < 1.3, "c=%.3f" % c)

    # —— 实测锚点：.84@0x0c 直采 noise=−27.7 与 tee mean=−27.84 同域 ——
    wr_tee([-27.84] * 20)
    st = observe(S1, p_stats, p_tee)
    c = crms(st["noise"])
    chk("② 实测锚点 mean=−27.84 ⇒ c=5.2（域内，与直采 −27.7→5.3 一致）",
        abs(c - 5.2) < 0.3, "c=%.2f" % c)

    # —— 关键防回归：单行抖动必须被窗口聚合吃掉 ——
    random.seed(7)
    jitter = [-27.84 + random.uniform(-1.5, 1.5) for _ in range(60)]
    wr_tee(jitter)
    st = observe(S1, p_stats, p_tee)
    chk("② 单行抖动 ±1.5 dB ⇒ 窗口中位数稳定在 ±0.5 dB",
        st and abs(st["noise"] + 27.84 - OBS_OFFSET_DB) < 0.5,
        "noise=%.2f rows=%s" % (st and st["noise"], st and st.get("n")))
    chk("② 聚合行数 >= TEE_MIN_ROWS", st and st["n"] >= TEE_MIN_ROWS,
        "rows=%s" % (st and st.get("n")))
    # 相邻两次采样（间隔 15 s）读数差 < 1 dB ⇒ 收敛门禁可过
    jitter2 = jitter + [-27.84 + random.uniform(-1.5, 1.5) for _ in range(75)]
    wr_tee(jitter2)
    st2 = observe(S1, p_stats, p_tee)
    dd = abs(20 * math.log10(crms(st2["noise"]) / crms(st["noise"])))
    chk("② 相邻采样点(15 s) 差 < 1 dB（收敛门禁可过）", dd < 1.0, "Δ=%.2f dB" % dd)

    # —— 样本不足 / 陈旧 / 损坏 ——
    wr_tee([-36.87] * (TEE_MIN_ROWS - 1))
    chk("② 窗内行数不足(%d<min) ⇒ None" % (TEE_MIN_ROWS - 1),
        observe(S1, p_stats, p_tee) is None)
    wr_tee([-36.87] * 20, age0_ms=120000)
    chk("② tee 末行已陈旧(>15s) ⇒ 返回 None", observe(S1, p_stats, p_tee) is None)
    os.remove(p_tee)
    chk("② 两源都无 ⇒ observe 返回 None（不伪造成 noise=0）",
        observe(S1, p_stats, p_tee) is None)
    with open(p_tee, "w") as f:
        f.write("bogus\n")
    chk("② tee 内容损坏 ⇒ 返回 None", observe(S1, p_stats, p_tee) is None)

    # —— 严格单源：两源同时可用时，各模式只认自己那一个 ——
    wr_stats({"noise": -21.1, "peak_signal": -0.8, "strong_signals": 0})
    wr_tee([-40.0] * 20)
    st = observe(RS, p_stats, p_tee)
    chk("两源都在 ⇒ readsb 模式取 stats（不看 tee）",
        st and st["src"] == "stats", "src=%s" % (st and st["src"]))
    st = observe(S1, p_stats, p_tee)
    chk("两源都在 ⇒ s1090 模式取 tee（不看 stats）",
        st and st["src"] == "tee", "src=%s" % (st and st["src"]))

    # —— 防回归：模式不许悄悄回退（否则配置错会被伪装成"工作正常"）——
    wr_stats({})                       # stats 不可用
    wr_tee([-40.0] * 20)               # 但 tee 有数据
    chk("readsb 模式下 stats 不可用 ⇒ None（不回退 tee）",
        observe(RS, p_stats, p_tee) is None)
    wr_stats({"noise": -21.1, "peak_signal": -0.8, "strong_signals": 0})
    os.remove(p_tee)
    chk("s1090 模式下 tee 不存在 ⇒ None（不回退 stats）",
        observe(S1, p_stats, p_tee) is None)

    # —— 窗口常量自洽：聚合窗大于采样间隔，相邻点才有重叠 ——
    chk("聚合窗 %.0fs > 采样间隔 15s（相邻点重叠）" % TEE_WINDOW_S,
        TEE_WINDOW_S > 15.0)

    for fn in (p_stats, p_tee):
        try:
            os.remove(fn)
        except OSError:
            pass
    try:
        os.rmdir(d)
    except OSError:
        pass
    return ok


def main():
    ok_all = True
    print("=" * 108)
    print("A. 真实轨迹回放（含写入落地模拟，按需介入）")
    # D84/D85/CONVA：BASE 段是脚本自己钉的码 ⇒ 全程不应有任何动作
    ok_all &= replay("D84 BASE 自钉 0x0a", "reg_trace_D84.log", CFG["84"], 0x0A, {"NONE"})
    ok_all &= replay("D85 BASE 自钉 0x11", "reg_trace_D85.log", CFG["85"], 0x11, {"NONE"})
    ok_all &= replay("CONVA BASE 自钉 0x11", "conv_A.log", CFG["85"], 0x11, {"NONE"})
    # CONVB：.85 从 0x0a 起步（欠驱动：c=0.84<1.3）⇒ 守卫应逐格升到标定码 0x11
    ok_all &= replay("CONVB .85 从 0x0a 起步", "conv_B.log", CFG["85"], 0x0A,
                     {"NONE", "WRITE"})

    print()
    print("=" * 108)
    print("B. 注入故障场景")
    c84, c85 = CFG["84"], CFG["85"]
    r = []
    # —— 健康态必须不动（且不访问 FC0013）——
    r.append(scenario("健康 .84@0x0a", 0x09, 0x0A, -32.1, -1.9, c84, 0x0A,
                       "NONE", "OK"))
    r.append(scenario("健康 .85@0x11", 0x09, 0x11, -21.1, -0.8, c85, 0x11,
                       "NONE", "OK"))
    # —— 第三方写探针（只在工况越界时被采样）——
    r.append(scenario("第三方开 AGC 但工况仍在域内 ⇒ 不纠正(取舍)", 0x01, 0x0A,
                       -32.1, -1.9, c84, 0x0A, "NONE", "OK"))
    r.append(scenario("第三方写 0x13=00 ⇒ 欠驱动 ⇒ 写回起点", 0x09, 0x00, -50.0,
                       -35.0, c84, 0x0A, "WRITE", "CANARY", 0x0A))
    r.append(scenario("第三方写 0x13=0a 在 .85(域内) ⇒ 不纠正(取舍)", 0x09, 0x0A,
                       -21.1, -0.8, c85, 0x11, "NONE", "OK"))
    r.append(scenario("守卫在 0x0b，第三方改回 0x0a(域内) ⇒ 不纠正(取舍)",
                       0x09, 0x0A, -32.1, -1.9, c84, 0x0B, "NONE", "OK"))
    r.append(scenario("第三方写 0x1f (过载码) ⇒ 越界后 canary 命中", 0x09, 0x1F, -5.0,
                       -0.5, c84, 0x0A, "WRITE", "CANARY", 0x0A))
    # —— 工况判据纯路径（recorded == 当前码，绕开 canary）——
    r.append(scenario(".85 欠驱动(0x0a) → 升一格到 0x0b", 0x09, 0x0A, -45.1,
                       -35.0, c85, 0x0A, "WRITE", "升一格", 0x0B))
    r.append(scenario(".85 欠驱动(0x10) → 升一格 0x11", 0x09, 0x10, -45.1,
                       -35.0, c85, 0x10, "WRITE", "升一格", 0x11))
    # —— 普适化：天花板是码值硬上限，欠驱动可一直升 ——
    r.append(scenario(".85 在 0x11 仍欠驱动 → 继续升(不卡标定码)", 0x09, 0x11, -45.1,
                       -35.0, c85, 0x11, "WRITE", "升一格", 0x12))
    r.append(scenario(".84 在 0x0a 仍欠驱动 → 继续升(不卡标定码)", 0x09, 0x0A, -45.1,
                       -35.0, c84, 0x0A, "WRITE", "升一格", 0x0B))
    r.append(scenario("升到硬上限 0x18 仍欠驱动 → 无目标(需人工)", 0x09, 0x18, -45.1,
                       -35.0, c85, 0x18, "NONE", "需人工"))
    # —— 降档：按超出量算格数，目标 = 域几何中点 sqrt(1.3*20)=5.1 码 ——
    r.append(scenario("过驱动(c=72) → 按超出量降到地板 0x08", 0x09, 0x12, -5.0,
                       -0.5, c85, 0x12, "WRITE", "降档", 0x08))
    r.append(scenario("温和过驱动(c=25) → 降 9 格到 0x09", 0x09, 0x12, -14.2,
                       -0.5, c85, 0x12, "WRITE", "降档", 0x09))
    r.append(scenario("过驱动但已在低位码 → 仍可再降", 0x09, 0x0A, -5.0, -0.5, c84,
                       0x0A, "WRITE", "降档", 0x08))
    # —— 削波是硬约束：与码位无关，固定降 2 格 ——
    r.append(scenario("peak 撞 0 dBFS → 降 2 格", 0x09, 0x12, -25.0, 0.0,
                       c85, 0x12, "WRITE", "CLIP", 0x10))
    r.append(scenario("peak 撞 0 dBFS 在低位码 → 仍降 2 格", 0x09, 0x11, -25.0, 0.0,
                       c85, 0x11, "WRITE", "CLIP", 0x0F))
    r.append(scenario("peak 撞顶但已到地板 → 无目标(需人工)", 0x09, 0x08, -25.0, 0.0,
                       c85, 0x08, "NONE", "需人工"))
    r.append(scenario("AGC ON 且 c 被压出域 ⇒ 检出并关闭", 0x01, 0x0A, -45.1, -35.0,
                       c84, 0x0A, "AGCOFF", "AGC_ON", 0x0A))
    # —— 空情无关性：升档只认 c_rms，不认 peak ——
    r.append(scenario("安静时段(peak=-35)但 c 在域内 ⇒ 不升档", 0x09, 0x11,
                       -21.1, -35.0, c85, 0x11, "NONE", "OK"))
    r.append(scenario("安静时段(peak=-35)且欠驱动 ⇒ 照升(与 peak 无关)", 0x09, 0x10,
                       -45.1, -35.0, c85, 0x10, "WRITE", "升一格", 0x11))
    r.append(scenario("天花板 0x18：0x17 可升一格", 0x09, 0x17, -45.1,
                       -35.0, c85, 0x17, "WRITE", "升一格", 0x18))
    # —— 窗口收敛门禁 ——
    r.append(scenario("样本不足(0/5) → 不判工况", 0x09, 0x11, -45.1, -35.0, c85,
                       0x11, "NONE", "UNSTABLE", hist=[]))
    r.append(scenario("样本不足(3/5) → 不判工况", 0x09, 0x11, -45.1, -35.0, c85,
                       0x11, "NONE", "UNSTABLE", hist=[0.7, 0.7, 0.7]))
    r.append(scenario("收敛暂态(极差 5 dB) → 不判工况", 0x09, 0x11, -45.1, -35.0,
                       c85, 0x11, "NONE", "UNSTABLE",
                       hist=[51.0, 42.0, 32.0, 29.0, 28.0]))
    r.append(scenario("稳定(极差 1 dB) → 正常判工况", 0x09, 0x11, -45.1, -35.0,
                       c85, 0x11, "WRITE", "升一格", 0x12,
                       hist=[0.71, 0.72, 0.73, 0.72, 0.71]))
    r.append(scenario("稳定但过驱动 → 正常判工况", 0x09, 0x12, -5.0, -0.5, c85,
                       0x12, "WRITE", "降档", 0x08, hist=[72, 71, 73, 72, 71]))

    print()
    print("=" * 108)
    print("C. 不介入 ⇒ 不访问 FC0013（专项）")
    r.append(check_idle_no_io())

    print()
    print("=" * 108)
    print("D. I2C 传输层离线自检（假 libusb，核对控制传输序列）")
    r.append(check_i2c_transport())

    print()
    print("=" * 108)
    print("E. 普适性（不绑定特定机器、不拒绝启动）")
    r.append(check_universal())

    print()
    print("=" * 108)
    print("F. 观测源（readsb 直采 / stream1090+net-only 自动切换）")
    r.append(check_observers())

    print()
    n_ok = sum(1 for x in r if x)
    print("场景通过 %d/%d ；回放无误动作=%s" % (n_ok, len(r), "是" if ok_all else "否"))
    return 0 if (n_ok == len(r) and ok_all) else 1


if __name__ == "__main__":
    sys.exit(main())
