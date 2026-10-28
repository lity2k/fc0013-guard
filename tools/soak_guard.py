#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""soak_guard.py — 守卫长时间观测（本机跑，SSH 轮询 .84/.85）。

观察三件事：
  1. **稳态不误动**：常态是否一直 `fc0013=未访问 | OK`（不读不写芯片）。
  2. **进程不崩**：PID 是否漂移（漂移 = 守卫自己退出，最该抓的信号）。
  3. **真能完成任务**：越界 / 第三方开 AGC 时是否介入并写回。

产出（--outdir）：
  soak.csv     每样本一行：ts, host, pid, noise, peak, strong, c, 0d/13/14
  events.log   守卫日志里的**非常态**行（ACT/WARN/CHANNEL/START/ALIGN/...）
  summary.txt  结束时自动写的汇总

用法：
  python soak_guard.py --minutes 90 --poll 30
  python soak_guard.py --minutes 20 --poll 15      # 试跑

⚠ 观测本身每轮读一次 FC0013（与守卫共用 /tmp/fc_daemon.lock，不会并发错位），
   但**不写**；故观测不改变工作点，只增加 I2C 读负载。
"""
import argparse
import csv
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import nb_ssh                                                     # noqa: E402

# 观测对象由 --hosts 指定（默认读 tools/nodes.json）
HOSTS = sorted(nb_ssh.HOSTS.items())


def probe(host):
    out = nb_ssh.exec_host(host, "python3 /tmp/soak_probe.py", tries=2, timeout=45)
    if not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("JSON "):
            try:
                return json.loads(line[5:])
            except ValueError:
                return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=90.0)
    ap.add_argument("--poll", type=float, default=30.0)
    ap.add_argument("--outdir", default=os.path.join(ROOT, "data", "长观测"))
    args = ap.parse_args()

    if not os.path.isdir(args.outdir):
        os.makedirs(args.outdir)
    fcsv = open(os.path.join(args.outdir, "soak.csv"), "w", newline="")
    w = csv.writer(fcsv)
    w.writerow(["ts", "host", "pids", "noise", "peak", "strong", "c",
                "reg0d", "reg13", "reg14", "loglines", "note"])
    fev = open(os.path.join(args.outdir, "events.log"), "a")

    t_end = time.time() + args.minutes * 60.0
    n = 0
    first_pid = {}
    last_lines = {}
    stats = {}
    print("soak start: %.1f min, poll %.0fs -> %s" % (
        args.minutes, args.poll, args.outdir), flush=True)

    while time.time() < t_end:
        n += 1
        for tag, ip in HOSTS:
            r = probe(ip)
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if r is None:
                w.writerow([ts, tag, "", "", "", "", "", "", "", "", "", "PROBE_FAIL"])
                print("%s %s PROBE_FAIL" % (ts, tag), flush=True)
                continue
            regs = r.get("regs") or []
            d = regs[0] if len(regs) > 0 else ""
            i13 = regs[1] if len(regs) > 1 else ""
            l = regs[2] if len(regs) > 2 else ""
            w.writerow([ts, tag, "|".join(r.get("pids") or []), r.get("noise"),
                        r.get("peak"), r.get("strong"), r.get("c"), d, i13, l,
                        r.get("loglines"), r.get("regs_err", "")])
            fcsv.flush()

            st = stats.setdefault(tag, {"n": 0, "cmin": 9e9, "cmax": -9e9,
                                        "pids": set(), "i13": {}, "d": {}})
            st["n"] += 1
            if r.get("c") is not None:
                st["cmin"] = min(st["cmin"], r["c"])
                st["cmax"] = max(st["cmax"], r["c"])
            st["pids"].update(r.get("pids") or [])
            st["i13"][i13] = st["i13"].get(i13, 0) + 1
            st["d"][d] = st["d"].get(d, 0) + 1
            if tag not in first_pid and r.get("pids"):
                first_pid[tag] = r["pids"][0]

            # 抓非常态日志行
            nl = r.get("loglines", -1)
            prev = last_lines.get(tag, 0)
            if nl and nl > prev:
                take = min(nl - prev, 40)
                tail = nb_ssh.exec_host(
                    ip, "tail -n %d /tmp/fc_guard.log" % take, tries=2, timeout=30)
                for line in (tail or "").splitlines():
                    if " | OK " in line or "STATS" in line and " | OK " in line:
                        continue
                    fev.write("[%s %s] %s\n" % (ts, tag, line.strip()))
                fev.flush()
                last_lines[tag] = nl

        if n % 10 == 0:
            for tag, _ in HOSTS:
                st = stats.get(tag)
                if not st:
                    continue
                print("  %s n=%d c=[%.2f,%.2f] pids=%s 13=%s 0d=%s" % (
                    tag, st["n"], st["cmin"], st["cmax"],
                    sorted(st["pids"]), st["i13"], st["d"]), flush=True)
        time.sleep(args.poll)

    fcsv.close()
    fev.close()
    with open(os.path.join(args.outdir, "summary.txt"), "w") as f:
        f.write("soak %s  时长 %.1f min  样本/机 %d\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            args.minutes, n))
        for tag, _ in HOSTS:
            st = stats.get(tag, {})
            f.write("%s: n=%d c=[%.2f,%.2f] pids=%s 0x13分布=%s 0x0d分布=%s\n" % (
                tag, st.get("n", 0), st.get("cmin", 0), st.get("cmax", 0),
                sorted(st.get("pids", [])), st.get("i13", {}), st.get("d", {})))
    print("soak done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
