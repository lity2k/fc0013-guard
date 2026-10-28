#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code_scan.py — 单节点 IF 增益码扫描（判定 c_rms 由谁决定）。

用途：在 readsb --gain 固定（LNA 0x14 恒定）的前提下，依次钉不同 0x13
码并采样 noise / c_rms。若 c_rms 随 0x13 变化而 --gain 全程未变 ⇒
说明 c_rms 由 0x13 决定，不由 --gain 决定。

⚠ 运行前必须停掉守卫（否则 canary 会把码写回），跑完重启。

用法：
  python3 /tmp/code_scan.py 0x10 0x12 0x11     # 末项即恢复码
"""
import json
import sys
import time

sys.path.insert(0, "/tmp")
import fc0013_guard as G                                     # noqa: E402

STATS = "/run/readsb/stats.json"
SETTLE = 70          # readsb noise 是 60s 滑动窗，换码后须等它收敛
GAP = 15


def sample(n=3):
    out = []
    for _ in range(n):
        try:
            lo = json.load(open(STATS))["last1min"]["local"]
            out.append((lo["noise"], lo["peak_signal"], G.c_rms(lo["noise"])))
        except Exception as e:
            out.append((None, None, None))
            print("   stats 读取失败: %s" % e)
        time.sleep(GAP)
    return out


def main():
    codes = [int(x, 0) & 0x1F for x in (sys.argv[1:] or ["0x10", "0x12", "0x11"])]
    fc = G.Fc()
    print("init regs (0d,13,14) = %s" % (fc.read_013(),))
    print("C_MIN=%.1f C_MAX=%.1f" % (G.C_MIN_DEFAULT, G.C_MAX_DEFAULT))
    for code in codes:
        ok, info = fc.set_code(code)
        print("set 0x%02x -> %s | %s" % (code, ok, info))
        time.sleep(SETTLE)
        s = sample(3)
        r = fc.read_013()
        ns = ["%.1f" % x[0] if x[0] is not None else "??" for x in s]
        cs = ["%.2f" % x[2] if x[2] is not None else "??" for x in s]
        ps = ["%.1f" % x[1] if x[1] is not None else "??" for x in s]
        print("  code=0x%02x regs=%s | noise=%s c=%s peak=%s"
              % (code, r, ns, cs, ps))
    print("DONE")


if __name__ == "__main__":
    main()
