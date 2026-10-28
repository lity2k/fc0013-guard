#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""soak_probe.py — 节点侧一次性采样（供本机 soak_guard.py 轮询），输出一行 JSON。

放在 /tmp，由 soak_guard.py 每轮 exec 调用。
"""
import json
import subprocess
import sys

sys.path.insert(0, "/tmp")
out = {}
p = subprocess.run(
    ["bash", "-c", "ps -eo pid,args | grep 'fc0013_gua[r]d' | grep -v 'bash -c'"],
    capture_output=True, text=True, timeout=10)
lines = [x for x in p.stdout.strip().split("\n") if x.strip()]
out["pids"] = [x.split()[0] for x in lines]

try:
    out["loglines"] = sum(1 for _ in open("/tmp/fc_guard.log"))
except Exception:
    out["loglines"] = -1

try:
    import fc0013_guard as G
    out["regs"] = G.Fc().read_013()
except Exception as e:
    out["regs"] = None
    out["regs_err"] = str(e)[:60]
    G = None

try:
    s = json.load(open("/run/readsb/stats.json"))["last1min"]["local"]
    out["noise"] = s["noise"]
    out["peak"] = s["peak_signal"]
    out["strong"] = s.get("strong_signals")
    if G is not None:
        out["c"] = round(G.c_rms(s["noise"]), 3)
except Exception as e:
    out["stats_err"] = str(e)[:60]

print("JSON " + json.dumps(out))
