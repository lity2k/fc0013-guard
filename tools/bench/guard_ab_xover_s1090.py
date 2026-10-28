#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard_ab_xover_s1090.py — 守卫「用 / 不用」双机交叉比对（**stream1090 + readsb --net-only** 拓扑）。

与 readsb 直采拓扑（`guard_ab_xover_readsb.py`）的关系
------------------------------------------------------------
实验设计、判据、2×2 交叉、reboot 开场纪律**完全沿用**；只有部署形态不同：

  直采                readsb 独占 SDR，观测 = stats.json 的 last1min.local.noise
  stream1090（本实验） readsb 只解码，管道 = rtl_sdr|tee_power|stream1090 → 30001
                      观测 = tee_power CSV 的 mean_dbfs（最近 30 s 中位数）

⇒ 三处必须改（否则拿到的是错数据）：
  1. net-only 下 `last1min.local` **整个为空** ⇒ 采样器的 noise 必须取自 tee CSV；
  2. ON 臂的守卫带 `--mode s1090`（自带管道）；**OFF 臂没有守卫 ⇒ 管道要单独起**
     （用 `s1090_pipe.sh`），否则 OFF 臂没有数据流、比的是"死机 vs 活机"；
  3. reboot 会清 `/tmp`（二进制没了）并让 overlay 的 `/etc/default/readsb` 复原
     ⇒ 每轮必须先重推 stream1090/tee_power/管道脚本，再把 readsb 改回 `--net-only`。

本实验要回答的问题
------------------
直采拓扑 B 段（AGC 注入）无守卫代价是 −30%~−35% 报文。而 s1090 形态下
stream1090 的 3:10 FIR 内插会平滑 ADC 量化台阶 ⇒ **抬码在 stream1090 下可能没有收益**
（甚至因为过驱动更早撞顶而变差）。本实验在 B 段直接验证这一点。

子命令
------
  bootstrap  全新硬件部署：环境预检 → 只上传守卫脚本 → 通道自检 → 启动并观察收敛
  run        A/B 交叉比对（A 段自然空情 + B 段注入 AGC 扰动）
  probe      即时状态查询
  analyze    从 raw CSV 出对比表

判据（预注册，取数前定死，事后不得改判）
----------------------------------------
  msg/s   = Δtotal.messages / Δt
  valid/s = Δtotal.messages_valid / Δt
  pos/s   = Δtotal.position_count_total / Δt   （DF17 位置报文，最稳健）
  c       = 128·10^(noise/20)（守卫标度）；越界占比 = (c<1.3 或 c>20)/N
  ⚠ msg/1e6 归一化**不可用**：net-only 下没有 `samples_processed`（readsb 不解调）

  阈值：A 段两臂 msg/s 相对差 |Δ| < 5% ⇒ 「守卫零打扰」；
        B 段 ON 臂 c 回域内、OFF 臂越界占比 > 80% ⇒ 「守卫有效」。

字段位置（2026-10-24 实测，net-only）
-------------------------------------
  total / last1min 顶层 : messages, messages_valid, position_count_total  ← 可用
  total.local           : **None**（readsb 不跑 demodulate2400）
  total.remote          : modes/accepted/bytes_in 等，可作吞吐旁证
  ⇒ 吞吐用顶层累计计数器首尾差；工况量 c 用 tee_power CSV。

实验纪律（2026-10-24 用户指定）
--------------------------------
  每一轮交叉实验都**以 reboot 开场**：两机重启 → 核验 /tmp 为空、无残留进程、
  readsb 起、时钟同步 → 再部署与取样。目的是保证"干净起点"可复现。
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
SAMPLER_CSV = "/tmp/xo_%s.csv"
SAMPLER_LOG = "/tmp/xo_sampler_%s.out"
PIPE = "/tmp/pipe_only.py"                   # OFF 臂用（复用守卫的 S1090Pipe）
TEE = "/tmp/tee_power.csv"                   # 唯一底噪观测源（net-only）
MODE = "s1090"
PIPE_LOG = "/tmp/xo_pipe.log"
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
"""xo_sampler.py — 节点侧常驻采样（stream1090 + readsb --net-only 形态）。

吞吐：/run/readsb/stats.json 顶层累计计数器（net-only 下 remote 报文仍累加到 total）。
底噪：/tmp/tee_power.csv 的 mean_dbfs。net-only 下 stats.json 的 local 整个为空，
      且口径必须与守卫一致 ⇒ 直接复用守卫的 read_tee()，不另写一份（避免两套口径）。
"""
import json, sys, time

sys.path.insert(0, "/tmp")
tag, every, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
HDR = ("ts,wall,t_msg,t_valid,t_pos,sp,sd,noise,peak,signal,strong,"
       "acc0,l1_valid,l1_pos,l1_modes,l1_sp\n")

try:
    import fc0013_guard as G
except Exception:
    G = None

with open(out, "w") as f:
    f.write(HDR)

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
        rem = tot.get("remote", {}) or {}
        acc = rem.get("accepted") or [0, 0]
        l1rem = l1.get("remote", {}) or {}
        row = (ts, wall,
               g(tot, "messages", 0), g(tot, "messages_valid", 0),
               g(tot, "position_count_total", 0),
               # sp/sd：net-only 下 readsb 不解调 ⇒ 无 samples_processed，置 0
               0, 0,
               # 底噪：tee_power mean_dbfs 最近 30 s 中位数（与守卫同口径）
               tee_noise(),
               "", "", "",
               acc[0] if isinstance(acc, list) else "",
               g(l1, "messages_valid", 0), g(l1, "position_count_total", 0),
               g(l1rem, "modes", 0), g(l1, "messages", 0))
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
    sh(host, "for p in $(pgrep -f 'fc0013_gua[r]d'); do kill -9 $p; done; "
              "rm -f %s; true" % GUARD_LOCK, timeout=30)


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
    cmd = ("(setsid python3 %s </dev/null >>/tmp/xo_pipe_%s.out 2>&1 &) ; "
           "sleep 8; echo -n rtl=; pgrep -c -x rtl_sdr; echo -n ' s1090='; "
           "pgrep -c -x stream1090; echo -n ' pipe='; "
           "pgrep -c -f 'pipe_onl[y]'"
           % (PIPE, host.lstrip(".")))
    return " ".join(sh(host, cmd, timeout=70, tries=1).split())


def guard_start(host, code, interval=15.0, mode=MODE):
    """显式起点码启动（--cal CODE）。s1090 模式下守卫会自带整条管道。"""
    guard_stop(host)
    pipe_stop(host)
    cmd = ("(setsid python3 %s --node %s --mode %s --cal %s --interval %s "
           "--log %s </dev/null >>/tmp/xo_guard_%s.out 2>&1 &) ; sleep 9; "
           "cat %s 2>/dev/null || echo NOPID"
           % (GUARD, host.lstrip("."), mode, code, interval, GUARD_LOG,
              host.lstrip("."), GUARD_LOCK))
    out = sh(host, cmd, timeout=60, tries=1)
    return out.strip().splitlines()[-1].strip()


def guard_start_raw(host, interval=15.0):
    """新用户路径：不带 --cal，起点码由脚本自解析（读回 0x13 > DEFAULT_CODE）。"""
    guard_stop(host)
    cmd = ("(setsid python3 %s --node %s --apply --interval %s --log %s "
           "</dev/null >>/tmp/xo_guard_%s.out 2>&1 &) ; sleep 4; "
           "cat %s 2>/dev/null || echo NOPID"
           % (GUARD, host.lstrip("."), interval, GUARD_LOG,
              host.lstrip("."), GUARD_LOCK))
    out = sh(host, cmd, timeout=45, tries=1)
    return out.strip().splitlines()[-1].strip()


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


def start_sampler(host, every=20.0):
    sh(host, "for p in $(pgrep -f 'xo_sampl[e]r'); do kill -9 $p; done; true",
       timeout=30)
    cmd = ("(setsid python3 %s %s %s %s </dev/null >>%s 2>&1 &) ; sleep 2; "
           "pgrep -f 'xo_sampl[e]r' | head -2"
           % (SAMPLER, host.lstrip("."), every,
              SAMPLER_CSV % host.lstrip("."), SAMPLER_LOG % host.lstrip(".")))
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


def set_net_only(host):
    """readsb 降级为纯解码器。

    ⚠ 节点根 fs 是 overlay，`/etc/default/readsb` 重启即复原 ⇒ **每轮都必须重做**，
      否则一看"readsb 在跑"就以为环境就绪，实际上 SDR 又被它独占了。
    """
    sh(host, "cp -n /etc/default/readsb /tmp/readsb.default.bak 2>/dev/null; "
             "sed -i 's|^RECEIVER_OPTIONS=.*|RECEIVER_OPTIONS=\"--net-only\"|' "
             "/etc/default/readsb", timeout=60)
    sh(host, "systemctl restart readsb", timeout=90)
    time.sleep(8)
    out = sh(host, "grep -h RECEIVER_OPTIONS /etc/default/readsb; "
                   "echo -n 'netonly_procs='; "
                   "pgrep -f 'readsb .*--net-o[n]ly' | wc -l; "
                   "echo -n ' active='; systemctl is-active readsb",
             timeout=60)
    return " | ".join(x.strip() for x in out.strip().splitlines() if x.strip())


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
    """采一次：底噪取 tee_power（net-only 下 stats 的 local 为空），吞吐取 total 累计计数器。"""
    cmd = ("python3 -c \"import json,sys;sys.path.insert(0,'/tmp');"
           "s=json.load(open('/run/readsb/stats.json'));t=s['total'];"
           "import fc0013_guard as G;r=G.read_tee();"
           "n=(r['noise'] if r else 0.0);"
           "print('S',round(n,2),0.0,0,int(t['messages']),"
           "int(t['messages_valid']),int(t['position_count_total']),0)\" 2>&1")
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
PHASES = [
    ("A1", "on",  "off", "a", False),
    ("A2", "off", "on",  "a", False),
    ("B1", "on",  "off", "b", True),
    ("B2", "off", "on",  "b", True),
]


def cmd_run(args):
    outdir = args.outdir
    for p in (outdir, os.path.join(outdir, "raw")):
        if not os.path.isdir(p):
            os.makedirs(p)
    pfile = open(os.path.join(outdir, "raw", "phases.jsonl"), "a")

    a_sec, b_sec = args.a_min * 60.0, args.b_min * 60.0
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
        phases = [p for p in PHASES if names.index(p[0]) >= names.index(args.from_phase)]
        print("续跑：从阶段 %s 开始（跳过部署与采样器重启）" % args.from_phase)

    if not args.from_phase:
        reboot_pair()          # 每轮必修：干净起点
        print("\n== 部署就位（脚本 + 二进制；reboot 已清 /tmp）==")
        for tag, ip in HOSTS:
            print("  .%s" % tag)
            deploy(ip)

        print("\n== readsb 降级为 --net-only（overlay 的 /etc 重启即复原）==")
        for tag, ip in HOSTS:
            print("  .%s %s" % (tag, set_net_only(ip)))

        print("\n== 启动采样器 ==")
        for tag, ip in HOSTS:
            print("  .%s pid=%s" % (tag, " ".join(start_sampler(ip, args.every))))

    for name, r84, r85, unit, disturb in phases:
        roles = {"84": r84, "85": r85}
        secs = a_sec if unit == "a" else b_sec
        print("\n== 阶段 %s (%s) %.0fs  84=%s 85=%s  扰动=%s =="
              % (name, unit, secs, r84, r85, "AGC-ON" if disturb else "无"))
        for _, ip in HOSTS:
            guard_stop(ip)
            pipe_stop(ip)          # 守卫的子进程不会被 kill -9 连带带走
        time.sleep(2)
        # 先起臂（ON=守卫自带管道 / OFF=独立管道），**后 pin**：
        # rtl_sdr 启动会把 0x13 抹回 0x0a 附近，先 pin 等于白做
        # （直采拓扑没有这个问题，管道拓扑必须调序）。
        for tag, ip in HOSTS:
            if roles[tag] == "on":
                print("  .%s 守卫 pid=%s"
                      % (tag, guard_start(ip, codes[tag], args.interval)))
            else:
                print("  .%s 对照臂：独立管道 %s" % (tag, pipe_start(ip)))
        for tag, ip in HOSTS:
            print("  .%s arm -> %s" % (tag, arm(ip, codes[tag])))
        time.sleep(4)
        t0 = {}
        for tag, ip in HOSTS:
            t0[tag] = node_epoch(ip)
            print("  .%s 起点 0d/13/14=%s" % (tag, fmt_regs(read_regs(ip))))
        inj = {}
        if disturb:
            time.sleep(5)
            for tag, ip in HOSTS:
                inj[tag] = node_epoch(ip)
                print("  .%s 注入 AGC ON: %s" % (tag, inject_agc(ip)))
        t_end = time.time() + secs
        nxt = time.time() + 120.0
        while time.time() < t_end:
            time.sleep(10)
            if time.time() >= nxt:
                nxt = time.time() + 120.0
                print("    …剩余 %.0fs" % max(t_end - time.time(), 0), flush=True)
        # 末态只**读文件**取守卫日志，不回读寄存器
        # （观测进程每轮读 I2C 会与守卫抢同一把 flock，互相把读回打错位）
        for tag, ip in HOSTS:
            tail = sh(ip, "tail -3 /tmp/fc_guard.log 2>/dev/null", timeout=30)
            print("  .%s 末态守卫日志：%s"
                  % (tag, (tail.strip().splitlines() or ["(空)"])[-1]))
            pfile.write(json.dumps({
                "phase": name, "host": tag, "role": roles[tag],
                "t0": t0[tag], "t1": node_epoch(ip),
                "inject": inj.get(tag), "code": codes[tag]}) + "\n")
        pfile.flush()
    pfile.close()

    print("\n== 收尾：两机恢复生产态（守卫 ON + 钉码）==")
    for tag, ip in HOSTS:
        arm(ip, codes[tag])
        print("  .%s 守卫 pid=%s"
              % (tag, guard_start(ip, codes[tag], args.interval)))

    print("\n== 拉取采样数据 ==")
    for tag, ip in HOSTS:
        stop_sampler(ip)
        data = nb_ssh.get_file(ip, SAMPLER_CSV % ip.lstrip("."),
                               tries=3, gap=5, timeout=60)
        if data:
            p = os.path.join(outdir, "raw", "xo_%s.csv" % tag)
            with open(p, "wb") as f:
                f.write(data)
            print("  .%s -> %s (%d bytes)" % (tag, p, len(data)))
        else:
            print("  .%s 拉取失败" % tag)
    print("\nrun done ->", outdir)
    return 0


# ------------------------------------------------------------ analyze
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

    out = []
    out.append("# 守卫 A/B 交叉比对结果（.84 / .85）\n")
    out.append("判据（预注册）：msg/s、valid/s、pos/s 取 `total` 累计计数器首尾差；"
               "msg/1e6 = Δmessages/Δsamples_processed×1e6；"
               "c = 128·10^(noise/20)（守卫标度，可行域 [1.3, 20]）。")
    out.append("稳态窗：跳过每阶段前 %d s（noise 为 60 s 级累加平均）。\n" % args.skip)

    for name, _, _, _, disturb in PHASES:
        out.append("\n## 阶段 %s（扰动=%s）\n" % (name, "AGC-ON" if disturb else "无"))
        out.append("| 主机 | 角色 | 样本 | msg/s | valid/s | pos/s | msg/1e6 | c 均值 | c 区间 | 越界% |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        cur = {}
        for tag, _ in HOSTS:
            ph = [p for p in phases if p["phase"] == name and p["host"] == tag]
            if not ph or tag not in csvs:
                continue
            ph = ph[-1]
            t0 = (ph["inject"] or ph["t0"]) if disturb else ph["t0"]
            t0 += args.skip
            win = [r for r in csvs[tag]
                   if t0 <= num(r.get("ts") or "") <= ph["t1"]
                   and not str(r.get("t_msg", "")).startswith("ERR")]
            if len(win) < 3:
                out.append("| .%s | %s | %d | - | - | - | - | - | - | - |"
                       % (tag, ph["role"], len(win)))
                continue
            rm, rv, rp = rate(win, "t_msg"), rate(win, "t_valid"), rate(win, "t_pos")
            dsp = num(win[-1]["sp"]) - num(win[0]["sp"])
            dmsg = num(win[-1]["t_msg"]) - num(win[0]["t_msg"])
            norm = (dmsg / dsp * 1e6) if dsp else None
            cs = [x for x in (c_of(r["noise"]) for r in win) if x is not None]
            oob = (sum(1 for x in cs if x < 1.3 or x > 20.0) / float(len(cs)) * 100
                   if cs else float("nan"))
            out.append("| .%s | %s | %d | %s | %s | %s | %s | %.2f | %.2f–%.2f | %.1f%% |" % (
                tag, ph["role"], len(win),
                "%.1f" % rm if rm is not None else "-",
                "%.1f" % rv if rv is not None else "-",
                "%.3f" % rp if rp is not None else "-",
                "%.2f" % norm if norm is not None else "-",
                sum(cs) / len(cs) if cs else float("nan"),
                min(cs) if cs else float("nan"),
                max(cs) if cs else float("nan"), oob))
            cur[ph["role"]] = {"msg": rm, "valid": rv, "pos": rp, "norm": norm,
                               "c": (sum(cs) / len(cs)) if cs else None,
                               "oob": oob, "host": tag}
        if "on" in cur and "off" in cur and cur["off"].get("msg"):
            d = (cur["on"]["msg"] - cur["off"]["msg"]) / cur["off"]["msg"] * 100.0
            out.append("\n**msg/s 相对差（ON vs OFF）：%+.2f%%**（ON=%.1f @.%s, "
                       "OFF=%.1f @.%s；|Δ|<5%% ⇒ 判定无差别）"
                       % (d, cur["on"]["msg"], cur["on"]["host"],
                          cur["off"]["msg"], cur["off"]["host"]))
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
    r.add_argument("--a-min", type=float, default=20.0)
    r.add_argument("--b-min", type=float, default=12.0)
    r.add_argument("--every", type=float, default=20.0, help="采样间隔秒")
    r.add_argument("--interval", type=float, default=15.0, help="守卫轮询间隔秒")
    r.add_argument("--code", default=None, help="强制起点码（缺省按 deployed.json）")
    r.add_argument("--from", dest="from_phase", default=None,
                   metavar="PHASE",
                   help="续跑：跳过前面的阶段与部署/采样器重启"
                        "（如 A2/B1）。节点侧进程已在运行时用")
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
