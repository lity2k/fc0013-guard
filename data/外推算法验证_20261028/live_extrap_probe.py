#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_extrap_probe.py — 外推算法实机验证（节点端，单机自包含）。

流程（全程本机完成，观测源 = readsb stats.json，守卫标度）：
  1. 读回当前 0x13（原码，用于收尾恢复）；
  2. 钉参考码 0x10 → 等 75 s（readsb 60 s 窗收敛）→ 采 noise ×3（隔 15 s）→ c_ref；
  3. 外推 code_tgt = code_ref − round(20·log10(c_ref/5.1)/1.55)，clamp [0x08,0x18]；
  4. 钉 code_tgt → 等 75 s → 采 noise ×3 → c_verify；
  5. 验证判定：c_verify ∈ [1.3,20] 且 |20log10(c_verify/5.1)| ≤ 2 dB ⇒ PASS；
     否则用 c_verify 第二轮外推（至多 2 轮）；
  6. 恢复原码，输出一行 JSON。

⚠ 调用方必须先停守卫（本脚本不处理服务）。
"""
import json
import math
import statistics
import sys
import time

sys.path.insert(0, "/tmp")
import fc0013_guard as G                                     # noqa: E402

STATS = "/run/readsb/stats.json"
C_TGT, SLOPE = 5.1, 1.55
C_MIN, C_MAX = 1.3, 20.0
CODE_LO, CODE_HI = 0x08, 0x18
CODE_REF = 0x10
SETTLE = 75
GAP = 15
ALLOW_DB = 2.0


def read_noise():
    try:
        lo = json.load(open(STATS))["last1min"]["local"]
        return lo.get("noise")
    except Exception:
        return None


def sample(n=3):
    out = []
    for _ in range(n):
        v = read_noise()
        if v is not None:
            out.append(v)
        time.sleep(GAP)
    return out


def c_of(noise):
    return 128.0 * (10.0 ** (noise / 20.0))


def extrapolate(code_ref, c_ref):
    delta = round(20.0 * math.log10(c_ref / C_TGT) / SLOPE)
    return max(CODE_LO, min(CODE_HI, code_ref - delta))


def pin(fc, code):
    ok, out = fc.set_code(code)
    r = fc.read_013()
    return ok, (r[1] & 0x1F) if r else None, out


def main():
    fc = G.Fc()
    orig = fc.read_013()
    orig_code = (orig[1] & 0x1F) if orig else None
    result = {"node": G.__name__ and sys.argv[1] if len(sys.argv) > 1 else "",
              "orig_code": ("0x%02x" % orig_code) if orig_code is not None else None,
              "rounds": []}

    code_ref, c_ref = CODE_REF, None
    for rnd in (1, 2):
        ok, readback, out = pin(fc, code_ref)
        print("R%d pin ref=0x%02x ok=%s readback=%s %s" % (rnd, code_ref, ok, readback, out), flush=True)
        if not ok or readback != (code_ref & 0x1F):
            result["rounds"].append({"round": rnd, "error": "pin ref fail"})
            break
        ns = sample()
        cs = [c_of(x) for x in ns]
        c_ref = statistics.median(cs)
        tgt = extrapolate(code_ref, c_ref)
        result["rounds"].append({"round": rnd, "ref_code": "0x%02x" % code_ref,
                                 "noise": ns, "c_ref": round(c_ref, 2),
                                 "target": "0x%02x" % tgt})
        print("R%d ref noise=%s c_ref=%.2f -> tgt=0x%02x" % (rnd, ns, c_ref, tgt), flush=True)

        # 钉目标码并验证
        ok, readback, out = pin(fc, tgt)
        print("R%d pin tgt=0x%02x ok=%s readback=%s %s" % (rnd, tgt, ok, readback, out), flush=True)
        if not ok or readback != tgt:
            result["rounds"][-1].update({"error": "pin tgt fail"})
            break
        ns2 = sample()
        cs2 = [c_of(x) for x in ns2]
        c_ver = statistics.median(cs2)
        dev = 20.0 * math.log10(c_ver / C_TGT)
        passed = C_MIN <= c_ver <= C_MAX and abs(dev) <= ALLOW_DB
        result["rounds"][-1].update({"verify_code": "0x%02x" % tgt, "noise2": ns2,
                                     "c_verify": round(c_ver, 2),
                                     "dev_db": round(dev, 2),
                                     "pass": passed})
        print("R%d verify c=%.2f dev=%+.1f dB %s" % (rnd, c_ver, dev,
                                                     "PASS" if passed else "RETRY"), flush=True)
        if passed:
            result["result"] = "PASS"
            break
        if not (C_MIN <= c_ver <= C_MAX) or abs(dev) > 6.0:
            result["result"] = "ANOMALY"      # 严重偏离，外推第二意义不大
            break
        code_ref = tgt                        # 第二轮以本轮目标码为新参考
    else:
        result["result"] = "FAIL"

    # 恢复原码
    if orig_code is not None:
        ok, readback, out = pin(fc, orig_code)
        result["restore"] = {"code": result["orig_code"], "ok": ok}
        print("restore 0x%02x ok=%s %s" % (orig_code, ok, out), flush=True)

    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
