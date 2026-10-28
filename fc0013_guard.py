#!/usr/bin/env python3
"""fc0013_guard.py — FC0013 IF 增益守卫（按需介入：先判工况，介入才碰 FC0013）

唯一任务：保证 IF 增益工作点留在该机的 ADC 可行域内。

执行顺序（当前做法）
------------------------------------------------------------------------
  第一步 工况判据（**只读 readsb stats.json，不访问 FC0013**）
      noise → c_rms = 128*10^(noise/20) → 与可行域 [C_MIN, C_MAX] 比较
      ⇒ need = 需要介入 / 不需要介入
  第二步 介入决策（**仅当 need=True 时才读 FC0013**）
      读 0x0d/0x13/0x14 ⇒ 判 AGC 是否开启、0x13 是否被第三方写过
      · AGC 开启（0x0d bit3==0）⇒ 动作 AGCOFF：先关 AGC，再写回"增益起点值"
      · 0x13 != 我方记录 ⇒ 动作 WRITE：写回"增益起点值"（第三方写探针）
      · 否则 ⇒ 按工况目标码写（升一格 / 按超出量降档）
  第三步 写 FC0013：**脚本自身**用 ctypes 直调 libusb（单次控制传输 [reg,val]，
            带回读校验），不经 fc_daemon 等外部二进制

  ⚠ 常态（工况在可行域内）**完全不访问 FC0013：不读、不写**。
  ⚠ 收敛门禁（近 5 样本极差 ≤3 dB）与 SETTLE 门禁（上次写入后 70 s）
    未过时 ⇒ 判为暂态 ⇒ 不介入 ⇒ 不访问 FC0013。

判据口径（全部来自实测，见 build_state_table.py）
------------------------------------------------------------------------
  c_rms = 128 * 10^(noise_dBFS/20)      8bit 满量程码值，**"守卫标度"**

  ⚠⚠ 本项目有两个都叫"码值"的标度，**勿混用**（2026-10-25 定论，旧 +4.77/×1.7318 已废）：
      · **守卫标度**（本文件所用）：`c = 128 × 10^(noise/20)`  —— 与 C_MIN/C_MAX
        及本脚本日志属于同一标度，判据必须在此标度内比较。
      · **真实 ADC 码值**：按观测源换算 ——
          tee 源（`tee_power` 的 mean_dbfs，单分量 rms）的 c **就是**真实码值；
          stats 源（`stats.json` 的 noise）的 c **÷1.24** 才是
          （中位 +1.84 dB，理论上限 +3.04 = 10log10(2)，逐机 1.16–1.42）。
      源码依据：`stats.c:535` 取的是 I²+Q² 的**功率**均值，比单分量高 3 dB 再扣信号功率；
      `demod_2400.c:503` 已扣 signal_power ⇒ 偏移上限 10log10(2) = 3.04 dB
      （见 `docs/02-判据与阈值.md`「标度」一节）。
      ⚠ 两种观测源的**物理窗口不等价**：tee [1.3, 20] / stats [1.05, 16.1]。
        现工作点真值 ≈4.0、两端余 5–6 dB，故阈值不改；但收紧 C_MAX 或抬高 C_MIN
        之前必须先统一观测源口径。

  实测健康态（守卫标度）： .84@0x0a → 2.93..3.33 ； .85@0x11 → 10.17..11.67
  实测 AGC 压缩态（守卫标度）：                 0.71..0.87
  ⇒ C_MIN = 1.3 干净分开两者（对空情不敏感：noise 是底噪估计，
    CONVA 里 strong 389..873 时 noise 仍稳定在 -22.0..-21.7）

  peak **不是**削波判据：.85 健康态 peak 就是 -0.8 dBFS（过顶航班决定），
  拿它当削波门限会常态误触发。真正的饱和用 peak >= SAT_LIMIT 兜底，
  且升档还需额外满足 headroom 条件（见 can_raise）。

动作不对称（红线）
------------------------------------------------------------------------
  降档：按超出量算格数，目标 = 可行域几何中点（过头是缓坡：量化噪声总损失上限
        ≈0.166 dB，出现在 c≈1.46 处；增益步长本身是 1.55 dB/码）
  升档：一格一格，天花板 = 码值硬上限 0x18（升过头 c>=40 是悬崖，全灭）
  ⚠ 升档门限不能用 peak 余量：peak 是空情量，安静时段会误判余量充足。

普适性
------------------------------------------------------------------------
  本脚本是**通用**脚本：不得出现任何与特定机器/天线/标定结果绑定的取值。
  起点码缺省时：启动读一次 0x13；读不回 ⇒ 用普适默认码 DEFAULT_CODE(0x0e)，
  **不拒绝启动**。可行域上界 C_MAX、升档天花板、降档目标全部由判据常量算出，
  不查任何逐机表 ⇒ 未知 FC0013 上也能自行收敛到域内。

用法（普通模式：零参数，一条命令）
------------------------------------------------------------------------
  python3 fc0013_guard.py                 # 默认 --mode readsb
  python3 fc0013_guard.py --mode s1090    # 配合 stream1090 时只加这一个参数

  什么都不用配：脚本启动时自己读一次 FC0013 的 0x13 当起点码，
  读不回就用普适默认码 0x0e；随后每 15 s 读一次底噪观测，
  工况在可行域内就**完全不碰芯片**，越界才介入。

两种部署形态（由 --mode 指定，**严格单源、互不回退**）
------------------------------------------------------------------------
  ① --mode readsb（默认）：readsb 独占 SDR 直采
       观测 = stats.json 的 last1min.local.noise / peak_signal
       只读 stats；读不到就报"不可读"跳过本轮，**不会**改用别的源——
       否则部署形态与 --mode 不符时会被伪装成"正在正常工作"。

  ② --mode s1090：stream1090 + readsb --net-only，readsb 只做纯解码器
       管道由本脚本自建（不再需要 s1090_pipe.sh，也不再需要 socat）：
         rtl_sdr -f 1090M -s 2.4M -g 19.7 -p 0 -b 32768 - \
           | tee_power --out /tmp/tee_power.csv \
           | stream1090 -s 2.4 | <本脚本 TCP 转发 → 127.0.0.1:30001>
       启动前置检查：readsb 必须已在 --net-only 运行；rtl_sdr / tee_power /
       stream1090 必须可执行（获取与编译由 install.sh 负责，见 tee_power/README.md）。
       不满足则带提示退出(2)。
       管道任一环断开 ⇒ 直接退出(3)，重启交给 systemd（不在进程内做自愈）。
       ⚠ net-only 下 readsb 不跑解调 ⇒ last1min.local 整个对象为空
         ⇒ 观测只能取管道内的 tee_power（直接看 UC8 样本，200 ms 粒度）。
         它是**唯一观测源**，不是可选转发环节，所以不能省。
         取它的 **mean_dbfs** 并在最近 30 s 内取中位数：与 readsb 的 noise
         同口径（实测残差 0.14 dB），故 C_MIN / C_MAX 判据阈值原样沿用。
         ⚠ 不要用 p50_dbfs：它比 readsb 口径低 2–7 dB 且随空情漂。
         peak 在 tee 口径下未与 readsb 标定对齐 ⇒ 不参与判据（日志打 peak=n/a），
         工况判定由 c_rms 域判据独占。
       ⚠ tee_power 必须插在 stream1090 之前（它透传 UC8、不改一个字节）；
         stream1090 输出的是 AVR 文本、不带 RSSI，无法反推底噪。

工程模式（⚠ 普通用户不要用，改错会直接把接收搞坏）
------------------------------------------------------------------------
  仅用于标定、排障与实验。以下开关都会改变工作点或判据，
  **误用会导致解码率下降甚至报文归零**：

    --cal CODE          手工指定起点/标定码
    --pin CODE          无条件钉码（关 AGC + 钉 0x13）
    --selftest N        真写 0x13 做通道自检（跑完自动钉回起点码）
    --c-min / --c-max   改判据可行域边界
    --raise-ceiling     改升档天花板
    --dry-run           只观测不写入（默认会写入）

  说明：`--cal` 在普通模式下**不需要**给；它只在"想强制某个
  标定落点"的工程场景才有意义。
"""
import argparse
import atexit
import contextlib
import ctypes
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time

try:
    import fcntl
except ImportError:          # Windows：仅用于本地离线回放，节点上是 Linux
    fcntl = None

# ---------------------------------------------------------------- 常量

# 8bit 满量程码值 <- readsb noise(dBFS)
def c_rms(noise_dbfs):
    return 128.0 * (10.0 ** (noise_dbfs / 20.0))


# 判据下界：量化分辨率不足则 ADC 效率下降。实测把健康态(2.93/10.17)与
# AGC 压缩态(0.71/0.87)干净分开。
C_MIN_DEFAULT = 1.3

# 1 码 = 1.55 dB（实测 0x0a(-38.9) -> 0x12(-26.5)：8 码 / 12.4 dB）
DB_PER_CODE = 1.55

# 饱和兜底：peak 达到 0 dBFS 附近才谈得上 ADC 撞顶。
# 健康态实测最大 -0.8 dBFS（.85 过顶航班），故这个门限不会常态误触发。
SAT_LIMIT_DBFS = -0.2

# 升档天花板 = CODE_RAISE_MAX（见上），取**码值硬上限**而非标定码：
#   标定码是机器绑定的，未知机器上默认码偏低时，欠驱动会被天花板卡住、守卫纠正不动。
#   用硬上限后，升档仍是一格一格 + 收敛门禁 + SETTLE 节流，
#   且一旦升过头，c_max 判据会把它降回来 ⇒ 双向自纠、闭环收敛。

# 窗口收敛门禁：工况判定前要求最近 STABLE_N 个样本的 c_rms 极差 <= STABLE_DB。
# 默认 5 个样本 = 75 s（间隔 15 s）≈ 一个完整 readsb 窗口。
STABLE_N = 5
STABLE_DB = 3.0

# 一次动作后必须等待的时间（秒）：readsb last1min 是 60s 滑动窗、每 10s 刷新，
# 写入到噪声稳定至少要覆盖一个完整窗。
SETTLE_S = 70.0

# 两次写之间的最小间隔（防抖）。守卫的写是幂等且带回读校验的，
# 但第三方（readsb 启动序列）可能连续写好几次 ⇒ 间隔不宜过大。
MIN_WRITE_GAP_S = 15.0

# 滑动窗口内动作次数上限，超过即退避（防第三方持续抢写导致的空转）
MAX_ACTIONS_PER_WINDOW = 4
ACTION_WINDOW_S = 300.0

LOCK_PATH = "/tmp/fc0013_guard.lock"

# ---------------------------------------------------------------- 观测源
# 两种部署形态，由 `--mode` 显式指定（默认 readsb）：
#   ① --mode readsb（默认）：readsb 独占 SDR 直采
#        观测 = stats.json 的 last1min.local.noise / peak_signal
#        ⚠ **严格只用 stats**，不做任何回退：stats 读不到就报"不可读"跳过本轮。
#   ② --mode s1090：stream1090 + readsb --net-only（readsb 只是纯解码器，不摸 SDR）
#        管道 = rtl_sdr -f 1090M -s 2.4M -g 19.7 -p 0 -b 32768 - \
#               | tee_power --out /tmp/tee_power.csv \
#               | stream1090 ... | <本脚本内置 TCP 转发 → 127.0.0.1:30001>
#        ⚠ net-only 下 readsb 不跑解调 ⇒ last1min.local 整个对象为空
#          ⇒ 观测只能取自管道内的 tee_power（它直接看 UC8 样本）
#          ⇒ tee_power 是**唯一观测源**，不是可选转发环节，必须可用。
STATS_DEFAULT = "/run/readsb/stats.json"
TEE_DEFAULT = "/tmp/tee_power.csv"

# `--mode` 取值
MODE_READSB = "readsb"
MODE_S1090 = "s1090"
MODES = (MODE_READSB, MODE_S1090)

# s1090 模式的管道参数（rtl_sdr 段保持与直采形态一致，否则工作点不可比）。
# 管道由本脚本用 socket 转发直连 readsb 的 net-ri 端口，不经 socat。
S1090_RTL_ARGS = ["-f", "1090000000", "-s", "2400000", "-g", "19.7",
                  "-p", "0", "-b", "32768", "-"]
S1090_PORT_DEFAULT = 30001
S1090_TRIM_ROWS = 3000          # tee CSV 每小时截到最近 N 行（7 行/s ≈ 20 MB/天，会撑满 /tmp）
S1090_TRIM_S = 3600.0

# tee 口径 → readsb 口径（守卫标度）的偏置，dB。
#   实测（2026-10-24 .84，同码 0x0c，管道↔直采 A-B-A 切换对照）：
#     readsb noise = −27.7 dBFS ；tee mean_dbfs 窗口聚合 = −27.84 dBFS
#     ⇒ 残差 0.14 dB，取 0（不补偿；±1 dB 在当前 11.9 dB 宽的可行域内无实质影响）
#   ⚠ 只做过 1 轮 A-B-A（p50 侧 2/2 可复现），属**初步标定**；待 ≥3 轮复现后再升级为主册结论。
OBS_OFFSET_DB = 0.0

# tee_power 的哪一列当底噪：
#   2 = p50_dbfs（无脉冲子块的分位数）  —— 低空情依赖，但**与 readsb 口径差 2–7 dB 且随空情漂**
#   3 = mean_dbfs（子块功率均值）        —— 与 readsb noise 同口径（实测残差 0.14 dB）
# 实测（.84 @0x0c）：readsb −27.7 / mean −27.84 / p50 −34.1…−29.7（两分钟内漂 4.4 dB）
# ⇒ 取 mean。判据阈值 C_MIN/C_MAX 是按 readsb noise 标定的，必须跟它同口径，
#   而不是跟"最安静的块"同口径。
TEE_COL = 3

# tee_power 默认 200 ms 出一行；超过这个时长没有新行 ⇒ 管道已停/未启动
TEE_STALE_S = 15.0

# tee 源的聚合窗与最小样本数。
#   ⚠ 单行也有 ±1–2 dB 抖动，必须窗口聚合；30 s 与 readsb 的 60 s 窗同量级，
#     且远大于 15 s 采样间隔 ⇒ 相邻采样点高度重叠，收敛门禁才有意义。
TEE_WINDOW_S = 30.0
TEE_MIN_ROWS = 10
TEE_READ_TAIL = 96 * 1024       # 只读文件尾部（30 s ≈ 150–300 行 ≈ 15–20 KB）

# ---------------------------------------------------------------- 普适常量
# ⚠ 本脚本是**通用**脚本：不得出现任何与特定机器/特定天线/特定标定结果绑定的
#   取值（无逐机表、无按节点分支）。任何机器上都应能自行收敛到可行域内。

# 冷启动默认起点码（未知 FC0013、且读不回 0x13 时用）。
#   推导（全部来自实测，可复算）：
#     .86 开路（无外部信号）实测链路自身底噪：0x0a 档 0.736 码、0x11 档 2.998 码
#     ⇒ 1.743 dB/码；按本文件保守取值 DB_PER_CODE=1.55 dB/码 复算同样成立。
#     0x0a 档自身底噪 0.736 码 < C_MIN(1.3) ⇒ **低于 0x0d 一定欠驱动**。
#     0x0d: 0.736 × 10^(3×1.55/20) = 1.26 码 ≈ C_MIN（临界，无余量）
#     0x0e: 0.736 × 10^(4×1.55/20) = 1.50 码 > C_MIN（余量 15%）
#   ⇒ 取 0x0e：即便天线开路/外部噪声为零也不欠驱动，且距上界有 14 格余量。
DEFAULT_CODE = 0x0E

# 可行域上界（过驱动判据），与机器无关。
#   实测悬崖：c ≥ 40 码 ⇒ 报文全灭。取 40 的一半 = 20 码（6 dB 余量）。
#   两机健康工作点（.84 c≈3.1 / .85 c≈11.7）都远低于它 ⇒ 常态绝不误动；
#   真过驱动（AGC 开后实测 .84 c=38.66）必触发。削波另有 peak 兜底。
C_MAX_DEFAULT = 20.0

# 升档天花板（硬码上限）：0x1c 是实测悬崖码，0x18 距它 4 格 ≈ 6.2 dB。
# ⚠ 不能用 peak 余量当升档门限：peak 是**空情量**（过顶航班决定），
#   安静时段 peak 很低、看起来余量充足 → 守卫误升码 → 等航班来了撞 0 dBFS。
CODE_RAISE_MAX = 0x18

# 降档地板：增益再低下去已无意义（外部噪声极强时才可能用到）。
CODE_FLOOR = 0x08


# ---------------------------------------------------------------- 决策核心（纯函数，便于离线回放）

def lower_target(c, cur, cfg):
    """过驱动的降档目标：按超出量算格数，把 c 拉回可行域几何中点。

    ⚠ 与机器无关（不查任何标定值）：目标 c = sqrt(c_min*c_max) 是域的几何中点，
      上下都留余量，避免"降过头 → 一格一格升回来"的振荡。至少降 1 格。
    """
    tgt_c = math.sqrt(cfg["c_min"] * cfg["c_max"])
    steps = int(round(20.0 * math.log10(max(c, 1e-9) / tgt_c) / DB_PER_CODE))
    return max(CODE_FLOOR, min(cur - 1, cur - max(1, steps)))


def decide_band(now, n, peak, cfg, state):
    """工况判据：**只用 stats + state['recorded']，不访问 FC0013**。

    返回 (need, target, reason)
      need=False ⇒ 不介入：不读不写 FC0013
      need=True  ⇒ 需要介入；target = 目标码（None = 无可写目标，仅核对 AGC/第三方写）
      reason     = 人类可读理由（供日志与离线回放断言）

    门禁顺序：收敛门禁 → SETTLE 门禁 → 工况判据。
    ⚠ 目标码由本函数显式给出，**不要靠匹配 reason 字符串反推**。
    """
    # --- 收敛门禁：readsb 的 noise 是 60 s 滑动窗 + 估计器慢收敛，
    #   任何扰动（我们自己的写、readsb 重启）之后读数会漂几十秒。
    #   实测（10-22 15:38 readsb 重启后）：写回后 c=51→42→32→29 一路在降，
    #   此刻的"过驱动"是**暂态不是工况**，若照判会误报。
    #   ⇒ 要求最近 STABLE_N 个样本（默认 5 个 = 75 s，覆盖一个完整窗口）
    #     的 c_rms 极差 <= STABLE_DB 才允许做工况判定。
    #     3 样本(45s) 不够：暂态是"缓慢"收敛的，早期样本彼此很接近。
    hist = state.get("hist") or []
    if len(hist) < STABLE_N:
        return False, None, "UNSTABLE 样本不足（%d/%d），暂不判工况" % (
            len(hist), STABLE_N)
    w = hist[-STABLE_N:]
    span_db = 20.0 * math.log10(max(w) / max(min(w), 1e-9))
    if span_db > STABLE_DB:
        return False, None, "UNSTABLE c_rms 仍在收敛（%.1f dB 极差）%s" % (
            span_db, " ".join("%.1f" % x for x in w))

    # --- SETTLE 门禁：写入后噪声需覆盖一个完整窗才有意义。
    #   未满 ⇒ 不判工况 ⇒ 不介入 ⇒ 不访问 FC0013。
    if (now - state.get("last_write", 0.0)) < SETTLE_S:
        return False, None, "SETTLE 未满 %.0fs，暂不判工况" % SETTLE_S

    # --- 工况判据 ---
    # "当前码"取 state['recorded']（我方上次写入的码 = 增益起点值）。
    # 常态下它与 0x13 一致；一旦第三方写过 0x13，介入路径会先经 canary 分支
    # 写回 recorded，从而恢复自洽。
    c = c_rms(n)
    cur = state["recorded"] & 0x1F
    if c < cfg["c_min"]:
        # 欠驱动：抬码。逐格，天花板 = 码值硬上限（与机器、空情都无关）
        ceiling = min(cfg.get("raise_ceiling", CODE_RAISE_MAX) & 0x1F,
                      CODE_RAISE_MAX)
        nxt = cur + 1
        if nxt > ceiling:
            return True, None, "UNDERDRIVEN c=%.2f<%.2f 但已到码上限 %02x" % (
                c, cfg["c_min"], cur)
        return True, nxt, "UNDERDRIVEN c=%.2f<%.2f ⇒ 升一格 %02x->%02x" % (
            c, cfg["c_min"], cur, nxt)
    if c > cfg["c_max"]:
        # 过驱动：按超出量降档到域的几何中点（不查标定值）
        tgt = lower_target(c, cur, cfg)
        if tgt == cur:
            return True, None, "OVERDRIVEN c=%.2f 但已到码地板 %02x（码外通道，需人工）" % (
                c, cur)
        return True, tgt, "OVERDRIVEN c=%.2f>%.2f ⇒ 降档 %02x->%02x" % (
            c, cfg["c_max"], cur, tgt)
    if peak >= SAT_LIMIT_DBFS:
        # 削波兜底：peak 是空情量，与 c 无关 ⇒ 不能按 c 算步长，固定降 2 格
        tgt = cur - 2
        if tgt < CODE_FLOOR:
            return True, None, "CLIP peak=%.1f 但已到码地板 %02x（需人工）" % (
                peak, cur)
        return True, tgt, "CLIP peak=%.1f ⇒ 降 2 格 %02x->%02x" % (peak, cur, tgt)
    return False, None, "OK c=%.2f peak=%.1f code=%02x" % (c, peak, cur)


def decide_action(d, i, band_target, band_reason, state):
    """介入决策：**此时才允许看 FC0013 寄存器**。

    返回 (action, target, reason, kind)
      action ∈ {'AGCOFF', 'WRITE', 'NONE'}
        AGCOFF：AGC 被第三方打开 ⇒ 先关 AGC，再写回起点
        WRITE  ：写 0x13 = target
        NONE   ：不动（无可写目标，需人工）
      kind ∈ {'canary', 'band', None}
        canary=第三方写探针类动作（不依赖 stats）
        band=工况判据类动作（依赖 stats）
    """
    recorded = state["recorded"]
    # 0x0d bit3==0 ⇒ 芯片 AGC 使能
    if not (d & 0x08):
        return "AGCOFF", recorded, "AGC_ON d=%02x ⇒ 第三方打开 AGC，写回起点 %02x" % (
            d, recorded), "canary"
    if (i & 0x1F) != (recorded & 0x1F):
        # 0x13 与我方记录不一致 ⇒ 有第三方写过 fc0013 ⇒ 写回"增益起点值"
        return "WRITE", recorded, "CANARY 0x13=%02x != 记录 %02x ⇒ 第三方写过，写回起点" % (
            i, recorded), "canary"
    if band_target is None:
        return "NONE", None, "%s ⇒ 无可写目标（需人工）" % band_reason, None
    if (band_target & 0x1F) == (i & 0x1F):
        return "NONE", None, "%s ⇒ 目标码与当前码相同，无需写" % band_reason, None
    return "WRITE", band_target, "%s ⇒ 写 %02x" % (band_reason, band_target), "band"


# ---------------------------------------------------------------- I2C 通道（裸 libusb）

USB_VID, USB_PID = 0x0BDA, 0x2832
FC_ADDR = 0xC6
DEV_LOCK = "/tmp/fc_daemon.lock"     # 与 fc_dump.py / fc_daemon 共用，避免错位读
CTL_MS = 300


class Fc(object):
    """FC0013 的 I2C-over-USB 通道：ctypes 直调 libusb 控制传输（不依赖 fc_daemon）。

    复刻 fc_daemon.c 中守卫真正用到的面：rep_on / rdreg / wrreg / flock /
    write_verified（写→读回→重试）/ read_trusted（三重校验+值域白名单）/
    写前先关 AGC 的严格顺序。不实现 repl / watch / 日志轮转——按需介入用不到。

    ⚠ 三条实现红线：
      1. 写必须**一次**控制传输传 [reg, val] 两字节、wIndex=0x0610；拆两次或换
         wIndex 会 USB 返回成功而 I2C 静默失败（读回仍是旧值）。
      2. **设备句柄每事务开关**，只有 libusb context 常驻。常驻句柄跑久会失效
         （fc_daemon watch 实测 44% 采样 UNTRUSTED (d=ff i=ff)）；且每个事务
         前都要 rep_on——别的进程退出时的 rep_off() 会让后续读全部 -1。
      3. **绝不走 librtlsdr**：rtlsdr_open() 会跑一遍 tuner init 并可能与
         readsb 抢设备。只做裸控制传输，不 claim 接口、不碰 bulk 端点。
    """

    def __init__(self):
        self.lib = ctypes.CDLL("libusb-1.0.so.0")
        self.lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.lib.libusb_init.restype = ctypes.c_int
        self.lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
        self.lib.libusb_open_device_with_vid_pid.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
        self.lib.libusb_close.argtypes = [ctypes.c_void_p]
        self.lib.libusb_control_transfer.restype = ctypes.c_int
        self.lib.libusb_control_transfer.argtypes = [
            ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
            ctypes.c_uint16, ctypes.c_uint16,
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_int]
        ctx = ctypes.c_void_p()
        if self.lib.libusb_init(ctypes.byref(ctx)) != 0:
            raise RuntimeError("libusb_init failed")
        self.ctx = ctx

    def _out(self, h, wValue, wIndex, data):
        buf = (ctypes.c_ubyte * len(data))(*data)
        return self.lib.libusb_control_transfer(
            h, 0x40, 0x00, wValue, wIndex, buf, len(data), CTL_MS)

    def _rep(self, h, on):
        """i2c repeater 开(0x18)/关(0x10)。开要重试：新进程首拉常失败。"""
        for _ in range(5 if on else 1):
            if self._out(h, (0x01 << 8) | 0x20, 0x10 | 1,
                         [0x18 if on else 0x10]) >= 0:
                return True
            time.sleep(0.05)
        return False

    @contextlib.contextmanager
    def txn(self):
        """一个 I2C 事务：抢 flock → 开句柄 → repeater 开；退出时 rep_off + 关句柄。
        开不出来 yield None，调用方按"设备不可用"处理，不写。"""
        fh = open(DEV_LOCK, "a+") if fcntl else None
        if fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
        h = None
        try:
            for _ in range(5):                    # 与 fc_daemon dev_attach 同策略
                h = self.lib.libusb_open_device_with_vid_pid(
                    self.ctx, USB_VID, USB_PID)
                if h and self._rep(h, True):
                    break
                if h:
                    self.lib.libusb_close(h)
                h = None
                time.sleep(0.2)
            yield h
        finally:
            if h:
                self._rep(h, False)
                self.lib.libusb_close(h)
            if fh:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()

    def rdreg(self, h, reg):
        if self._out(h, FC_ADDR, 0x0610, [reg]) < 0:
            return None
        buf = (ctypes.c_ubyte * 1)()
        if self.lib.libusb_control_transfer(
                h, 0xC0, 0x00, FC_ADDR, 0x0600, buf, 1, CTL_MS) < 0:
            return None
        return buf[0]

    def wrreg(self, h, reg, val):
        return self._out(h, FC_ADDR, 0x0610, [reg, val]) >= 0

    def write_verified(self, h, reg, val, tries=4):
        """写 → 等 20 ms → 读回；不符重试。返回 (ok, 用了几次)。"""
        for i in range(tries):
            self.wrreg(h, reg, val)
            time.sleep(0.02)
            if self.rdreg(h, reg) == val:
                return True, i + 1
            time.sleep(0.01)
        return False, tries

    def read_013(self):
        """读 0x0d/0x13/0x14，三重校验：3 次尝试 + 值域白名单（0x0d 高 4 位、
        0x13 高 3 位须为 0）+ 连读两次一致。返回 (d, i, l) 或 None=不可信
        （**不可信时绝不据此写**）。"""
        with self.txn() as h:
            if not h:
                return None
            for _ in range(3):
                d, i, l = (self.rdreg(h, r) for r in (0x0D, 0x13, 0x14))
                if (d is not None and i is not None and l is not None
                        and (d & 0xF0) == 0 and (i & 0xE0) == 0
                        and self.rdreg(h, 0x0D) == d and self.rdreg(h, 0x13) == i):
                    return (d, i, l)
                time.sleep(0.015)
        return None

    def set_code(self, code, agcoff_first=True):
        """关 AGC(0x0d=09) → 钉 IF 增益码(0x13)，每步写后读回校验、重试 4 次。
        顺序不可颠倒：AGC 开着时 0x13 会被芯片立刻改掉。返回 (ok, 说明)。"""
        code &= 0x1F
        with self.txn() as h:
            if not h:
                return False, "device open failed"
            parts = []
            if agcoff_first:
                ok, used = self.write_verified(h, 0x0D, 0x09, 4)
                parts.append("agcoff rc=%d tries=%d" % (0 if ok else 1, used))
                if not ok:
                    return False, " | ".join(parts)
            ok, used = self.write_verified(h, 0x13, code, 4)
            parts.append("set %02x rc=%d tries=%d" % (code, 0 if ok else 1, used))
            return ok, " | ".join(parts)

    def selftest(self, n=20):
        """循环写 0x0a..0x1c，统计"写-读回一致"率（对齐 fc_daemon selftest 判据）。
        ⚠ 真写 0x13，末项停在 0x1c（悬崖码）——调用方须随后钉回标定码。"""
        seq = [0x0A, 0x0C, 0x0F, 0x10, 0x12, 0x14, 0x16, 0x18, 0x1A, 0x1C]
        ok_n = first = extra = 0
        bad = []
        with self.txn() as h:
            if not h:
                return None
            for i in range(n):
                v = seq[i % len(seq)]
                ok, used = self.write_verified(h, 0x13, v, 4)
                if ok:
                    ok_n += 1
                    if used == 1:
                        first += 1
                    else:
                        extra += used - 1
                else:
                    bad.append("i=%d set=%02x got=%s tries=%d"
                               % (i, v, self.rdreg(h, 0x13), used))
        for b in bad:
            print("  FAIL " + b)
        print("RESULT ok=%d fail=%d first_try=%d extra_tries=%d"
              % (ok_n, len(bad), first, extra))
        return ok_n, len(bad), first, extra


# ---------------------------------------------------------------- stats

def _pf(peak, valid):
    """peak 的日志表示：观测源没提供可比 peak 时打 n/a 而不是伪造数值。"""
    return ("%.1f" % peak) if valid else "n/a"


def _ex(st):
    """观测源的附加信息（tee 源的聚合行数），stats 源为空。"""
    return (" rows=%d" % st["n"]) if "n" in st else ""

def read_stats(path=STATS_DEFAULT):
    """形态①（readsb 直采）：last1min.local 的 noise / peak_signal。

    形态②（stream1090 + net-only）下 `local` 整个对象为空 ⇒ 返回 None。
    """
    try:
        with open(path, "r") as f:
            d = json.load(f)
    except (IOError, ValueError):
        return None
    l = d.get("last1min", {}).get("local")
    if not l:
        return None
    try:
        return {
            "noise": float(l["noise"]),
            "peak": float(l["peak_signal"]),
            "strong": int(l.get("strong_signals", 0)),
            "acc": l.get("accepted", [0, 0]),
            "src": "stats",
            "peak_valid": True,
        }
    except (KeyError, TypeError, ValueError):
        return None


def read_tee(path=TEE_DEFAULT, max_age=TEE_STALE_S, window_s=TEE_WINDOW_S):
    """形态②（stream1090 管道）：tee_power CSV 在最近 window_s 内的 mean_dbfs 中位数。

    为什么取 `mean_dbfs` 而不是 `p50_dbfs`（2026-10-24 实测）：
      readsb 的 noise 是"扣信号后的功率均值"，tee 的 mean_dbfs 是子块功率均值
      ⇒ 同口径（同码 0x0c 对照残差 0.14 dB）。
      而 p50_dbfs 代表"最安静的那批子块"，比 readsb 口径低 2–7 dB 且随空情漂
      （实测两分钟内 −34.1→−29.7），判据阈值会跟着漂 ⇒ 不作判据。
      本项目阈值按 readsb noise 标定，必须与它同口径；p50 列仍输出，只作观察。

    ⚠ 仍必须做窗口聚合，不能只看末行：单行 mean 也有 ±1–2 dB 抖动，而守卫
      每 15 s 才采一个点；取最近 30 s 中位数后性质与 readsb 的 60 s 滑窗一致。

    peak：tee 的 peak_dbfs 定义（单分量峰值/2）未与 readsb 的 peak_signal
    标定对齐 ⇒ **不参与判据**，置一个永不触发饱和兜底的值并标记 peak_valid=False。
    """
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-TEE_READ_TAIL, os.SEEK_END)
                head_partial = True
            except OSError:
                f.seek(0)
                head_partial = False
            blob = f.read()
    except IOError:
        return None
    lines = blob.decode("ascii", "replace").splitlines()
    if head_partial and lines:
        lines = lines[1:]               # 首行可能被 seek 截断
    if not lines:
        return None

    now_ms = time.time() * 1000.0
    lo = now_ms - window_s * 1000.0
    vals = []
    newest = None
    for line in lines:
        p = line.split(",")
        if len(p) <= TEE_COL:
            continue
        try:
            t = float(p[0])
            v = float(p[TEE_COL])
        except ValueError:
            continue
        if newest is None or t > newest:
            newest = t
        if lo <= t <= now_ms + 1000.0:
            vals.append(v)
    if newest is None or (now_ms - newest) / 1000.0 > max_age:
        return None                     # 管道已停：宁可报不可读，也不要用陈旧值判工况
    if len(vals) < TEE_MIN_ROWS:
        return None                     # 窗内样本太少，不足以代表底噪
    vals.sort()
    return {
        "noise": vals[len(vals) // 2] + OBS_OFFSET_DB,
        "peak": SAT_LIMIT_DBFS - 1.0,   # 占位（见 docstring）
        "strong": 0,
        "acc": [0, 0],
        "src": "tee",
        "peak_valid": False,
        "n": len(vals),
    }


def observe(mode, stats_path=STATS_DEFAULT, tee_path=TEE_DEFAULT):
    """按 `--mode` 取观测源，**严格单源、不互相回退**。

    为什么要"严格"而不是"自动探测"（2026-10-24 定）：
      自动探测看似方便，但会让"配置错了"伪装成"正在正常工作"——
      `--mode readsb` 下 stats 读不到（readsb 没跑/路径改了）却悄悄改用 tee 的
      残留 CSV，操作者看不出部署形态已经和 `--mode` 不符。模式既然由人显式指定，
      就让它成为唯一的判据来源：不符 ⇒ 报不可读，而不是替人猜。

    返回 None 表示"本轮无有效观测"，调用方按原逻辑跳过本轮（不伪造 noise=0）。
    """
    if mode == MODE_S1090:
        return read_tee(tee_path)
    return read_stats(stats_path) if stats_path else None


# ---------------------------------------------------------------- s1090 模式

def _proc_argv():
    """遍历 /proc 取 (pid, argv)，用于判断 readsb 的真实启动参数。"""
    for name in os.listdir("/proc"):
        if not name.isdigit() or name in (str(os.getpid()), str(os.getppid())):
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as f:
                raw = f.read()
        except (IOError, OSError):
            continue
        if raw:
            yield name, [a for a in raw.decode("utf-8", "replace").split("\0") if a]


def find_exe(name):
    """定位可执行文件：先 PATH，再常见目录（tee_power/stream1090 常被放 /tmp）。"""
    import shutil
    p = shutil.which(name)
    if p:
        return p
    for d in ("/tmp", "/usr/local/bin", "/usr/bin"):
        c = os.path.join(d, name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def check_readsb_net_only():
    """readsb 必须是 --net-only，否则 SDR 被它独占，管道起不来。

    判据取运行中进程的 argv，不看 /etc/default/readsb：配置文件是"打算怎么启动"，
    进程实参才是"实际怎么启动"（改了配置没重启时只有后者不骗人）。
    """
    for pid, argv in _proc_argv():
        if os.path.basename(argv[0]) != "readsb":
            continue
        if "--net-only" in argv:
            return True, "readsb(pid %s) 已是 --net-only" % pid
        return False, "readsb(pid %s) 未启用 --net-only：%s" % (
            pid, " ".join(argv)[:140])
    return False, "未发现运行中的 readsb 进程"


class S1090Pipe(object):
    """`rtl_sdr | tee_power | stream1090 | → readsb --net-ri-port`，取代 s1090_pipe.sh + socat。

    tee_power 必须留着：它不是转发环节，是**唯一观测源**（--net-only 下 readsb 不跑
    解调，stats.json 的 last1min.local 整个为空）。去掉的只是 socat 那一段。

    崩溃即退出（os._exit 非 0），由 systemd 的重启策略接管；子进程与守卫同属一个
    cgroup，systemd 停/重启服务时会一并清理。
    """

    def __init__(self, tee_path, port, logf):
        self.tee_path = tee_path
        self.port = port
        self.logf = logf
        self._halt = threading.Event()
        self.procs = []

    def _err(self, name):
        try:
            return open("/tmp/s1090_%s.err" % name, "ab")
        except (IOError, OSError):
            return subprocess.DEVNULL

    def _die(self, why):
        log("PIPE 死亡：%s ⇒ 退出，交由 systemd 重启" % why, self.logf)
        sys.stdout.flush()
        os._exit(3)

    def start(self, rtl, tee, s1090):
        """起管道。参数默认 = 生产（rtl 2.4 MSPS，stream1090 `-s 2.4`）。

        两个环境变量可覆盖，**只为做参数对照实验**，不设时行为与以前逐字节一致：
          S1090_RTL_S   rtl_sdr 的 -s 值（Hz），默认 2400000
          S1090_ARGS    stream1090 的完整参数串，默认 "-s 2.4"
        ⚠ 两处必须一致：`-s` 是 stream1090 对输入流的**声明**，与 rtl_sdr 的实际
          采样率不符时会解析错帧率（表现为解出乱码/零报文），不是报错退出。
        """
        env = dict(os.environ)
        env["STREAM1090_MAX_DROPS_PER_MIN"] = "0"
        rtl_args = list(S1090_RTL_ARGS)
        env_s = os.environ.get("S1090_RTL_S", "").strip()
        if env_s:
            rtl_args[rtl_args.index("-s") + 1] = env_s
        s_args = os.environ.get("S1090_ARGS", "").split() or ["-s", "2.4"]
        p1 = subprocess.Popen([rtl] + rtl_args, stdout=subprocess.PIPE,
                              stderr=self._err("rtl_sdr"))
        p2 = subprocess.Popen([tee, "--out", self.tee_path], stdin=p1.stdout,
                              stdout=subprocess.PIPE, stderr=self._err("tee_power"))
        p3 = subprocess.Popen([s1090] + s_args, stdin=p2.stdout,
                              stdout=subprocess.PIPE, stderr=self._err("stream1090"),
                              env=env)
        p1.stdout.close()
        p2.stdout.close()
        self.procs = [p1, p2, p3]
        log("PIPE 启动 %s | %s | %s %s → 127.0.0.1:%d"
            % (rtl, tee, s1090, " ".join(s_args), self.port), self.logf)
        threading.Thread(target=self._forward, daemon=True).start()
        threading.Thread(target=self._trim_loop, daemon=True).start()

    def _forward(self):
        """stream1090 stdout → readsb 30001，取代 `socat -u - TCP4:...`。"""
        out = self.procs[-1].stdout
        sock = None
        while True:
            chunk = out.read1(8192) if hasattr(out, "read1") else out.read(8192)
            if not chunk:
                self._die("stream1090 输出结束(rc=%s)，见 /tmp/s1090_stream1090.err"
                          % self.procs[-1].poll())
                return
            if sock is None:
                try:
                    sock = socket.create_connection(("127.0.0.1", self.port), timeout=5.0)
                    sock.settimeout(None)
                    log("PIPE 已接上 readsb 127.0.0.1:%d" % self.port, self.logf)
                except OSError as e:
                    self._die("连不上 127.0.0.1:%d（%s）—— readsb 是否在监听 "
                              "--net-ri-port？" % (self.port, e))
                    return
            try:
                sock.sendall(chunk)
            except OSError as e:
                self._die("转发中断（%s）—— readsb 是否已退出？" % e)
                return

    def shutdown(self):
        """收尾（--once 用）：守卫退出后管道不该继续占着 SDR。"""
        self._halt.set()
        for p in self.procs:
            if p.poll() is None:
                p.terminate()

    def _trim_loop(self):
        """tee CSV 约 7 行/s ≈ 20 MB/天，不截会撑满 /tmp。

        ⚠ 必须原地覆盖同一 inode（r+b + truncate），不能 os.replace 换文件：
          tee_power 以追加方式持有旧 fd，换了 inode 后它继续写已 unlink 的文件，
          观测值会冻结在截断那一刻。
        """
        while not self._halt.wait(S1090_TRIM_S):
            try:
                with open(self.tee_path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    back = min(size, 2 * 1024 * 1024)
                    f.seek(size - back)
                    blob = f.read()
                lines = blob.splitlines()
                if back < size:
                    lines = lines[1:]           # 首行可能被 seek 截断
                data = b"\n".join(lines[-S1090_TRIM_ROWS:]) + b"\n"
                with open(self.tee_path, "r+b") as g:
                    g.seek(0)
                    g.write(data)
                    g.truncate()
            except (IOError, OSError) as e:
                log("PIPE tee CSV 截断失败（%s）" % e, self.logf)


def prepare_s1090(tee_path, port, logf):
    """s1090 前置检查 + 起管道。返回 (pipe, 退出码)：pipe 为 None 表示检查不通过。

    检查不通过就带提示退出（而不是降级去读 stats）：模式是人显式选的，
    配置不符时应当报错，不要让守卫静默地按另一种形态工作。
    """
    ok, why = check_readsb_net_only()
    if not ok:
        log("MODE s1090 检查失败：%s" % why)
        log('处理：把 readsb 改成纯解码器 —— RECEIVER_OPTIONS="--net-only"，'
            "并确认 NET_OPTIONS 含 --net-ri-port %d" % port)
        return None, 2
    log("MODE s1090 检查①通过：%s" % why)

    exes = {}
    for n in ("rtl_sdr", "tee_power", "stream1090"):
        p = find_exe(n)
        if not p:
            log("MODE s1090 检查失败：找不到 %s" % n)
            log("处理：安装/编译 %s（install.sh 负责部署 tee_power；"
                "rtl_sdr/stream1090 需自行就位）。"
                "若只想直采，去掉 --mode %s" % (n, MODE_S1090))
            return None, 2
        exes[n] = p
    log("MODE s1090 检查②通过：%s | %s | %s"
        % (exes["rtl_sdr"], exes["tee_power"], exes["stream1090"]))

    pipe = S1090Pipe(tee_path, port, logf)
    try:
        pipe.start(exes["rtl_sdr"], exes["tee_power"], exes["stream1090"])
    except OSError as e:
        log("MODE s1090 管道启动失败：%s" % e)
        return None, 2
    return pipe, 0




# ---------------------------------------------------------------- 主循环

def log(msg, path=None, verbose=True):
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    if verbose:
        print(line)
        sys.stdout.flush()
    if path:
        try:
            with open(path, "a") as f:
                f.write(line + "\n")
        except IOError:
            pass


def acquire_lock(path=LOCK_PATH):
    if fcntl is None:
        return open(os.devnull, "w")     # 非 Linux：跳过加锁（仅离线回放会走到）
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        return None
    f.write("%d\n" % os.getpid())
    f.flush()
    return f


def resolve_code(given, fc):
    """起点码来源（优先级）：显式 --cal > 启动读回 0x13 > DEFAULT_CODE。

    ⚠ **永不返回 None**：脚本是通用的，不得因"未知机器/读不回"拒绝启动。
      落到 DEFAULT_CODE 时 need_write=True ⇒ 调用方写一次使其成立，
      避免 recorded 与实际码不符（否则首次介入会把机器拉到这个默认值上而不自知）。

    返回 (code, 来源说明, need_write)
    """
    if given:
        return int(given, 0) & 0x1F, "--cal 显式指定", False
    if fc is not None:
        regs = fc.read_013()
        if regs:
            return regs[1] & 0x1F, "启动读回 0x13", False
    return DEFAULT_CODE, "默认起点码（0x13 读不回，未实测）", True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=socket.gethostname(),
                    help="节点标识（仅日志用，默认取主机名）")
    ENG = ("【工程模式·普通用户不要用】")
    ap.add_argument("--cal", default=None,
                    help=ENG + " 起点/标定码，如 0x11。缺省：启动读一次 0x13；"
                         "读不回则用普适默认码 0x%02x（不拒绝启动）" % DEFAULT_CODE)
    ap.add_argument("--c-min", type=float, default=C_MIN_DEFAULT,
                    help=ENG + " 可行域下界（码）")
    ap.add_argument("--c-max", type=float, default=0.0,
                    help=ENG + " 可行域上界（码）。0 = 用普适默认 %.1f 码"
                               "（实测悬崖 40 码的 1/2）" % C_MAX_DEFAULT)
    ap.add_argument("--raise-ceiling", type=int, default=0,
                    help=ENG + " 升档天花板（码）。0 = 用硬上限 0x%02x"
                               % CODE_RAISE_MAX)
    ap.add_argument("--mode", choices=MODES, default=MODE_READSB,
                    help="部署形态（默认 %s）：\n"
                         "  %s = readsb 独占 SDR 直采，观测只读 stats.json\n"
                         "  %s  = stream1090 + readsb --net-only，本脚本自建 "
                         "rtl_sdr|tee_power|stream1090 管道并用 TCP 转发给 readsb，"
                         "观测读 tee_power CSV（需 rtl_sdr/tee_power/stream1090 已就位）"
                         % (MODE_READSB, MODE_READSB, MODE_S1090))
    ap.add_argument("--net-ri-port", type=int, default=S1090_PORT_DEFAULT,
                    help="仅 %s 模式：readsb 的 --net-ri-port（默认 %d）"
                         % (MODE_S1090, S1090_PORT_DEFAULT))
    ap.add_argument("--stats", default=STATS_DEFAULT,
                    help=ENG + " readsb stats.json 路径（仅 %s 模式使用）" % MODE_READSB)
    ap.add_argument("--tee", default=TEE_DEFAULT,
                    help="tee_power CSV 路径（仅 %s 模式使用；既是观测源，"
                         "也是管道写出的位置）" % MODE_S1090)
    ap.add_argument("--pin", metavar="CODE", default=None,
                    help=ENG + " 无条件钉码：关 AGC(0x0d=09) + 钉 0x13=CODE "
                         "并读回校验。供启动对齐/人工钉码用，不受工况判据约束")
    ap.add_argument("--selftest", type=int, default=0,
                    help=ENG + " 自检：循环写 0x0a..0x1c 统计写-读回一致率"
                         "（**真写**！）跑完自动钉回起点码")
    ap.add_argument("--log", default="/tmp/fc_guard.log")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--once", action="store_true", help=ENG + " 只跑一轮")
    ap.add_argument("--apply", action="store_true",
                    help="兼容旧命令：写入已是默认行为，可省略")
    ap.add_argument("--dry-run", action="store_true",
                    help="只观测、不写芯片（默认会写入）")
    args = ap.parse_args()

    try:
        fc = Fc()
    except OSError as e:
        # libusb 拉不起来（库缺失 / 设备栈未就绪）时不当场崩：工况判据只读 stats，
        # 通道不可用 ⇒ 守卫降级为纯观测器（照常报警，只是介入不了）。
        fc = None
        log("CHANNEL 不可用（%s）⇒ 只观测，无法介入" % e, args.log)

    if args.pin:
        # 无条件钉码（等价 fc_daemon `set CODE --assert`）：不过工况判据，
        # 也不依赖标定码 ⇒ 放在 resolve_cal 之前，未知节点也能钉。
        # 守卫按需介入 ⇒ 启动后不会自动钉，开机对齐靠本参数做一次性钉码。
        if fc is None:
            log("PIN 失败：通道不可用")
            return 1
        target = int(args.pin, 0) & 0x1F
        ok, out = fc.set_code(target)
        log("PIN node=%s -> %02x %s" % (args.node, target,
                                        "OK" if ok else "FAIL " + out))
        return 0 if ok else 1

    cal, cal_src, need_write = resolve_code(args.cal, fc)
    if need_write and fc is not None:
        # 起点未实测 ⇒ 写一次让 recorded 与实际码自洽（否则首次介入会凭空改工作点）
        ok, out = fc.set_code(cal)
        log("ALIGN 起点未实测 ⇒ 写默认码 %02x %s" % (cal, "OK" if ok else "FAIL " + out),
            args.log)
    elif need_write:
        log("WARN 起点用默认码 %02x（通道不可用，未写入、未核实）" % cal, args.log)
    c_max = args.c_max if args.c_max > 0 else C_MAX_DEFAULT
    cfg = {
        "cal": cal,
        "c_min": args.c_min,
        "c_max": c_max,
        "raise_ceiling": (args.raise_ceiling & 0x1F) if args.raise_ceiling
                         else CODE_RAISE_MAX,
    }

    if args.selftest:
        # 自检走独立流程（不加主循环的锁）：等价 fc_daemon selftest 的判据。
        # ⚠ 真写 0x13 ⇒ 跑完必须钉回标定码，否则会停在 0x1c 悬崖码上。
        if fc is None:
            log("SELFTEST 失败：通道不可用")
            return 2
        r = fc.selftest(args.selftest)
        if r is None:
            log("SELFTEST 设备打不开（node=%s）" % args.node)
            return 2
        ok, out = fc.set_code(cal)
        log("SELFTEST node=%s ok=%d fail=%d first_try=%d extra=%d | pin 回 %02x %s"
            % (args.node, r[0], r[1], r[2], r[3], cal, out))
        return 0 if (r[1] == 0 and ok) else 1

    lock = acquire_lock()
    if lock is None:
        log("LOCK 已有实例在跑，退出")
        return 0

    # s1090 模式：前置检查 + 起管道（放在拿锁之后，避免两个实例同时起管道抢设备）
    if args.mode == MODE_S1090:
        pipe, rc = prepare_s1090(args.tee, args.net_ri_port, args.log)
        if pipe is None:
            return rc
        atexit.register(pipe.shutdown)  # --once / 正常退出时不要留下占着 SDR 的子进程

    state = {"recorded": cal, "last_write": 0.0, "actions": [], "hist": []}
    log("START node=%s mode=%s code=%02x(%s) cfg=%s write=%d" % (
        args.node, args.mode, cal, cal_src, cfg, 0 if args.dry_run else 1), args.log)

    while True:
        now = time.time()
        st = observe(args.mode, args.stats, args.tee)
        if st is None:
            log("STATS 不可读（%s 模式唯一观测源无有效数据%s），跳过本轮" % (
                args.mode,
                "：检查 readsb 是否在跑、stats 路径是否正确"
                if args.mode == MODE_READSB else
                "：检查管道是否在更新 tee CSV"), args.log)
            if args.once:
                return 0
            time.sleep(args.interval)
            continue

        c = c_rms(st["noise"])
        state.setdefault("hist", []).append(c)
        state["hist"] = state["hist"][-6:]

        # ---- 第一步：工况判据（只读 stats，不碰 FC0013）----
        need, band_target, band_reason = decide_band(
            now, st["noise"], st["peak"], cfg, state)
        if not need:
            # 不介入 ⇒ 不读不写 FC0013
            log("STATS src=%s n=%.1f peak=%s c=%.2f%s | fc0013=未访问 | %s" % (
                st["src"], st["noise"], _pf(st["peak"], st["peak_valid"]),
                c, _ex(st), band_reason), args.log)
            if args.once:
                return 0
            time.sleep(args.interval)
            continue

        # ---- 第二步：需要介入，才读 FC0013 ----
        if fc is None:
            log("NEED 但通道不可用，无法介入 | %s" % band_reason, args.log)
            if args.once:
                return 1
            time.sleep(min(args.interval, 30))
            continue
        regs = fc.read_013()
        if regs is None:
            log("READ 失败（设备忙/句柄失效），退避", args.log)
            if args.once:
                return 1
            time.sleep(min(args.interval, 30))
            continue
        d, i, l = regs
        action, target, reason, kind = decide_action(
            d, i, band_target, band_reason, state)

        # 写节流
        if action != "NONE":
            gap = now - state["last_write"]
            recent = [t for t in state["actions"] if now - t < ACTION_WINDOW_S]
            if gap < MIN_WRITE_GAP_S:
                action, target, reason = "NONE", None, "THROTTLE 距上次写 %.0fs" % gap
            elif len(recent) >= MAX_ACTIONS_PER_WINDOW:
                action, target, reason = "NONE", None, "THROTTLE %ds 内已动作 %d 次，退避" % (
                    ACTION_WINDOW_S, len(recent))

        log("STATS src=%s n=%.1f peak=%s c=%.2f%s | 0d=%02x 13=%02x 14=%02x | %s -> %s" % (
            st["src"], st["noise"], _pf(st["peak"], st["peak_valid"]),
            c, _ex(st), d, i, l, reason, action), args.log)

        if action != "NONE" and not args.dry_run:
            ok, out = fc.set_code(target, agcoff_first=True)
            if ok:
                state["recorded"] = target
                state["last_write"] = time.time()
                state["actions"].append(state["last_write"])
                log("ACT %s 写 %02x 成功 [%s]" % (action, target, out), args.log)
            else:
                log("ACT %s 写 %02x 失败 [%s]" % (action, target, out), args.log)
        elif action != "NONE":
            log("DRY %s 目标码 %02x（未写）" % (action, target), args.log)

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
