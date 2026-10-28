#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipe_only.py — 对照臂（OFF）专用：只起数据管道，不做任何芯片治理。

为什么要单独一个文件，而不是用 s1090_pipe.sh：
  1. `s1090_pipe.sh` 末段是 `socat`，socat 是 apt 装的、节点根 fs 是 overlay
     ⇒ reboot 后必然 `socat: not found`，管道 2 s 内起停，对照臂变成"没有数据流"；
  2. 更本质：ON 臂的管道是守卫 `--mode s1090` 内建的。若 OFF 臂用另一份实现，
     "ON vs OFF"就混入了"两份管道实现的差异"——实验要隔离的只是"守卫管不管芯片"。
     ⇒ 这里直接复用 `fc0013_guard.S1090Pipe`，两臂管道逐字节一致。

用法：python3 /tmp/pipe_only.py [port]
"""
import sys
import time

sys.path.insert(0, "/tmp")
import fc0013_guard as G                                          # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else G.S1090_PORT_DEFAULT
exes = {}
for name in ("rtl_sdr", "tee_power", "stream1090"):
    p = G.find_exe(name)
    if not p:
        print("MISSING %s" % name)
        sys.exit(2)
    exes[name] = p

pipe = G.S1090Pipe(G.TEE_DEFAULT, port, "/tmp/xo_pipe.log")
pipe.start(exes["rtl_sdr"], exes["tee_power"], exes["stream1090"])
print("PIPE_ONLY started pid=%d port=%d" % (pipe.procs[0].pid, port))
sys.stdout.flush()

while True:
    time.sleep(600)
