#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orchestrate_verify.py — 三机（.84/.85/.86）外推算法实机验证编排。

动作：对每台并行：put fc0013_guard.py + verify_node.py → /tmp；
     nohup 启动（远端后台，避免 SSH 长连接超时）→ 轮询 /tmp/verify_out.log
     直到出现 RESULT 行（或超时 8 min）→ 汇总打印 → 回收日志。
用法：NODE_SSH_PASS=... python orchestrate_verify.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "tools")))
import nb_ssh                                              # noqa: E402

HOSTS = [".84", ".85", ".86"]
OBS = {".84": "stats", ".85": "stats", ".86": "tee"}   # .86 无 readsb → tee
# .86 的 0x10 已接近目标（无外推跨度）⇒ 参考码取 0x13 制造 3 码跨度
REF = {".84": "0x10", ".85": "0x10", ".86": "0x13"}
REMOTE_DIR = "/tmp"
POLL_GAP = 30
POLL_MAX = 16                     # 16 × 30 s = 8 min


def launch(host):
    addr = nb_ssh.resolve(host)      # nb_ssh 的 connect 不做简写解析（仅 CLI 入口做）
    ok1 = nb_ssh.put_text(addr, os.path.join(HERE, "..", "..",
                          "fc0013_guard.py"), REMOTE_DIR + "/fc0013_guard.py")
    ok2 = nb_ssh.put_text(addr, os.path.join(HERE, "verify_node.py"),
                          REMOTE_DIR + "/verify_node.py")
    if not (ok1 and ok2):
        return "put 失败"
    pre = ""
    if OBS[host] == "tee":
        with open(os.path.join(HERE, "..", "..", "tee_power",
                               "tee_power-armhf"), "rb") as f:
            ok3 = nb_ssh.put_bytes(addr, f.read(), REMOTE_DIR + "/tee_power")
        if not ok3:
            return "tee_power put 失败"
        # 兜底：预编译件跑不起来（架构不符）则现场编译
        pre = ("chmod 755 /tmp/tee_power && "
               "/tmp/tee_power --help >/dev/null 2>&1 || "
               "gcc -O2 -o /tmp/tee_power %s/../tee_power/tee_power.c -lm; "
               % REMOTE_DIR)
        # 源码不在节点，现场编译直接用仓库源码上传
        ok4 = nb_ssh.put_text(addr, os.path.join(HERE, "..", "..",
                              "tee_power", "tee_power.c"),
                              REMOTE_DIR + "/tee_power.c")
        if not ok4:
            return "tee_power.c put 失败"
        pre = ("chmod 755 /tmp/tee_power 2>/dev/null; "
               "/tmp/tee_power --help >/dev/null 2>&1 || "
               "gcc -O2 -o /tmp/tee_power /tmp/tee_power.c -lm; ")
    # systemd-run 接管：进程完全脱离 SSH channel（nohup/& 会持有 fd 导致 exec 等 EOF）
    cmd = ("rm -f %s/verify_out.log; systemctl reset-failed fcverify 2>/dev/null; "
           % REMOTE_DIR +
           "%s systemd-run --unit=fcverify --collect "
           "-p StandardOutput=file:%s/verify_out.log "
           "-p StandardError=file:%s/verify_out.log "
           "/usr/bin/python3 %s/verify_node.py --obs %s --code-ref %s 2>&1; sleep 1; "
           "systemctl is-active fcverify; pgrep -af verify_node.py | head -1"
           % (pre, REMOTE_DIR, REMOTE_DIR, REMOTE_DIR, OBS[host], REF[host]))
    out = nb_ssh.exec_host(addr, cmd, timeout=45)
    return out.strip() if out else "launch 无响应"


def main():
    print("== 上传并启动（三机并行）==")
    for h in HOSTS:
        print("%s: %s" % (h, launch(h)))
    print("\n== 轮询结果（每 %d s，全机并行推进）==" % POLL_GAP, flush=True)
    pending = set(HOSTS)
    while pending:
        for h in sorted(pending):
            out = nb_ssh.exec_host(
                nb_ssh.resolve(h),
                "cat %s/verify_out.log 2>/dev/null | tail -40" % REMOTE_DIR,
                timeout=30)
            if out and "RESULT " in out:
                pending.discard(h)
                tail = [l for l in out.splitlines() if l.startswith("RESULT ") or
                        "c_ref=" in l or "c_verify=" in l]
                print("\n---- %s ----\n%s" % (h, "\n".join(tail) or out[-800:]),
                      flush=True)
            elif out:
                last = out.strip().splitlines()[-1] if out.strip() else "(空)"
                print("[%s] %s" % (h, last), flush=True)
        if pending:
            time.sleep(POLL_GAP)
    # 回收日志（get_file 返回 bytes）
    for h in HOSTS:
        dst = os.path.join(HERE, "node_log_%s.log" % h.lstrip("."))
        data = nb_ssh.get_file(nb_ssh.resolve(h), REMOTE_DIR + "/verify_out.log")
        if data:
            with open(dst, "wb") as f:
                f.write(data)
            print("日志已回收: %s" % dst)
        else:
            print("日志回收失败: %s" % h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
