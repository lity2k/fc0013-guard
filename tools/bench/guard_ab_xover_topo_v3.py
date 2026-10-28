#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard_ab_xover_topo.py — 守卫「用 / 不用」交叉比对，**两种拓扑各跑一轮**。

回答的问题（2026-10-25 用户指定）
--------------------------------
之前两轮分别只在单一拓扑下做过守卫 on/off 交叉（直采 → +51.2%；stream1090 → +292.32%），
两套数字**不可直接比**：线路不需要的数据通路不同、AGC 注入的作用方向也不同。
本实验把「拓扑 × 守卫」四种组合放进同一个 2×2 交叉框架：

    readsb 直采  + 守卫 ON      readsb 直采  + 无守卫（对照）
    stream1090   + 守卫 ON      stream1090   + 无守卫（对照）

设计要点
--------
**每种组合都要与其它组合两两比对**，四种组合 ⇒ C(4,2) = 6 个无序对，每对再做双机交叉
（角色对换）⇒ **12 个阶段**。组合记作 `(拓扑, 守卫)`：

    R-on   readsb 直采 + 守卫        R-off  readsb 直采 + 无守卫
    S-on   stream1090  + 守卫        S-off  stream1090  + 无守卫

    Q1  .84=R-on  .85=R-off      Q7  .84=R-off .85=R-on     ← R-on × R-off（守卫效应·直采）
    Q2  .84=R-off .85=S-off      Q8  .84=S-off .85=R-off    ← 拓扑效应·无守卫
    Q3  .84=R-off .85=S-on       Q9  .84=S-on  .85=R-off
    Q4  .84=R-on  .85=S-off      Q10 .84=S-off .85=R-on
    Q5  .84=R-on  .85=S-on       Q11 .84=S-on  .85=R-on     ← 拓扑效应·有守卫
    Q6  .84=S-off .85=S-on       Q12 .84=S-on  .85=S-off    ← 守卫效应·s1090

  每一对取交叉平均 ⇒ 消掉机器偏置：
      效应 = (Δ_Qi − Δ_Qj) / 2  ，机器偏置 = (Δ_Qi + Δ_Qj) / 2（i,j 为同一对的两个方向）

⚠ **拓扑切换只能在阶段边界做**：改 readsub 配置后必须 `systemctl restart readsub`，
  而 restart 会把 `total` 累计计数器清零 ⇒ 绝不能在阶段中途切，否则 Δ<0、速率全废。
  每个阶段都重新记录自己的 t0，计数器归零不影响首尾差。

每轮（12 阶段）以 reboot 开场：reboot 顺带把 `/tmp`（二进制被清）与 `/etc/default/readsb`
（节点复位的出厂配置）一起重置，保证第 1 阶段的起点就是干净的直采态。

子命令
------
  run      12 阶段交叉比对（reboot 开场 + 部署 + 逐阶段切拓扑/起臂 + 拉数据）
  probe    即时状态查询
  analyze  从 raw CSV 出「6 对 × 交叉平均」对比表

判据（预注册，取数前定死，事后不得改判）
----------------------------------------
  msg/s   = Δtotal.messages / Δt
  valid/s = Δtotal.messages_valid / Δt
  pos/s   = Δtotal.position_count_total / Δt   （DF17 位置报文，最稳健）
  msg/1e6 = Δmessages / Δsamples_processed × 1e6 —— **仅直采可用**
            （net-only 下 readsb 不跑解调，`local` 整个为 None，无 samples_processed）
  c       = 128·10^(noise/20)（守卫标度）；越界占比 = (c<1.3 或 c>20)/N

  阈值：A 段两臂 msg/s 相对差 |Δ| < 5% ⇒ 「守卫零打扰」；
        B 段 ON 臂 c 回域内、OFF 臂越界占比 > 80% ⇒ 「守卫有效」。

两种拓扑的取数差异（错一处即整轮数据报废）
------------------------------------------
| 项 | readsb 直采 | stream1090 + net-only |
|---|---|---|
| 底噪 c | stats.json 的 `last1min.local.noise` | tee_power CSV 的 `mean_dbfs`（30 s 中位数） |
| 采样处理量 | `total.local.samples_processed` 可用 | 无 ⇒ msg/1e6 置 `-` |
| 对照臂数据流 | readsb 自己占 SDR，不跑守卫也有数据 | **必须单独起管道**，否则该臂无数据 |
| peak/strong | `last1min.local` 有 | tee 口径未标定 ⇒ 置空，不参与判据 |

实验纪律（2026-10-24 用户指定）
--------------------------------
  每一轮交叉实验都**以 reboot 开场**：两机重启 → 核验 /tmp 为空、无残留进程、
  readsb 起、时钟同步 → 再部署与取样。
  ⚠ `--from` 只用于同一轮内被中断后的接续，不支持跨轮复用。
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import nb_ssh                                                     # noqa: E402
from nb_ssh import exec_host, resolve                             # noqa: E402

HOSTS = [("84", "192.168.13.84"), ("85", "192.168.13.85")]
GUARD = "/tmp/fc0013_guard.py"
GUARD_LOG = "/tmp/fc_guard.log"
GUARD_LOCK = "/tmp/fc0013_guard.lock"
SAMPLER = "/tmp/xo_sampler.py"
SAMPLER_CSV = "/tmp/xo_%s.csv"               # 一机一个文件，跨 14 阶段持续 append
SAMPLER_LOG = "/tmp/xo_sampler_%s.out"
DF = "/tmp/df_count.py"                      # L3：DF 分布计数器（与拓扑无关）
PIPE = "/tmp/pipe_only.py"                   # OFF 臂专用（s1090 拓扑，复用守卫 S1090Pipe）
TEE = "/tmp/tee_power.csv"                   # net-only 下唯一底噪观测源
TOPOS = ("readsb", "s1090")
TOPO_MODE = {"readsb": "readsb", "s1090": "s1090"}   # 守卫 --mode 与拓扑同名
PIPE_LOG = "/tmp/xo_pipe.log"
NET_ONLY = 'RECEIVER_OPTIONS="--net-only"'
DEFAULT_DIR_CFG = 'RECEIVER_OPTIONS="--device 0 --device-type rtlsdr --gain 19.7 --ppm 0"'
DEFAULT_OUTDIR = os.path.abspath(os.path.join(HERE, ".."))
DEPLOYED = os.path.join(ROOT, "tools", "fc0013_guard", "deployed.json")

# reboot 会清 /tmp ⇒ 每轮都要重推的二进制（本地上游：stream1090 源码目录）
BINARIES = [
    ("knowledge/references/stream1090_src/stream1090-armhf", "/tmp/stream1090", True),
    ("knowledge/references/stream1090_src/tee_power-armhf", "/tmp/tee_power", True),
    (os.path.join(ROOT, "tools", "fc0013_guard", "pipe_only.py"), PIPE, False),
]


# ------------------------------------------------------------ 上传通道
def push_file(host, local, remote, chunk=24000, text=True):
    """分块 base64 上传 + md5 双端校验（两机 SFTP 子系统不可用）。

    ⚠ `text=True` 才会做 CRLF→LF；二进制（stream1090/tee_power）必须 False，
      否则会把 elf 里的 0d0a 字节改坏、上传后跑不起来。
    """
    with open(local, "rb") as f:
        data = f.read()
    if text:
        data = data.replace(b"\r\n", b"\n")
    sh(host, "rm -f %s && touch %s" % (remote, remote), timeout=30)
    for i in range(0, len(data), chunk):
        b64 = base64.b64encode(data[i:i + chunk]).decode()
        sh(host, "echo '%s' | base64 -d >> %s" % (b64, remote), timeout=150)
    want = hashlib.md5(data).hexdigest()
    got = sh(host, "md5sum %s | cut -d' ' -f1" % remote, timeout=30).strip()
    return (want == got), want[:12]


# ------------------------------------------------------------ 节点侧采样器
SAMPLER_SRC = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xo_sampler.py — 节点侧常驻采样，支持两种拓扑（argv[4] = readsb | s1090）。

  拓扑        吞吐                        底噪 c
  ---------   -------------------------   -------------------------------------
  readsb      stats.json total 累加计数器  last1min.local.noise（60 s 滑动窗）
  s1090       同上（net-only 下 remote      tee_power CSV 的 mean_dbfs
              报文仍累加进 total）          —— 直接复用守卫的 read_tee()，不另写
                                           一份口径（避免两套口径混用）

⚠ net-only 下 `total.local` 整个为 None（readsb 不跑 demodulate2400）
  ⇒ samples_processed 不存在，msg/1e6 归一化在该拓扑下天然不可用。

⚠ 采样器口径由 argv[4] 显式给定，**不自动探测**：同一台机的拓扑会随阶段变化，
   让采样器自己猜源等于让"数据口径"由读出时的偶然状态决定。由 harness 在每阶段
   起臂后按当期拓扑重启本进程，CSV 以 append 累积，分析时按 phases 的 ts 切窗。
"""
import json, os, sys, time

sys.path.insert(0, "/tmp")
tag, every, out, topo = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
if topo not in ("readsb", "s1090"):
    # 参序防护：防止把 (tag, every, topo, csv) 传错位而静默写错文件
    sys.stderr.write("FATAL: argv[4]=%r 不是合法拓扑（期望 tag every OUT TOPo）\\n"
                     % topo)
    sys.exit(2)
HDR = ("ts,wall,t_msg,t_valid,t_pos,sp,sd,noise,peak,signal,strong,"
       "acc0,l1_valid,l1_pos,l1_modes,l1_sp\n")

try:
    import fc0013_guard as G
except Exception:
    G = None

with open(out, "a") as f:
    if f.tell() == 0:
        f.write(HDR)      # append 模式：只有首次（文件为空）才写表头

def g(d, k, dflt=""):
    if not isinstance(d, dict):
        return dflt
    v = d.get(k, dflt)
    return dflt if v is None else v

def tee_noise():
    if G is None:
        return ""
    try:
        st = G.read_tee()
        return "%.2f" % st["noise"] if st else ""
    except Exception:
        return ""

while True:
    ts = time.time()
    wall = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    try:
        s = json.load(open("/run/readsb/stats.json"))
        tot, l1 = s.get("total", {}) or {}, s.get("last1min", {}) or {}
        tl, ll = tot.get("local", {}) or {}, l1.get("local", {}) or {}
        if topo == "readsb":
            acc = tl.get("accepted") or [0, 0]
            row = (ts, wall,
                   g(tot, "messages", 0), g(tot, "messages_valid", 0),
                   g(tot, "position_count_total", 0),
                   g(tl, "samples_processed", 0), g(tl, "samples_dropped", 0),
                   # 工况量一律取 last1min（60 s 滑动窗）：
                   # total.local.noise 是全程累加平均，混着启动暂态不代表当前工况
                   g(ll, "noise", ""), g(ll, "peak_signal", ""),
                   g(ll, "signal", ""), g(ll, "strong_signals", 0),
                   acc[0] if isinstance(acc, list) else "",
                   g(l1, "messages_valid", 0), g(l1, "position_count_total", 0),
                   g(ll, "modes", 0), g(ll, "samples_processed", 0))
        else:
            net = tot.get("remote", {}) or {}
            acc = net.get("accepted") or [0, 0]
            row = (ts, wall,
                   g(tot, "messages", 0), g(tot, "messages_valid", 0),
                   g(tot, "position_count_total", 0),
                   0, 0,                     # net-only 无 samples_processed
                   tee_noise(), "", "", "",  # peak/signal/strong：tee 口径未标定
                   acc[0] if isinstance(acc, list) else "",
                   g(l1, "messages_valid", 0), g(l1, "position_count_total", 0),
                   g((l1.get("remote") or {}), "modes", 0), g(l1, "messages", 0))
        with open(out, "a") as f:
            f.write(",".join(str(x) for x in row) + "\n")
    except Exception as e:
        with open(out, "a") as f:
            f.write("%s,%s,ERR:%s\n" % (ts, wall, str(e)[:60]))
    time.sleep(every)
'''


# ------------------------------------------------------------ 基础动作
def sh(host, cmd, timeout=60, tries=2):
    return exec_host(resolve(host), cmd, tries=tries, gap=4, timeout=timeout) or ""


def node_epoch(host):
    out = sh(host, "date +%s.%N", timeout=20, tries=2).strip().splitlines()
    try:
        return float(out[-1])
    except Exception:
        return time.time()


def read_regs(host):
    """只读回读 0d/13/14（走守卫自身通道，不依赖 fc_daemon）。"""
    cmd = ("python3 -c \"import sys;sys.path.insert(0,'/tmp');"
           "import fc0013_guard as G;print('REGS',G.Fc().read_013())\" 2>&1")
    out = sh(host, cmd, timeout=90)
    for line in out.splitlines():
        if line.startswith("REGS"):
            s = line[4:].strip().strip("()[]")
            try:
                v = [int(x) for x in s.replace(" ", "").split(",")]
                if len(v) >= 3 and all(0 <= x <= 255 for x in v):
                    return v[0], v[1], v[2]
            except Exception:
                pass
    return None


def fmt_regs(r):
    return ("%02x/%02x/%02x" % r) if r else "读不到"


def guard_stop(host):
    """停守卫 + 归档其日志。

    ⚠⚠ 自匹配陷阱（2026-10-25 dry-run 实测，代价：遗留守卫活 3 小时、锁被占、
    收尾守卫全部静默退出）：
      `pgrep -f 'fc0013_gua[r]d'` 的方括号只规避**模式字面量**，但同一条命令里
      `rm -f /tmp/fc0013_guard.lock` 的**路径字面**含子串 `fc0013_guard`
      ⇒ 匹配到承载命令的 wrapper 自身 ⇒ `kill -9` 先杀死自己 ⇒ 真守卫没杀、
      rm 也没执行。**kill 与 rm 必须分两次 exec，且 rm 的目标不许出现完整字面**
      （用通配符 `/tmp/fc0013_gua*.lock`：正则 `fc0013_gua[r]d` 不匹配它）。
    """
    sh(host, "for p in $(pgrep -f 'fc0013_gua[r]d'); do kill -9 $p; done; true",
       timeout=30)
    # 归档而非删除：末态/事后分析要用；名字里同样不许出现完整字面
    sh(host, "f=/tmp/fc_guard.log; [ -f $f ] && mv $f /tmp/fc_guard.prev.log; "
             "rm -f /tmp/fc0013_gua*.lock; true", timeout=30)


def pipe_stop(host):
    """停管道四环。

    ⚠ 必须显式停：守卫是 `--mode s1090` 时管道是它的子进程，但 `kill -9` 守卫
      **不会**连带杀掉它们（没有 PDEATHSIG）⇒ 残留的 rtl_sdr 会和新一轮抢同一个
      USB device，两臂互相拖死且日志上看不出原因。
    模式里的方括号是自匹配规避：承载本命令的 shell 命令行里是 `rtl_sd[r]` 字面，
    匹配不到 `rtl_sdr`，不会被自己杀掉。
    """
    sh(host, "for p in $(pgrep -f 'rtl_sd[r] -f'); do kill -9 $p; done; "
             "for p in $(pgrep -f 'tee_powe[r] --out'); do kill -9 $p; done; "
             "for p in $(pgrep -f 'stream109[0] -s'); do kill -9 $p; done; "
             "for p in $(pgrep -f 'pipe_onl[y]'); do kill -9 $p; done; "
             "for p in $(pgrep -f 's1090_pip[e]'); do kill -9 $p; done; "
             "sleep 2; true", timeout=40)


def pipe_start(host):
    """对照臂专用：没有守卫 ⇒ 管道必须单独起，否则该臂没有数据流。

    这一点是本拓扑与直采拓扑最大的设计差别：直采时 OFF 臂只要不跑守卫，
    readsb 自己就把数据流和底噪都测好了；net-only 下 SDR 没有人推流就彻底没数据。

    `pipe_only.py` 复用守卫的 `S1090Pipe`，所以两臂的管道实现完全一致。
    """
    pipe_stop(host)
    # ⚠ 同 guard_start：本命令字面含 `/tmp/pipe_only.py` ⇒ pgrep 会命中 wrapper
    # 自身，`pgrep -c` 把 wrapper 算进去 ⇒ 真实管道没起来也显示 pipe=1。
    # 必须 `grep -vw $$` 排除当前 shell。
    cmd = ("(setsid python3 %s </dev/null >>/tmp/xo_pipe_%s.out 2>&1 &) ; "
           "sleep 8; echo -n rtl=; pgrep -c -x rtl_sdr; echo -n ' s1090='; "
           "pgrep -c -x stream1090; echo -n ' pipe='; "
           "pgrep -f 'pipe_onl[y]' | grep -vw $$ | wc -l"
           % (PIPE, host.lstrip(".")))
    return " ".join(sh(host, cmd, timeout=70, tries=1).split())


def guard_start(host, code, interval=15.0, mode=TOPO_MODE["readsb"]):
    """显式起点码启动（--cal CODE）。s1090 模式下守卫会自带整条管道。

    ⚠ 返回值只用于打印，**不允许**依赖它判断成功：守卫要先过前置检查、还会去
    拿 I2C 事务读起点码，偶尔慢于等待窗口 ⇒ 此时用 `pgrep` 直接问进程，
    读不到就返回诊断串。上一版依赖 `cat <lock>`，空输出会让 IndexError 把整轮
    12 阶段实验打断，代价远大于"少打一个 pid"。
    """
    guard_stop(host)
    pipe_stop(host)
    # ⚠ pgrep 必须排除 wrapper 自身：本命令的字面里含 `/tmp/fc0013_guard.py`
    # （setsid 启动参数），模式 `fc0013_gua[r]d` 会匹配到这个子串 ⇒ 拿到的是
    # wrapper 的 PID 而不是守卫的。`grep -vw $$` 把当前 shell 自己剔掉。
    cmd = ("(setsid python3 %s --node %s --mode %s --cal %s --interval %s "
           "--log %s </dev/null >>/tmp/xo_guard_%s.out 2>&1 &) ; sleep 10; "
           "p=$(pgrep -f 'fc0013_gua[r]d' | grep -vw $$ | head -1); "
           "if [ -n \"$p\" ]; then echo pid=$p; "
           "else echo \"NOPID|$(tail -1 /tmp/xo_guard_%s.out 2>/dev/null)\"; fi"
           % (GUARD, host.lstrip("."), mode, code, interval, GUARD_LOG,
              host.lstrip("."), host.lstrip(".")))
    out = sh(host, cmd, timeout=60, tries=1)
    lines = out.strip().splitlines()
    return lines[-1].strip() if lines else "NOPID|(exec 无输出)"


def guard_start_raw(host, interval=15.0):
    """新用户路径：不带 --cal，起点码由脚本自解析（读回 0x13 > DEFAULT_CODE）。"""
    guard_stop(host)
    cmd = ("(setsid python3 %s --node %s --apply --interval %s --log %s "
           "</dev/null >>/tmp/xo_guard_%s.out 2>&1 &) ; sleep 6; "
           "p=$(pgrep -f 'fc0013_gua[r]d' | grep -vw $$ | head -1); "
           "if [ -n \"$p\" ]; then echo pid=$p; else echo NOPID; fi"
           % (GUARD, host.lstrip("."), interval, GUARD_LOG,
              host.lstrip(".")))
    out = sh(host, cmd, timeout=45, tries=1)
    lines = out.strip().splitlines()
    return lines[-1].strip() if lines else "NOPID|(exec 无输出)"


def arm(host, code, tries=2):
    """一次性对齐起点：守卫 --pin = 关 AGC(0x0d=09) + 钉 0x13=code + 回读校验。

    设备刚被上一次运行打开又关闭时，libusb 偶发 `device open failed` ⇒ 隔 3 s 重试。
    """
    out = ""
    for _ in range(tries):
        out = sh(host, "python3 %s --node %s --pin %s --log /dev/null 2>&1 | tail -2"
                 % (GUARD, host.lstrip("."), code), timeout=90)
        if "OK" in out:
            break
        time.sleep(3)
    return out.strip().replace("\n", " | ")


def inject_agc(host):
    """注入扰动：打开芯片 AGC（0x0d bit3 清零 → 0x01）。走守卫自身 I2C 通道。

    ⚠ `write_verified(h, reg, val)` 的第一个参数是设备句柄，必须先从
      `txn()` 上下文取出；漏传会 TypeError（且不会发生任何写入）。
    """
    cmd = ("python3 -c \"import sys;sys.path.insert(0,'/tmp');"
           "import fc0013_guard as G;fc=G.Fc();cm=fc.txn();h=cm.__enter__();"
           "r=fc.write_verified(h,0x0d,0x01) if h else None;"
           "cm.__exit__(None,None,None);print('AGCWR',r)\" 2>&1")
    out = sh(host, cmd, timeout=90)
    return out.strip().replace("\n", " | ")[:140]


def start_sampler(host, every=20.0, topo=TOPOS[0]):
    sh(host, "for p in $(pgrep -f 'xo_sampl[e]r'); do kill -9 $p; done; true",
       timeout=30)
    # ⚠ 同 guard_start：启动命令字面含 `/tmp/xo_sampler.py` ⇒ pgrep 会命中
    # wrapper 自身，必须 `grep -vw $$` 排除，否则打印的 pid 是 wrapper 的。
    # ⚠⚠ argv 顺序必须与 xo_sampler.py 的解析一致：`tag every OUT TOPO`
    #   （OUT 在第 3 位）。若传成 (tag, every, topo, csv) ⇒ out 会拿到 "readsb"
    #   撞 cwd 同名目录直接崩；另一台 cwd 无该目录 ⇒ 静默写进错文件、
    #   topo 永远拿到 CSV 路径 ⇒ 口径恒错且 CSV 从未生成。**这台"看似成功"**
    #   的失败模式比崩溃更危险：pid 有、进程在、就是没有一行有效数据。
    cmd = ("(setsid python3 %s %s %s %s %s </dev/null >>%s 2>&1 &) ; sleep 2; "
           "pgrep -f 'xo_sampl[e]r' | grep -vw $$ | head -2"
           % (SAMPLER, host.lstrip("."), every,
              SAMPLER_CSV % host.lstrip("."), topo,
              SAMPLER_LOG % host.lstrip(".")))
    out = sh(host, cmd, timeout=45, tries=1)
    return out.strip().split()


def stop_sampler(host):
    sh(host, "for p in $(pgrep -f 'xo_sampl[e]r'); do kill -9 $p; done; true",
       timeout=30)


def deployed_code(host, fallback=None):
    try:
        with open(DEPLOYED, encoding="utf-8") as f:
            d = json.load(f).get("hosts", {})
    except Exception:
        d = {}
    for k in (host, "." + host.lstrip(".")):
        v = d.get(k)
        if isinstance(v, dict) and v.get("code"):
            return v["code"]
    return fallback


def node_offset(host, n=7):
    """时钟偏差（远端 − 本机，秒），round-trip 中点法，丢弃往返最慢的 1/3。"""
    res = []
    for _ in range(n):
        t1 = time.time()
        out = sh(host, "date +%s.%N", timeout=20, tries=1)
        t2 = time.time()
        try:
            rt = float(out.strip().splitlines()[-1])
        except Exception:
            continue
        res.append((rt - (t1 + t2) / 2.0, t2 - t1))
    if not res:
        return float("nan")
    res.sort(key=lambda x: x[1])
    keep = res[:max(3, len(res) - len(res) // 3)]
    return sum(x[0] for x in keep) / len(keep)


def deploy(host):
    """部署：守卫 + 采样器 + 本实验必需的二进制（reboot 会清 /tmp，每次都要重推）。"""
    gsrc = os.path.join(ROOT, "tools", "fc0013_guard", "fc0013_guard.py")
    ok, m = push_file(host, gsrc, GUARD)
    print("    守卫脚本: %s (md5 %s)" % ("OK" if ok else "FAIL", m))
    tmp = os.path.join(HERE, "_sampler_upload.py")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(SAMPLER_SRC.replace("\r\n", "\n"))
    ok2, m2 = push_file(host, tmp, SAMPLER)
    os.remove(tmp)
    sh(host, "chmod +x %s" % SAMPLER, timeout=20)
    print("    采样器:   %s (md5 %s)" % ("OK" if ok2 else "FAIL", m2))
    okd, md = push_file(host, os.path.join(HERE, "df_count.py"), DF)
    print("    DF 计数:  %s (md5 %s)" % ("OK" if okd else "FAIL", md))
    ok3 = True
    for local, remote, is_bin in BINARIES:
        okb, mb = push_file(host, os.path.join(ROOT, local), remote,
                            text=not is_bin)
        if is_bin:
            sh(host, "chmod +x %s" % remote, timeout=20)
        print("    %-12s %s (md5 %s)" % (os.path.basename(remote),
                                         "OK" if okb else "FAIL", mb))
        ok3 = ok3 and okb
    return ok and ok2 and ok3


def read_conf(host):
    """读出 RECEIVER_OPTIONS 行。

    reboot 后节点给出的是**出厂直采配置** ⇒ 在切任何拓扑之前先把它存下来，
    之后切回直采时按原样写回，不猜参数（`--gain 19.7` 之类不该由 harness 假定）。
    """
    out = sh(host, "grep -h '^RECEIVER_OPTIONS' /etc/default/readsb", timeout=30)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("RECEIVER_OPTIONS="):
            return line
    return ""


def set_topology(host, topo, conf_direct):
    """把 readsb 切成「直采独占 SDR」或「--net-only 纯解码器」。

    ⚠ 只在**阶段边界**调用：`systemctl restart readsb` 会把 `total` 累计计数器清零，
      阶段中途切会让该阶段的 Δ 变负、速率全废。
    """
    if topo == "s1090":
        sh(host, "cp -n /etc/default/readsb /tmp/readsb.default.bak 2>/dev/null; "
                 "sed -i 's|^RECEIVER_OPTIONS=.*|%s|' /etc/default/readsb"
                 % NET_ONLY, timeout=60)
    else:
        sh(host, "sed -i 's|^RECEIVER_OPTIONS=.*|%s|' /etc/default/readsb"
                 % (conf_direct or DEFAULT_DIR_CFG), timeout=60)
    sh(host, "systemctl restart readsb", timeout=90)
    time.sleep(10)
    out = sh(host, "grep -h '^RECEIVER_OPTIONS' /etc/default/readsb; "
                   "echo -n 'net_only_procs='; "
                   "pgrep -f 'readsb .*--net-o[n]ly' | wc -l; "
                   "echo -n ' active='; systemctl is-active readsb",
             timeout=60)
    return " | ".join(x.strip() for x in out.strip().splitlines() if x.strip())


def apply_phase(host, topo, role, code, interval, conf_direct, prev_topo):
    """装配一个阶段所需的一台机组态。返回状态串。

    顺序是硬约束：停干净 → （拓扑变了才）改配置重启 readsb → 起数据流 → **最后** pin。
    原因：readsb 启动 ~3 s 会由 `set_tuner_gain` 把 0x13 抹回 0x0a 附近；
    `rtl_sdr` 启动同样抹一次 ⇒ **先 pin 等于白做**。
    """
    st = []
    guard_stop(host)
    pipe_stop(host)          # kill -9 守卫不会连带杀掉它的管道子进程
    if topo != prev_topo:
        st.append("拓扑->%s [%s]" % (topo, set_topology(host, topo, conf_direct)))
    if role == "on":
        st.append("守卫 pid=%s" % guard_start(host, code, interval,
                                              mode=TOPO_MODE[topo]))
    elif topo == "s1090":
        # 对照臂没有守卫 ⇒ net-only 下 SDR 无人推流 ⇒ 必须单独起管道，
        # 否则该臂彻底没有数据（比的是"活机 vs 死机"）
        st.append("独立管道 %s" % pipe_start(host))
    else:
        st.append("直采对照（readsb 独占 SDR）")
    time.sleep(6)
    st.append("arm %s" % arm(host, code))
    return " ; ".join(st)


# ------------------------------------------------------------ bootstrap
def cmd_bootstrap(args):
    """全新硬件：机上无 fc_daemon、无钉码、无标定数据，只投一个脚本。"""
    outdir = args.outdir
    raw = os.path.join(outdir, "raw")
    for p in (outdir, raw):
        if not os.path.isdir(p):
            os.makedirs(p)
    flog = open(os.path.join(raw, "bootstrap.jsonl"), "a")

    def rec(stage, host, **kw):
        kw["stage"] = stage
        kw["host"] = host
        kw["ts"] = time.time()
        flog.write(json.dumps(kw, ensure_ascii=False) + "\n")
        flog.flush()

    # --- 阶段 1：环境预检 + 部署（串行，无时序要求）---
    print("== 阶段 1：环境预检 + 部署 ==")
    for tag, ip in HOSTS:
        print("\n-- .%s (%s)" % (tag, ip))
        env = sh(ip, "python3 -V 2>&1; "
                     "ls -l /lib/arm-linux-gnueabihf/libusb-1.0.so.0 2>&1 | head -1; "
                     "lsusb -d 0bda:2838 2>&1 | head -1; "
                     "echo -n 'readsb='; systemctl is-active readsb; "
                     "echo -n 'oldguard:'; pgrep -f 'fc0013_gua[r]d' | tr '\\n' ' '",
                 timeout=60)
        env = " | ".join(x.strip() for x in env.strip().splitlines() if x.strip())
        print("  环境:", env)
        rec("env", tag, detail=env)
        deploy(ip)

    # --- 阶段 2：通道自检（真写 0x13，跑完自动钉回起点）---
    print("\n== 阶段 2：通道自检 --selftest %d ==" % args.selftest)
    for tag, ip in HOSTS:
        print("\n-- .%s" % tag)
        st = sh(ip, "python3 %s --node %s --selftest %d 2>&1 | tail -6"
                % (GUARD, tag, args.selftest), timeout=1200)
        for line in st.strip().splitlines():
            print("   ", line)
        rec("selftest", tag, out=st.strip()[-400:])

    # --- 阶段 3：裸机态（未钉码）---
    print("\n== 阶段 3：裸机态（readsb 自管，硬件 AGC 默认开）==")
    for tag, ip in HOSTS:
        r = read_regs(ip)
        s = sample_one(ip)
        print("  .%s 0d/13/14=%s  noise=%s  c=%s  msg=%s"
              % (tag, fmt_regs(r), s["noise"] if s else "?",
                 round(c_of(s["noise"]), 2) if s else "?",
                 s["msg"] if s else "?"))
        rec("bare", tag, regs=r, **(s or {}))

    # --- 阶段 4：两机几乎同时启动采样器 + 守卫（不给 --cal）---
    print("\n== 阶段 4：启动采样器 + 守卫（新用户路径，不传 --cal）==")
    for tag, ip in HOSTS:
        start_sampler(ip, args.every)
    started = {}
    for tag, ip in HOSTS:
        started[tag] = node_epoch(ip)
        pid = guard_start_raw(ip, args.interval)
        print("  .%s 守卫 pid=%s" % (tag, pid))
        rec("guard_start", tag, pid=pid, t0=started[tag])

    # --- 阶段 5：并行观察收敛 ---
    print("\n== 阶段 5：并行观察 %.0f min ==" % args.watch)
    flog.write(json.dumps({"stage": "phases_boot",
                           "t0": started, "watch_min": args.watch}) + "\n")
    flog.flush()
    t_end = time.time() + args.watch * 60.0
    while time.time() < t_end:
        time.sleep(args.every)
        for tag, ip in HOSTS:
            s = sample_one(ip)
            if not s:
                print("    .%s 采样失败" % tag)
                continue
            print("    .%s t+%3.0fs  c=%6.2f  noise=%6.1f  peak=%6.1f  "
                  "strong=%-4s"
                  % (tag, time.time() - started[tag], c_of(s["noise"]),
                     s["noise"], s["peak"], s["strong"]), flush=True)
            rec("watch", tag, **s)
    flog.close()
    return 0


def sample_one(host):
    """采一次（probe 用）：底噪按当前拓扑自动选源。

    `last1min.local` 非空 ⇒ 直采（取 local.noise）；为空 ⇒ net-only（取 tee mean_dbfs）。
    这里**允许**自动探测，因为它只是一次性诊断读数、不进正式数据；
    正式数据的口径由采样器的 argv[4] 显式固定。
    """
    cmd = ("python3 -c \"import json,sys;sys.path.insert(0,'/tmp');"
           "import fc0013_guard as G;"
           "s=json.load(open('/run/readsb/stats.json'));t=s['total'];"
           "ll=(s.get('last1min') or {}).get('local') or {};"
           "r=None if ll.get('noise') is not None else G.read_tee();"
           "n=(ll.get('noise') if ll.get('noise') is not None "
           "else (r['noise'] if r else 0.0));"
           "tl=t.get('local') or {};"
           "sp=int(tl.get('samples_processed',0)) if tl else 0;"
           "print('S',round(n,2),0.0,0,int(t['messages']),"
           "int(t['messages_valid']),int(t['position_count_total']),sp)\" 2>&1")
    out = sh(host, cmd, timeout=60)
    for line in out.splitlines():
        if line.startswith("S "):
            p = line.split()
            try:
                return {"noise": float(p[1]), "peak": float(p[2]),
                        "strong": int(p[3]), "msg": int(p[4]),
                        "valid": int(p[5]), "pos": int(p[6]),
                        "sp": int(p[7])}
            except Exception:
                return None
    return None


def c_of(noise):
    """守卫标度：8bit 满量程码值 = 128 × 10^(noise_dBFS/20)。

    ⚠ 只接受 last1min.local.noise（60s 滑动窗）。total.local.noise 是
    全程累加平均，混有启动暂态，不代表当前工况。
    """
    try:
        return 128.0 * 10.0 ** (float(noise) / 20.0)
    except Exception:
        return None


# ------------------------------------------------------------ precheck
def cmd_precheck(args):
    print("== 预检 ==")
    ok = True
    for tag, ip in HOSTS:
        print("\n-- .%s (%s)" % (tag, ip))
        out = sh(ip, "systemctl is-active readsb; "
                     "uptime -s; date -u '+%F %T'; "
                     "stat -c %y /run/readsb/stats.json 2>&1; "
                     "ls -l /tmp/fc_daemon /tmp/fc0013_guard.py 2>&1 | tail -3",
                 timeout=60)
        print(out.strip() or "(无输出)")
        r = read_regs(ip)
        print("  寄存器 0d/13/14 =", fmt_regs(r))
        if r is None:
            ok = False
    print("\n-- 时钟偏差（远端 − 本机，秒；正=远端快）--")
    for tag, ip in HOSTS:
        print("  .%s offset = %+.3f s" % (tag, node_offset(ip)))
    return 0 if ok else 1


# ------------------------------------------------------------ probe
def cmd_probe(args):
    print("== 即时状态 ==")
    for tag, ip in HOSTS:
        r = read_regs(ip)
        g = sh(ip, "pgrep -f 'fc0013_gua[r]d' | tr '\\n' ' '", timeout=20).strip()
        s = sample_one(ip)
        if s:
            print("  .%s 0d/13/14=%s guard=[%s] c=%.2f noise=%.1f peak=%.1f "
                  "strong=%s msg=%s valid=%s pos=%s"
                  % (tag, fmt_regs(r), g, c_of(s["noise"]), s["noise"],
                     s["peak"], s["strong"], s["msg"], s["valid"], s["pos"]))
        else:
            print("  .%s 0d/13/14=%s guard=[%s] 采样失败" % (tag, fmt_regs(r), g))
    return 0


# ------------------------------------------------------------ 归零（每轮必修）
def wait_node_ready(host, budget=300.0):
    """等节点重启完成：/proc/uptime 可读且小于单次预算（证明是本次新启动）。"""
    t0 = time.time()
    while time.time() - t0 < budget:
        out = sh(host, "cat /proc/uptime", timeout=15, tries=1).strip()
        try:
            up = float(out.split()[0])
            if up < budget:
                return True, up
        except (ValueError, IndexError):
            pass
        time.sleep(8)
    return False, None


def reboot_pair():
    """实验纪律：每一轮交叉实验都以 reboot 开场，保证环境清洁。

    流程：下发 reboot → 等两机回线（uptime 归零）→ 核验 /tmp 为空、
    无守卫/采样器残留、readsb 已起 → 等时钟同步 → 返回。
    """
    print("\n== 归零：reboot 两机（每轮必修，保证干净起点）==")
    for tag, ip in HOSTS:
        sh(ip, "(sleep 2; sync; reboot) >/dev/null 2>&1 &", timeout=20, tries=1)
        print("  .%s reboot 已下发" % tag)
    time.sleep(30)
    for tag, ip in HOSTS:
        t0 = time.time()
        ok, up = wait_node_ready(ip)
        if not ok:
            raise SystemExit("✗ .%s 重启后 %.0fs 内未回线，实验中止" % (tag, time.time() - t0))
        print("  .%s 回线 uptime=%.0fs（等 %.0fs）" % (tag, up, time.time() - t0))

    print("\n== 清洁性核验 ==")
    for tag, ip in HOSTS:
        out = sh(ip,
                 "echo \"tmp=[$(ls -A /tmp | tr '\\n' ' ')]\"; "
                 "echo \"guard=$(pgrep -c -f 'fc0013_gua[r]d' 2>/dev/null || true) "
                 "sampler=$(pgrep -c -f 'xo_sampl[e]r' 2>/dev/null || true)\"; "
                 "echo \"readsb=$(systemctl is-active readsb)\"; "
                 "echo \"ntp=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)\"",
                 timeout=60, tries=2)
        for line in out.strip().splitlines():
            print("  .%s %s" % (tag, line.strip()))

    for tag, ip in HOSTS:
        t0 = time.time()
        synced = False
        while time.time() - t0 < 240:
            v = sh(ip, "timedatectl show -p NTPSynchronized --value 2>/dev/null",
                   timeout=20, tries=1).strip()
            if v == "yes":
                synced = True
                break
            time.sleep(10)
        if synced:
            print("  .%s 时钟已同步（%.0fs）" % (tag, time.time() - t0))
        else:
            print("  .%s ⚠ 时钟未报告同步（继续；相位窗口按节点时钟对齐）" % tag)


# ------------------------------------------------------------ run
# 四种组合 = (拓扑, 守卫)；每种组合与其它三种两两比对 ⇒ C(4,2)=6 对，
# 每对再双机交叉（角色对换）⇒ 12 个阶段。
COMBO = {("readsb", "on"): "R-on", ("readsb", "off"): "R-off",
         ("s1090", "on"): "S-on", ("s1090", "off"): "S-off"}

# (阶段名, (.84 组合), (.85 组合))
# ⚠ v3 设计要点：**每一对的两个方向背靠背**（相邻阶段，空情差通常 <2 msg/s）。
#   v1 把"6 个正向在前、6 个反向在后"，两方向相隔约 1 h，空情差 3.9–24 msg/s，
#   而比值随空情斜率高达 +0.49~+0.87 pp/(msg/s) ⇒ 系统偏置 2–21 pp，与效应同量级
#   （见 REVIEW.md P0-2）。背靠背把这段混杂压到 <2 msg/s ⇒ 偏置 <2 pp 量级。
#   末尾 Z1/Z2 是 P1 的**原样重复**，作空白对照：Z 与 P1 之差 = 漂移 + 重复性。
PHASES = [
    ("P1a", ("readsb", "on"),  ("readsb", "off")),
    ("P1b", ("readsb", "off"), ("readsb", "on")),
    ("P2a", ("readsb", "off"), ("s1090",  "off")),
    ("P2b", ("s1090",  "off"), ("readsb", "off")),
    ("P3a", ("readsb", "off"), ("s1090",  "on")),
    ("P3b", ("s1090",  "on"),  ("readsb", "off")),
    ("P4a", ("readsb", "on"),  ("s1090",  "off")),
    ("P4b", ("s1090",  "off"), ("readsb", "on")),
    ("P5a", ("readsb", "on"),  ("s1090",  "on")),
    ("P5b", ("s1090",  "on"),  ("readsb", "on")),
    ("P6a", ("s1090",  "off"), ("s1090",  "on")),
    ("P6b", ("s1090",  "on"),  ("s1090",  "off")),
    ("Z1",  ("readsb", "on"),  ("readsb", "off")),   # 空白对照 = P1a 重复
    ("Z2",  ("readsb", "off"), ("readsb", "on")),    # 空白对照 = P1b 重复
]

# 6 个无序对：(左组合, 右组合, 正向阶段, 反向阶段)
# 「正向」= 左组合在 .84；「反向」= 左组合在 .85。
#   效应    c = (d_fwd + d_rev) / 2      ← 机器偏置对称抵消
#   机器偏置 b = (d_fwd − d_rev) / 2
PAIRS = [
    ("R-on",  "R-off", "P1a", "P1b"),
    ("R-off", "S-off", "P2a", "P2b"),
    ("R-off", "S-on",  "P3a", "P3b"),
    ("R-on",  "S-off", "P4a", "P4b"),
    ("R-on",  "S-on",  "P5a", "P5b"),
    ("S-off", "S-on",  "P6a", "P6b"),
]

# 空白对照：与 P1 同一对组合，末尾原样重跑
BLANK = ("R-on", "R-off", "P1a", "P1b", "Z1", "Z2")

# 冗余闭合关系（4 个单元格只有 3 个自由度，6 对差里必有 3 条恒等式）
# 以 R-off 为基准：R-on=P1, S-off=−P2, S-on=−P3
#   (a) P4 应 = P1 + P2        （R-on − S-off 走两条路）
#   (b) P5 应 = P1 + P3        （R-on − S-on）
#   (c) P6 应 = P3 − P2        （S-off − S-on）
CLOSURE = [
    ("P4 = P1 + P2", "P4", ("P1", "P2"), 1.0),
    ("P5 = P1 + P3", "P5", ("P1", "P3"), 1.0),
    ("P6 = P3 − P2", "P6", ("P3", "P2"), 1.0),
]
def cmd_run(args):
    outdir = args.outdir
    for p in (outdir, os.path.join(outdir, "raw")):
        if not os.path.isdir(p):
            os.makedirs(p)
    pfile = open(os.path.join(outdir, "raw", "phases.jsonl"), "a")
    secs = args.min_ * 60.0

    codes = {}
    for tag, ip in HOSTS:
        c = deployed_code(tag, args.code)
        if c is None:
            print("✗ .%s 无标定码（deployed.json 缺失且未给 --code）" % tag)
            return 2
        codes[tag] = c
    print("起点码：", codes)

    phases = PHASES
    if args.from_phase:
        names = [p[0] for p in PHASES]
        if args.from_phase not in names:
            print("✗ --from 必须是 %s 之一" % "/".join(names))
            return 2
        phases = [p for p in PHASES
                  if names.index(p[0]) >= names.index(args.from_phase)]
        print("续跑：从阶段 %s 开始（跳过 reboot 与部署）" % args.from_phase)

    if not args.from_phase:
        reboot_pair()          # 每轮必修：干净起点
        print("\n== 部署就位（脚本 + 二进制；reboot 已清 /tmp）==")
        for tag, ip in HOSTS:
            print("  .%s" % tag)
            deploy(ip)

    # reboot 后节点给出的是出厂直采配置 ⇒ 切任何拓扑之前先留存，切回时按原样写回
    conf_direct = {}
    for tag, ip in HOSTS:
        conf_direct[tag] = read_conf(ip)
        print("  .%s 出厂 RECEIVER_OPTIONS: %s" % (tag, conf_direct[tag] or "(读不到)"))

    prev = {tag: None for tag, _ in HOSTS}
    for name, c84, c85 in phases:
        plan = {"84": c84, "85": c85}
        print("\n== 阶段 %s (%.0f min) .84=%s .85=%s =="
              % (name, secs / 60.0, COMBO[c84], COMBO[c85]))
        for tag, ip in HOSTS:
            topo, role = plan[tag]
            print("  .%s [%s] %s" % (tag, COMBO[(topo, role)],
                                     apply_phase(ip, topo, role, codes[tag],
                                                 args.interval,
                                                 conf_direct[tag], prev[tag])))
            prev[tag] = topo
            # ⚠ 传 tag 而不是 ip：`host.lstrip(".")` 只脱标签的前导点，对 IP 无效
            #   ⇒ 传 ip 会让 CSV 变成 /tmp/xo_192.168.13.85.csv（功能自洽但难读）
            sp = start_sampler(tag, args.every, topo)   # 口径随拓扑切换
            print("  .%s 采样器 pid=%s" % (tag, " ".join(sp) or "(无!)"))
        time.sleep(4)
        t0 = {}
        for tag, ip in HOSTS:
            t0[tag] = node_epoch(ip)
            print("  .%s 起点 0d/13/14=%s" % (tag, fmt_regs(read_regs(ip))))
        t_end = time.time() + secs
        nxt = time.time() + 120.0
        df_launched = False
        df_at = t0["84"] + args.df_at
        while time.time() < t_end:
            time.sleep(10)
            if not df_launched and time.time() >= df_at:
                df_launched = True
                for tag, ip in HOSTS:
                    sh(ip, "(setsid python3 %s %d > /tmp/df_%s.txt 2>&1 &) ; true"
                          % (DF, int(args.df_sec), name), timeout=30, tries=1)
                print("    DF 计数启动（%d s）" % int(args.df_sec), flush=True)
            if time.time() >= nxt:
                nxt = time.time() + 120.0
                print("    …剩余 %.0fs" % max(t_end - time.time(), 0), flush=True)
        # 末态只**读文件**取守卫日志，不回读寄存器
        # （观测进程每轮读 I2C 会与守卫抢同一把 flock，互相把读回打错位）
        for tag, ip in HOSTS:
            tail = sh(ip, "tail -3 /tmp/fc_guard.log 2>/dev/null", timeout=30)
            print("  .%s 末态守卫日志：%s"
                  % (tag, (tail.strip().splitlines() or ["(空)"])[-1]))
            # 整段日志拉回本地：guard_stop 已在上一阶段末把它归档/清空，
            # 故本文件只含当前阶段 ⇒ 按阶段命名即为纯净切片
            full = sh(ip, "cat /tmp/fc_guard.log 2>/dev/null", timeout=60)
            if full.strip():
                gp = os.path.join(outdir, "raw", "guardlogs")
                if not os.path.isdir(gp):
                    os.makedirs(gp)
                with open(os.path.join(gp, "%s_%s.log" % (name, tag)),
                          "w", encoding="utf-8") as f:
                    f.write(full)
            # L3：DF 分布（解释 msg/s 与 pos/s 是否一致的必要旁证）
            dfout = sh(ip, "cat /tmp/df_%s.txt 2>/dev/null" % name, timeout=30)
            if dfout.strip():
                dp = os.path.join(outdir, "raw", "df")
                if not os.path.isdir(dp):
                    os.makedirs(dp)
                with open(os.path.join(dp, "%s_%s.txt" % (name, tag)),
                          "w", encoding="utf-8") as f:
                    f.write(dfout)
                summ = [l for l in dfout.splitlines() if l.startswith("SUMMARY")]
                if summ:
                    print("  .%s DF: %s" % (tag, summ[-1]))
            pfile.write(json.dumps({
                "phase": name, "host": tag, "topo": plan[tag][0],
                "role": plan[tag][1], "combo": COMBO[plan[tag]],
                "t0": t0[tag], "t1": node_epoch(ip), "code": codes[tag]}) + "\n")
        pfile.flush()
    pfile.close()

    print("\n== 收尾：两机恢复生产态（直采 + 守卫 ON + 钉码）==")
    for tag, ip in HOSTS:
        stop_sampler(ip)
        guard_stop(ip)
        pipe_stop(ip)
        set_topology(ip, "readsb", conf_direct[tag])
        print("  .%s 守卫 pid=%s"
              % (tag, guard_start(ip, codes[tag], args.interval,
                                  mode=TOPO_MODE["readsb"])))
        arm(ip, codes[tag])

    print("\n== 拉取采样数据 ==")
    for tag, ip in HOSTS:
        data = nb_ssh.get_file(ip, SAMPLER_CSV % tag,
                               tries=3, gap=5, timeout=60)
        if data:
            p = os.path.join(outdir, "raw", "xo_%s.csv" % tag)
            with open(p, "wb") as f:
                f.write(data)
            print("  .%s -> %s (%d bytes)" % (tag, p, len(data)))
        else:
            # 诊断：节点上到底有没有文件、叫什么名（避免"静默丢一臂数据"）
            diag = sh(ip, "ls -l /tmp/xo_*.csv 2>&1 | head -5", timeout=30)
            print("  .%s 拉取失败；节点侧：%s"
                  % (tag, " | ".join(x.strip() for x in
                                     diag.strip().splitlines()) or "(无)"))
    print("\nrun done ->", outdir)
    return 0


# ------------------------------------------------------ 分析辅助（v3 沿用）
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


def rate(win, key):
    a, b = num(win[0].get(key)), num(win[-1].get(key))
    dt = num(win[-1]["ts"]) - num(win[0]["ts"])
    if a is None or b is None or not dt:
        return None
    return (b - a) / dt


# ------------------------------------------------------------ analyze
# v3 分析：两机**共用窗** + 亚窗重复 + 冗余闭合检验 + 空白对照。
# 与 v1 的三处差别（每处都对应 REVIEW.md 里的一条缺陷）：
#   1. 共用窗 [max(t0)+skip, min(t1)]：v1 让两机各用自己的 t0/t1，相差约 20 s，
#      改动 0.3–0.9 pp（见 REVIEW P1-5）。
#   2. 亚窗（默认 120 s）给出**阶段内重复** ⇒ 可以算标准误，v1 每臂只有一个点。
#   3. 取数处加 `host_of()` 断言：左组合必须落在本方向声称的主机上。
#      v1 的符号 bug 正是没有这条断言才跑了几小时没被发现（REVIEW P0-1）。
CHUNK_S = 120.0
C_LO, C_HI = 1.3, 20.0


def _rows_in(rows, lo, hi):
    out = []
    for r in rows:
        t = num(r.get("ts"))
        if t is None or t < lo or t > hi:
            continue
        if str(r.get("t_msg", "")).startswith("ERR"):
            continue
        out.append(r)
    return out


def cmd_analyze(args):
    outdir = args.outdir
    raw = os.path.join(outdir, "raw")
    phases = []
    with open(os.path.join(raw, "phases.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                phases.append(json.loads(line))
    csvs = {}
    for tag, _ in HOSTS:
        p = os.path.join(raw, "xo_%s.csv" % tag)
        if os.path.exists(p):
            csvs[tag] = load_csv(p)
    if not csvs:
        print("✗ raw/xo_*.csv 不存在")
        return 2

    ph = {}
    for r in phases:
        ph[(r["phase"], r["host"])] = r
    combos = {n: (a, b) for n, a, b in PHASES}

    # ---- 阶段级：两机共用窗 ----
    stat = {}
    for name, _, _ in PHASES:
        a, b = ph.get((name, "84")), ph.get((name, "85"))
        if not a or not b:
            continue
        lo = max(a["t0"], b["t0"]) + args.skip
        hi = min(a["t1"], b["t1"])
        if hi - lo < 60:
            continue
        for tag in ("84", "85"):
            w = _rows_in(csvs.get(tag, []), lo, hi)
            if len(w) < 3:
                continue
            cs = [x for x in (c_of(r["noise"]) for r in w) if x is not None]
            if not cs:
                continue
            stat[(name, tag)] = {
                "n": len(w), "lo": lo, "hi": hi, "dur": hi - lo,
                "msg": rate(w, "t_msg"), "valid": rate(w, "t_valid"),
                "pos": rate(w, "t_pos"),
                "c_mean": sum(cs) / len(cs), "c_max": max(cs),
                "oob": sum(1 for x in cs if x < C_LO or x > C_HI) / len(cs) * 100.0,
            }

    def host_of(name, label):
        """返回携带组合 label 的主机号；组合不符直接断言失败（防符号 bug 复发）。"""
        c84, c85 = combos[name]
        if COMBO[c84] == label:
            return "84"
        if COMBO[c85] == label:
            return "85"
        raise AssertionError("阶段 %s 不含组合 %s（实际 %s/%s）"
                             % (name, label, COMBO[c84], COMBO[c85]))

    def dir_effect(name, left, right, key):
        i, j = host_of(name, left), host_of(name, right)
        if i == j:
            raise AssertionError("阶段 %s 左右组合相同" % name)
        a, b = stat.get((name, i)), stat.get((name, j))
        if not a or not b or not a.get(key) or not b.get(key):
            return None
        return (a[key] - b[key]) / b[key] * 100.0

    def chunk_rates(name, tag):
        s = stat.get((name, tag))
        if not s:
            return []
        out, t = [], s["lo"]
        while t + CHUNK_S <= s["hi"] + 1e-6:
            w = _rows_in(csvs[tag], t, t + CHUNK_S)
            if len(w) >= 2:
                out.append({"msg": rate(w, "t_msg"),
                            "valid": rate(w, "t_valid"),
                            "pos": rate(w, "t_pos")})
            t += CHUNK_S
        return out

    def chunk_effects(name, left, right, key):
        i, j = host_of(name, left), host_of(name, right)
        ci, cj = chunk_rates(name, i), chunk_rates(name, j)
        out = []
        for k in range(min(len(ci), len(cj))):
            if ci[k].get(key) and cj[k].get(key):
                out.append((ci[k][key] - cj[k][key]) / cj[k][key] * 100.0)
        return out

    def pair_stats(left, right, fwd, rev, key):
        df = dir_effect(fwd, left, right, key)
        dr = dir_effect(rev, left, right, key)
        if df is None or dr is None:
            return None
        cf = chunk_effects(fwd, left, right, key)
        cr = chunk_effects(rev, left, right, key)
        m = min(len(cf), len(cr))
        reps = [(cf[k] + cr[k]) / 2.0 for k in range(m)]
        se = None
        if len(reps) > 1:
            mu = sum(reps) / len(reps)
            var = sum((x - mu) ** 2 for x in reps) / (len(reps) - 1)
            se = (var / len(reps)) ** 0.5
        return {"fwd": df, "rev": dr, "eff": (df + dr) / 2.0,
                "bias": (df - dr) / 2.0, "reps": reps, "se": se}

    P = {}
    for i, (left, right, fwd, rev) in enumerate(PAIRS, 1):
        P["P%d" % i] = pair_stats(left, right, fwd, rev, "msg")
        P["P%d_pos" % i] = pair_stats(left, right, fwd, rev, "pos")

    def f1(v):
        return "-" if v is None else "%+.2f%%" % v

    out = []
    out.append("# 守卫 × 拓扑 四组合交叉比对 **v3（背靠背设计）**\n")
    out.append("- 判据：`msg/s`、`valid/s`、`pos/s` 取 `total` 累计计数器首尾差；"
               "`c = 128·10^(noise/20)`（守卫标度，可行域 [1.3, 20]）。")
    out.append("- 稳态窗：两机**共用窗** `[max(t0)+%d, min(t1)]`；亚窗 %.0f s。"
               % (args.skip, CHUNK_S))
    out.append("- 每对两个方向**背靠背**（相邻阶段）⇒ 空情混杂被压到最小；"
               "亚窗重复给出标准误。")
    out.append("- 相对差口径：(左 − 右)/右 × 100%；`效应 = (正向+反向)/2`，"
               "`机器偏置 = (正向−反向)/2`。\n")

    # ---- 1. 阶段原始读数 ----
    out.append("\n## 1. 阶段原始读数（共用窗）\n")
    out.append("| 阶段 | 时长s | .84 组合 | msg/s | valid/s | pos/s | c 均值 | c max | 越界% "
               "| .85 组合 | msg/s | valid/s | pos/s | c 均值 | c max | 越界% |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, c84, c85 in PHASES:
        a, b = stat.get((name, "84")), stat.get((name, "85"))

        def cells(s):
            if not s:
                return ["-"] * 6
            return ["%.1f" % s["msg"] if s["msg"] else "-",
                    "%.1f" % s["valid"] if s["valid"] else "-",
                    "%.2f" % s["pos"] if s["pos"] else "-",
                    "%.2f" % s["c_mean"], "%.1f" % s["c_max"],
                    "%.0f%%" % s["oob"]]

        dur = "%.0f" % a["dur"] if a else ("%.0f" % b["dur"] if b else "-")
        row = ([name, dur, COMBO[c84]] + cells(a) + [COMBO[c85]] + cells(b))
        out.append("| " + " | ".join(row) + " |")

    # ---- 2. 6 对 ----
    out.append("\n## 2. 六对组合（背靠背）\n")
    out.append("| 对 | 左组合 | 右组合 | 阶段(正/反) | msg 正向 | msg 反向 | "
               "**msg 效应** | msg 机器偏置 | pos 效应 | pos 亚窗 SE | 亚窗数 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, (left, right, fwd, rev) in enumerate(PAIRS, 1):
        s, sp = P.get("P%d" % i), P.get("P%d_pos" % i)
        if not s:
            out.append("| P%d | %s | %s | %s/%s | - | - | - | - | - | - | - |"
                       % (i, left, right, fwd, rev))
            continue
        out.append("| P%d | %s | %s | %s/%s | %s | %s | **%s** | %s | %s | %s | %d |"
                   % (i, left, right, fwd, rev,
                      f1(s["fwd"]), f1(s["rev"]), f1(s["eff"]), f1(s["bias"]),
                      f1(sp["eff"]) if sp else "-",
                      "%.2f" % sp["se"] if sp and sp["se"] is not None else "-",
                      len(s["reps"])))

    # ---- 3. 冗余闭合检验 ----
    out.append("\n## 3. 冗余闭合检验（设计自检）\n")
    out.append("4 个单元格只有 3 个自由度，6 对差之间存在 3 条恒等式。"
               "**闭合 = 加性成立、取数无符号/配对错误**；不闭合则说明存在交互或仍有混杂。\n")
    closure = [
        ("P4 = P1 + P2", "P4", [("P1", +1), ("P2", +1)]),
        ("P5 = P1 + P3", "P5", [("P1", +1), ("P3", +1)]),
        ("P6 = P3 − P2", "P6", [("P3", +1), ("P2", -1)]),
    ]
    out.append("| 恒等式 | msg 实测 | msg 预测 | msg 残差 | pos 实测 | pos 预测 | pos 残差 |")
    out.append("|---|---|---|---|---|---|---|")
    resid = {"msg": [], "pos": []}
    for label, tgt, terms in closure:
        sm, sp_ = P.get(tgt), P.get(tgt + "_pos")
        pm = sum(sgn * P[n]["eff"] for n, sgn in terms if P.get(n))
        pp = sum(sgn * P[n + "_pos"]["eff"] for n, sgn in terms if P.get(n + "_pos"))
        rm = (sm["eff"] - pm) if sm else None
        rp = (sp_["eff"] - pp) if sp_ else None
        if rm is not None:
            resid["msg"].append(rm)
        if rp is not None:
            resid["pos"].append(rp)
        out.append("| %s | %s | %s | %s | %s | %s | %s |"
                   % (label, f1(sm["eff"]) if sm else "-", f1(pm), f1(rm),
                      f1(sp_["eff"]) if sp_ else "-", f1(pp), f1(rp)))
    out.append("")
    for key, name in (("msg", "msg/s"), ("pos", "pos/s")):
        if resid[key]:
            out.append("- %s 闭合残差绝对值：均值 **%.2f pp**、最大 **%.2f pp**（n=%d）"
                       % (name, sum(abs(x) for x in resid[key]) / len(resid[key]),
                          max(abs(x) for x in resid[key]), len(resid[key])))

    # ---- 4. 空白对照 ----
    out.append("\n## 4. 空白对照（重复性 / 漂移）\n")
    left, right, f1p, f2p, z1, z2 = BLANK
    zp_ = pair_stats(left, right, z1, z2, "msg")
    zpp = pair_stats(left, right, z1, z2, "pos")
    if P.get("P1") and zp_:
        out.append("同一对组合（%s × %s）在开头（P1）与末尾（Z）各跑一遍，"
                   "两者之差直接给出「漂移 + 重复性」：\n" % (left, right))
        out.append("| 口径 | 开头 P1 | 末尾 Z | 差（Z − P1） |")
        out.append("|---|---|---|---|")
        out.append("| msg/s 效应 | %s | %s | %s |"
                   % (f1(P["P1"]["eff"]), f1(zp_["eff"]),
                      "%+.2f pp" % (zp_["eff"] - P["P1"]["eff"])))
        if P.get("P1_pos") and zpp:
            out.append("| pos/s 效应 | %s | %s | %s |"
                       % (f1(P["P1_pos"]["eff"]), f1(zpp["eff"]),
                          "%+.2f pp" % (zpp["eff"] - P["P1_pos"]["eff"])))
        out.append("")
        out.append("⇒ 该差值是**任何单对估计的噪声下限量级**；"
                   "若它大于某对效应本身，那个效应就不能当结论。")
    else:
        out.append("（缺少 Z1/Z2 或 P1，跳过）")

    # ---- 5. 主效应汇总 ----
    out.append("\n## 5. 主效应汇总（2×2 因子）\n")
    def cell(k):
        return P.get(k)["eff"] if P.get(k) else None
    g1, g2 = cell("P1"), cell("P6")          # 守卫：R-on−R-off；S-on−S-off = −P6
    t1, t2 = cell("P2"), cell("P5")          # 拓扑：R-off−S-off；R-on−S-on
    rows = [
        ("守卫效应（R 拓扑内：R-on − R-off）", g1),
        ("守卫效应（S 拓扑内：S-on − S-off）", -g2 if g2 is not None else None),
        ("拓扑效应（无守卫：R-off − S-off）", t1),
        ("拓扑效应（有守卫：R-on − S-on）", t2),
        ("交互（R-off − S-on）", cell("P3")),
        ("交互（R-on − S-off）", cell("P4")),
    ]
    out.append("| 项目 | msg/s |")
    out.append("|---|---|")
    for label, v in rows:
        out.append("| %s | %s |" % (label, f1(v)))
    gm = [x for x in (rows[0][1], rows[1][1]) if x is not None]
    tm = [x for x in (t1, t2) if x is not None]
    out.append("")
    if gm:
        out.append("- **守卫主效应** = %+.2f pp（两拓扑平均，n=%d）" % (sum(gm) / len(gm), len(gm)))
    if tm:
        out.append("- **拓扑主效应** = %+.2f pp（guard on/off 平均，n=%d）" % (sum(tm) / len(tm), len(tm)))
    out.append("- ⚠ 每组只有 n=2 个独立估计，标准误只能作量级参考；"
               "真正的判断依据是 §3 闭合残差与 §4 空白对照。")

    # ---- 6. 亚窗明细 ----
    out.append("\n## 6. 亚窗明细（效应随时间的稳定性）\n")
    out.append("| 对 | 亚窗 msg 效应（逐个） | 均值 ± SE |")
    out.append("|---|---|---|")
    for i in range(1, len(PAIRS) + 1):
        s = P.get("P%d" % i)
        if not s or not s["reps"]:
            continue
        out.append("| P%d | %s | %+.2f ± %.2f pp |"
                   % (i, " / ".join("%+.2f" % x for x in s["reps"]),
                      s["eff"], s["se"] if s["se"] is not None else 0.0))

    # ---- 7. DF 分布 ----
    import glob as _glob
    dfs = {}
    ddir = os.path.join(raw, "df")
    if os.path.isdir(ddir):
        for fp in sorted(_glob.glob(os.path.join(ddir, "*.txt"))):
            base = os.path.basename(fp)[:-4]
            if "_" not in base:
                continue
            name, tag = base.rsplit("_", 1)
            summ = {}
            for line in open(fp, encoding="utf-8", errors="replace"):
                if line.startswith("SUMMARY"):
                    for kv in line.split()[1:]:
                        k, _, v = kv.partition("=")
                        summ[k] = v
            if summ:
                dfs[(name, tag)] = summ
    if dfs:
        out.append("\n## 7. 报文构成（L3：msg 与 pos 是不是同一件事）\n")
        out.append("readsb 的 `position_count_total` 数的是**带 CPR 的位置报文**"
                   "（`track.c:1101`，DF17/18 里 TC 5–8 / 9–18 / 20–22），"
                   "不是全部 DF17 ⇒ 只有 `posmsg%` 才能和 pos/msg 对照。\n")
        out.append("| 阶段 | .84 组合 | DF17+18% | 位置报文% | 实测 pos/msg | "
                   ".85 组合 | DF17+18% | 位置报文% | 实测 pos/msg |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for name, c84, c85 in PHASES:
            row = [name]
            for tag, lab in (("84", COMBO[c84]), ("85", COMBO[c85])):
                d = dfs.get((name, tag))
                s = stat.get((name, tag))
                meas = "-"
                if s and s["msg"] and s["pos"]:
                    meas = "%.2f%%" % (s["pos"] / s["msg"] * 100.0)
                row += [lab,
                        (d.get("pos_pct", "-") + "%") if d else "-",
                        (d.get("posmsg_pct", "-") + "%") if d else "-",
                        meas]
            out.append("| " + " | ".join(row) + " |")

    txt = "\n".join(out)
    p = os.path.join(raw, "xover_stats.md")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    print(txt)
    print("\n-> " + p)
    return 0
# ------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("precheck").set_defaults(f=cmd_precheck)

    b = sub.add_parser("bootstrap", help="全新硬件：只投守卫脚本并自行收敛")
    b.add_argument("--selftest", type=int, default=20)
    b.add_argument("--interval", type=float, default=15.0)
    b.add_argument("--every", type=float, default=30.0)
    b.add_argument("--watch", type=float, default=10.0, help="观察分钟")
    b.add_argument("--outdir", default=DEFAULT_OUTDIR)
    b.set_defaults(f=cmd_bootstrap)

    r = sub.add_parser("run")
    r.add_argument("--min", dest="min_", type=float, default=12.0,
                   help="每阶段分钟数（14 阶段 × 该值）")
    r.add_argument("--every", type=float, default=20.0, help="采样间隔秒")
    r.add_argument("--interval", type=float, default=15.0, help="守卫轮询间隔秒")
    r.add_argument("--df-at", dest="df_at", type=float, default=300.0,
                   help="阶段开始后多少秒启动 DF 计数（默认 300）")
    r.add_argument("--df-sec", dest="df_sec", type=float, default=60.0,
                   help="DF 计数时长秒（默认 60）")
    r.add_argument("--code", default=None, help="强制起点码（缺省按 deployed.json）")
    r.add_argument("--from", dest="from_phase", default=None,
                   metavar="PHASE",
                   help="续跑：跳过 reboot/部署并从该阶段开始"
                        "（如 P3a）。仅用于同一轮内被中断后的接续")
    r.add_argument("--outdir", default=DEFAULT_OUTDIR)
    r.set_defaults(f=cmd_run)

    sub.add_parser("probe").set_defaults(f=cmd_probe)

    a = sub.add_parser("analyze")
    a.add_argument("--outdir", default=DEFAULT_OUTDIR)
    a.add_argument("--skip", type=int, default=240)
    a.set_defaults(f=cmd_analyze)

    args = ap.parse_args()
    return args.f(args)


if __name__ == "__main__":
    sys.exit(main())
