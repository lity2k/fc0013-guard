#!/usr/bin/env python3
"""inject_agc_test.py — 真机注入验证：第三方打开 AGC 后，守卫是否必然介入并写回。

新架构（按需介入）的关键安全声明：域内的第三方写**不再纠正**（已知取舍），
但**第三方开 AGC 仍必被检出**——因为 AGC 压缩态 c_rms 落到 0.71~0.87，
低于 C_MIN=1.3 ⇒ 判欠驱动 ⇒ 介入 ⇒ 此时才读 0x0d ⇒ 检出 AGC 开 ⇒ 写回起点。

本脚本把这条链在真机上跑一遍：开 AGC → 轮询等守卫介入 → 判定是否回到钉码态。

⚠ 影响：AGC 开到守卫纠正之间（实测数十秒到 2 分钟）该机接收基本废掉。
   只应在低峰、单节点上做，做完必须确认回到 0x0d=09 / 0x13=cal。

用法
  python tools/fc0013_guard/inject_agc_test.py .84 0x0a          # 需本机有 paramiko
  python inject_agc_test.py .84 0x0a --wait 240
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nb_ssh import exec_host, resolve  # noqa: E402

READ_CMD = ("python3 -c \"import sys;sys.path.insert(0,'/tmp');"
            "import fc0013_guard as G;fc=G.Fc();print('REGS',fc.read_013())\" 2>&1")


def probe(host, timeout=60):
    """回读一次寄存器 + 守卫日志尾部。返回 (d, i, l, 日志末行)。"""
    out = exec_host(resolve(host),
                    "%s; echo '--- log ---'; tail -4 /tmp/fc_guard.log" % READ_CMD,
                    tries=2, gap=3, timeout=timeout) or ""
    d = i = l = None
    for line in out.splitlines():
        if line.startswith("REGS"):
            try:
                t = line[line.index("(") + 1:line.index(")")]
                d, i, l = (int(x) for x in t.split(","))
            except (ValueError, IndexError):
                pass
    tail = [x for x in out.splitlines() if "STATS" in x or "ACT" in x]
    return d, i, l, (tail[-1].strip() if tail else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("cal", help="标定码，如 0x0a")
    ap.add_argument("--wait", type=int, default=240, help="最长等待秒数")
    ap.add_argument("--poll", type=int, default=20, help="轮询间隔秒")
    a = ap.parse_args()
    cal = int(a.cal, 0) & 0x1F

    d, i, l, _ = probe(a.host)
    print("前态 0d=%s 13=%s 14=%s（期望 0d=09 13=%02x）" % (d, i, l, cal))
    if d is None:
        print("FAIL: 新通道读不到寄存器")
        return 2
    if not (d & 0x08):
        print("FAIL: 前态 AGC 已是开的，先钉回再测")
        return 2

    print("注入：打开 AGC……")
    out = exec_host(resolve(a.host), "/tmp/fc_daemon agcon 2>&1", tries=2, timeout=60) or ""
    print("  agcon ->", out.strip().replace("\n", " | ")[:160])

    t0 = time.time()
    while time.time() - t0 < a.wait:
        time.sleep(a.poll)
        d, i, l, tail = probe(a.host)
        ago = int(time.time() - t0)
        print("  +%3ds 0d=%s 13=%s | %s" % (ago, d, i, tail[:90]))
        if d is not None and (d & 0x08) and (i == cal):
            print("PASS: %ds 内守卫介入并写回钉码态 0d=%02x 13=%02x" % (ago, d, i))
            return 0
    print("FAIL: %ds 内未回到钉码态（末态 0d=%s 13=%s）。"
          "⚠ 须人工 /tmp/fc_daemon set %s --assert 钉回" % (a.wait, d, i, a.cal))
    return 1


if __name__ == "__main__":
    sys.exit(main())
