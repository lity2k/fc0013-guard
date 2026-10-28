#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_node.py — 节点侧外推算法实机验证（由编排器上传 /tmp 后 nohup 执行）。

观测源二选一（显式参数，不互回退）：
  --obs stats  读 /run/readsb/stats.json 的 noise（默认；需 readsb 在跑）
               判据口径：c = 128·10^(noise/20)，目标 5.1，域 [1.3, 20]
  --obs tee    自起 rtl_sdr | tee_power 管道读 mean_dbfs（无 readsb 的节点用）
               tee 口径 c 即真实码值（标度标定：stats c ÷1.236 = tee c），
               故目标 = 5.1×1.236 = 6.30，域 ×1.236 = [1.61, 24.72]；
               斜率 1.55 dB/码是物理量，口径无关。

流程（单机，约 4 min）：
  1. 前置：读回 0x13 原码；若 fc0013-guard 在跑则 stop（结束按原状态恢复）
  2. 钉参考码 0x10 → 等 75 s → 采 3 次取中位
  3. 外推 code_tgt = 0x10 − round(20·log10(c_ref/C_TGT)/1.55)，clamp [0x08,0x18]
  4. 钉 code_tgt → 同法采样 → c_verify
  5. 判定：c_verify ∈ 域 且 |距目标| ≤ 2 dB
  6. 无论成败：钉回原码，恢复守卫原状态，关管道
输出：阶段行实时 flush；最后一行 `RESULT {json}`。
"""
import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fc0013_guard as G                      # noqa: E402  （与主脚本同目录上传）

STATS = "/run/readsb/stats.json"
TEE_CSV = "/tmp/tee_verify.csv"
SCALE = 1.236                                # stats c ÷1.236 = 真实码值（标度标定中位）
UNIT = "fc0013-guard.service"
C_TGT, SLOPE = 5.1, 1.55
C_MIN, C_MAX = 1.3, 20.0
CODE_LO, CODE_HI = 0x08, 0x18
CODE_REF = 0x10
ALLOW_DB = 2.0


class Obs:
    """观测源：stats（readsb）或 tee（自起 rtl_sdr|tee_power）。"""
    def __init__(self, mode):
        self.mode = mode
        self.pipe = None
        if mode == "tee":
            self.pipe = subprocess.Popen(
                "rtl_sdr -f 1090000000 -s 2400000 -g 19.7 - 2>/dev/null | "
                "/tmp/tee_power --out %s" % TEE_CSV,
                shell=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
            time.sleep(2)

    def read(self):
        if self.mode == "stats":
            try:
                lo = json.load(open(STATS))["last1min"]["local"]
                return lo.get("noise")
            except Exception:
                return None
        # tee：读 CSV 尾部 30 s 的 mean_dbfs 中位
        try:
            rows = [r for r in open(TEE_CSV, errors="replace")
                    if not r.startswith("#")]
            tail = [r for r in rows[-160:] if r.count(",") >= 6]
            # 列: epoch_ms, p10, p50, mean_dbfs, peak, clip, subblocks
            vals = [float(r.split(",")[3]) for r in tail]
            return statistics.median(vals) if vals else None
        except Exception:
            return None

    def stop(self):
        if self.pipe:
            try:
                os.killpg(os.getpgid(self.pipe.pid), signal.SIGTERM)
            except Exception:
                pass
            self.pipe = None


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def sample_median(obs, n=3, gap=15):
    vals = []
    for i in range(n):
        v = obs.read()
        log("  sample[%d/%d] reading=%s" % (i + 1, n, v))
        if v is not None:
            vals.append(v)
        if i < n - 1:
            time.sleep(gap)
    return statistics.median(vals) if vals else None


def pin(fc, code, tag):
    ok, out = fc.set_code(code)
    r = fc.read_013()
    back = r[1] & 0x1F if r else None
    log("PIN %s -> 0x%02x ok=%d read_back=%s" % (tag, code, ok, back))
    return ok and back == (code & 0x1F)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", choices=("stats", "tee"), default="stats",
                    help="观测源：stats=readsb stats.json；tee=自起 rtl_sdr|tee_power")
    ap.add_argument("--code-ref", type=lambda x: int(x, 0), default=CODE_REF,
                    help="参考码（默认 0x10）。若该码已接近目标，换一个明显偏离的码"
                         "才有外推跨度")
    args = ap.parse_args()
    obs_mode = args.obs
    code_ref = args.code_ref & 0x1F
    # tee 口径即真实码值 ⇒ 目标/域按标度比换算（stats c ÷1.236 = tee c）
    c_tgt = C_TGT * SCALE if obs_mode == "tee" else C_TGT
    c_min = C_MIN * SCALE if obs_mode == "tee" else C_MIN
    c_max = C_MAX * SCALE if obs_mode == "tee" else C_MAX
    out = {"node": os.uname().nodename, "obs": obs_mode,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    guard_was = "unknown"
    try:
        guard_was = "active" if os.system(
            "systemctl is-active --quiet %s" % UNIT) == 0 else "inactive"
    except Exception:
        guard_was = "no-unit"
    out["guard_was"] = guard_was
    if guard_was == "active":
        os.system("systemctl stop %s" % UNIT)
        log("守卫已停（结束恢复）")

    obs = Obs(obs_mode)
    try:
        fc = G.Fc()
    except OSError as e:
        print("RESULT %s" % json.dumps(dict(out, verdict="FAIL",
              reason="channel:%s" % e)), flush=True)
        return 1

    r0 = fc.read_013()
    orig = (r0[1] & 0x1F) if r0 else None
    log("原码=%s regs0=%s" % (hex(orig) if orig is not None else "??", r0))
    out["orig_code"] = ("0x%02x" % orig) if orig is not None else None

    try:
        if not pin(fc, code_ref, "ref"):
            print("RESULT %s" % json.dumps(dict(out, verdict="FAIL",
                  reason="pin_ref_failed")), flush=True)
            return 1
        log("等 75 s（60 s 滑动窗收敛）")
        time.sleep(75)
        n_ref = sample_median(obs)
        if n_ref is None:
            print("RESULT %s" % json.dumps(dict(out, verdict="FAIL",
                  reason="观测源不可读（readsb 未在跑 / tee 无数据）")), flush=True)
            return 1
        c_ref = G.c_rms(n_ref)
        delta = round(20.0 * __import__("math").log10(c_ref / c_tgt) / SLOPE)
        code_tgt = max(CODE_LO, min(CODE_HI, code_ref - delta))
        log("c_ref=%.2f (reading=%.1f) 目标c=%.2f → 外推 delta=%+d → code_tgt=0x%02x"
            % (c_ref, n_ref, c_tgt, delta, code_tgt))
        out.update(c_ref=round(c_ref, 3), reading_ref=n_ref, code_ref="0x%02x" % code_ref,
                   code_tgt="0x%02x" % code_tgt, delta=delta)

        if not pin(fc, code_tgt, "tgt"):
            print("RESULT %s" % json.dumps(dict(out, verdict="FAIL",
                  reason="pin_tgt_failed")), flush=True)
            return 1
        log("等 75 s")
        time.sleep(75)
        n_ver = sample_median(obs)
        if n_ver is None:
            print("RESULT %s" % json.dumps(dict(out, verdict="FAIL",
                  reason="观测源不可读@verify")), flush=True)
            return 1
        c_ver = G.c_rms(n_ver)
        import math
        dev = 20.0 * math.log10(c_ver / c_tgt)
        in_dom = c_min <= c_ver <= c_max
        verdict = "PASS" if (in_dom and abs(dev) <= ALLOW_DB) else \
                  "PARTIAL" if in_dom else "FAIL"
        log("c_verify=%.2f (reading=%.1f) 域内=%s 距目标=%+.1f dB（允差±%.0f）→ %s"
            % (c_ver, n_ver, in_dom, dev, ALLOW_DB, verdict))
        out.update(c_verify=round(c_ver, 3), reading_verify=n_ver,
                   dev_db=round(dev, 2), in_domain=in_dom, verdict=verdict)
        print("RESULT %s" % json.dumps(out), flush=True)
        return 0
    finally:
        # 顺序要紧：tee 模式的 rtl_sdr 负责 detach 内核 DVB 驱动，
        # 先关管道会让裸 libusb 打不开设备 ⇒ 恢复码必须先做
        if orig is not None:
            pin(fc, orig, "restore")
        obs.stop()
        if guard_was == "active":
            os.system("systemctl start %s" % UNIT)
            log("守卫已恢复")


if __name__ == "__main__":
    sys.exit(main())
