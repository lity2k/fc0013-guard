#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_install.py — install.sh 真机验收（reboot → 安装 → 核验 → 对齐保存 → 卸载 → 恢复）

用法（口令走环境变量，不入库）：
  export NODE_SSH_PASS=...
  python tools/verify_install.py                                  # 三机全量
  python tools/verify_install.py --nodes .84,.85                  # 指定机
  python tools/verify_install.py --from P6                        # 从某阶段续跑
  python tools/verify_install.py --keep                           # 末尾不恢复（停在生产态之外）
  python tools/verify_install.py --modemap .84=readsb,.86=s1090   # 覆盖形态

阶段：
  P1  传包到 /tmp/fcg（install.sh、主脚本、tee_power 源与预编译件、读寄存器探针）
  P2  reboot + 等回线（/proc/uptime 证明是新启动）
  P3  reboot 后基线：readsb active、无守卫残留、芯片 regs
  P4  install.sh --dry-run：只打印，系统无变化
  P5  install.sh 实装
  P6  落位核验：unit 内容 / is-enabled / is-active / 文件权限 / 日志 START / conf 未生成
  P7  --pin 对齐 ⇒ conf 生成 ⇒ 重启 ⇒ 日志 cal_src=已保存对齐码
  P8  reboot ⇒ 自启 ⇒ conf 自动写回（芯片回默认态时）
  P9  删 conf ⇒ 重启 ⇒ cal_src=启动读回 0x13
  P10 --uninstall ⇒ unit/脚本/conf 全清，芯片 0x0d 仍 09
  P11 恢复：重装 + --pin deployed.json 该机码（--keep 时不跑）
"""
import argparse
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import nb_ssh  # noqa: E402

PKG = "/tmp/fcg"
CONF = "/etc/fc0013-guard.conf"
UNIT = "/etc/systemd/system/fc0013-guard.service"
FILES = ["install.sh", "fc0013_guard.py",
         "tee_power/tee_power.c", "tee_power/tee_power-armhf"]
SET_PY = """import sys, importlib.util
spec = importlib.util.spec_from_file_location('G', '@PKG@/fc0013_guard.py')
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)
code = int(sys.argv[1], 0) & 0x1F
print('SET ' + str(G.Fc().set_code(code)))
""".replace("@PKG@", PKG)
REGS_PY = """import importlib.util
spec = importlib.util.spec_from_file_location('G', '%s/fc0013_guard.py')
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)
try:
    r = G.Fc().read_013()
    print('REGS ' + ('none' if r is None else ' '.join('0x%%02x' %% x for x in r)))
except Exception as e:
    print('REGS ERR %%s %%s' %% (type(e).__name__, e))
""" % PKG

RES = {}
LOCK = threading.Lock()


def rec(node, phase, ok, detail):
    with LOCK:
        RES.setdefault(node, {})[phase] = (bool(ok), detail.strip()[:400])
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", node, detail.strip()[:300]))


def sh(ip, cmd, timeout=90):
    out = nb_ssh.exec_host(ip, cmd, tries=2, gap=5, timeout=timeout)
    return "" if out is None else out


def wait_up(ip, budget=420):
    """reboot 后等回线：/proc/uptime 可读且 < budget 秒 ⇒ 本次新启动。"""
    for _ in range(60):
        out = sh(ip, "cat /proc/uptime", timeout=15)
        try:
            up = float(out.split()[0])
        except Exception:
            up = 1e9
        if up < budget:
            return True, up
        time.sleep(5)
    return False, -1.0


def regs(ip):
    """读芯片 (0x0d, 0x13, 0x14)，返回 '0x09 0x0c 0x..' 或 ''。"""
    out = sh(ip, "python3 %s/_regs.py" % PKG, timeout=60)
    for line in out.splitlines():
        if line.startswith("REGS ") and "none" not in line and "ERR" not in line:
            return line[5:].strip().replace("\r", "")
    return ""


def upload(ip):
    """传包（幂等）。reboot 会清空 /tmp ⇒ 每个用到包的阶段前都要补一次。"""
    ok = True
    sh(ip, "mkdir -p %s/tee_power" % PKG)
    for rel in FILES:      # 保持发布目录结构（install.sh 按 $HERE/tee_power/... 找件）
        src = os.path.join(ROOT, rel)
        if not os.path.exists(src):
            return False
        ok &= nb_ssh.put_bytes(ip, open(src, "rb").read(), "%s/%s" % (PKG, rel))
    ok &= nb_ssh.put_bytes(ip, REGS_PY.encode(), "%s/_regs.py" % PKG)
    ok &= nb_ssh.put_bytes(ip, SET_PY.encode(), "%s/_setcode.py" % PKG)
    sh(ip, "chmod +x %s/install.sh %s/tee_power/tee_power-armhf" % (PKG, PKG))
    n = sh(ip, "ls %s/install.sh %s/fc0013_guard.py %s/tee_power/tee_power.c "
                "%s/tee_power/tee_power-armhf %s/_regs.py 2>/dev/null | wc -l"
                % (PKG, PKG, PKG, PKG, PKG)).strip()
    return ok and n == "5"


def phase_upload(ip, node, mode):
    rec(node, "P1", upload(ip), "传包 %s（reboot 后需补传）" % PKG)


def phase_reboot(ip, node, mode):
    sh(ip, "reboot", timeout=10)
    time.sleep(10)
    ok, up = wait_up(ip)
    rec(node, "P2", ok, "回线 uptime=%.0fs" % up)


def phase_baseline(ip, node, mode):
    upload(ip)          # reboot 清了 /tmp ⇒ 取芯片状态前先把探针放回去
    act = sh(ip, "systemctl is-active readsb").strip()
    unit = sh(ip, "ls %s 2>&1" % UNIT).strip()
    proc = sh(ip, "pgrep -af 'fc0013_gua[r]d' | wc -l").strip()
    conf = sh(ip, "ls %s 2>&1" % CONF).strip()
    r = regs(ip)
    ok = act == "active" and "No such" in unit and proc == "0" and "No such" in conf
    rec(node, "P3", ok, "readsb=%s unit=%s proc=%s conf=%s regs=[%s]"
        % (act, unit, proc, conf, r))


def phase_dry(ip, node, mode):
    upload(ip)
    out = sh(ip, "cd %s && bash install.sh --mode %s --dry-run" % (PKG, mode), timeout=120)
    unit = sh(ip, "ls %s 2>&1" % UNIT).strip()
    ok = "[dry-run]" in out and "No such" in unit
    rec(node, "P4", ok, "dry-run 打印=%d 行，unit 未落位=%s"
        % (len(out.splitlines()), "No such" in unit))


def phase_install(ip, node, mode):
    upload(ip)
    out = sh(ip, "cd %s && bash install.sh --mode %s" % (PKG, mode), timeout=180)
    rec(node, "P5", "完成" in out or "已安装并启动" in out,
        out.strip().splitlines()[-1] if out.strip() else "无输出")


def phase_placed(ip, node, mode):
    en = sh(ip, "systemctl is-enabled fc0013-guard").strip()
    act = sh(ip, "systemctl is-active fc0013-guard").strip()
    perm = sh(ip, "stat -c '%a %n' /usr/local/bin/fc0013_guard.py "
                 "/usr/local/bin/tee_power 2>&1").strip().replace("\n", " | ")
    u = sh(ip, "cat %s 2>&1" % UNIT)
    time.sleep(20)
    log = sh(ip, "grep -a 'START' /tmp/fc_guard.log | tail -1").strip()
    conf = sh(ip, "ls %s 2>&1" % CONF).strip()
    want = ["After=readsb.service", "BindsTo=readsb.service", "Restart=on-failure",
            "WantedBy=multi-user.target"]
    miss = [w for w in want if w not in u]
    ok = (en == "enabled" and act == "active" and not miss
          and "755 /usr/local/bin/fc0013_guard.py" in perm
          and "START" in log and "No such" in conf)
    rec(node, "P6", ok, "enabled=%s active=%s unit缺=%s perm=%s conf=%s | %s"
        % (en, act, miss or "无", perm, conf, log[-120:]))


def phase_pin(ip, node, mode, code):
    sh(ip, "systemctl stop fc0013-guard")
    out = sh(ip, "python3 /usr/local/bin/fc0013_guard.py --pin %s" % code, timeout=60)
    conf = sh(ip, "cat %s 2>&1" % CONF).strip()
    sh(ip, "systemctl start fc0013-guard")
    time.sleep(25)
    log = sh(ip, "grep -a 'START' /tmp/fc_guard.log | tail -1").strip()
    ok = ("已保存为起点码" in out and code in conf
          and ("code=%02x" % int(code, 0)) in log.replace("0x", "")
          and "已保存对齐码" in log)
    rec(node, "P7", ok, "pin=%s conf=%s | %s"
        % (out.strip()[-60:], conf[:80], log[-120:]))


def phase_writback(ip, node, mode, code):
    """等价“芯片回默认态”：把芯片改成别的码，重启服务，看是否按 conf 写回。

    不用 reboot 造这个场景：本机根 fs 是临时叠加层，reboot 会把安装本身也清掉
    （实验主机特性，非部署常态）⇒ 只能就地改码验证写回逻辑。
    """
    upload(ip)
    sh(ip, "systemctl stop fc0013-guard")
    other = "0x%02x" % (int(code, 0) + 2)
    sh(ip, "python3 %s/_setcode.py %s" % (PKG, other), timeout=60)
    r0 = regs(ip)
    sh(ip, "systemctl start fc0013-guard")
    time.sleep(25)
    log = sh(ip, "grep -a 'START\\|ALIGN' /tmp/fc_guard.log | tail -2").strip()
    r1 = regs(ip)
    ok = (r0.split()[1] == other and r1.split()[1] == code
          and "已保存对齐码" in log)
    rec(node, "P8", ok, "改码 %s regs=[%s] → 启服务后 regs=[%s] | %s"
        % (other, r0, r1, log.replace("\n", " | ")[-160:]))


def phase_forget(ip, node, mode):
    sh(ip, "rm -f %s" % CONF)
    conf = sh(ip, "ls %s 2>&1" % CONF).strip()
    sh(ip, "systemctl restart fc0013-guard")
    time.sleep(25)
    log = sh(ip, "grep -a 'START' /tmp/fc_guard.log | tail -1").strip()
    ok = "No such" in conf and "启动读回" in log
    rec(node, "P9", ok, "conf 已删=%s | %s" % (conf, log[-120:]))


def phase_uninstall(ip, node, mode):
    upload(ip)
    before = regs(ip)
    out = sh(ip, "cd %s && bash install.sh --uninstall" % PKG, timeout=120)
    miss = []
    for p in (UNIT, "/usr/local/bin/fc0013_guard.py",
              "/usr/local/bin/tee_power", CONF):
        if "No such" not in sh(ip, "ls %s 2>&1" % p):
            miss.append(p)
    en = sh(ip, "systemctl is-enabled fc0013-guard 2>&1").strip()
    after = regs(ip)
    ok = not miss and "enabled" not in en and (not before or before == after)
    rec(node, "P10", ok, "残留=%s is-enabled=%s regs %s -> %s"
        % (miss or "无", en, before, after))


def phase_restore(ip, node, mode, code):
    upload(ip)
    out = sh(ip, "cd %s && bash install.sh --mode %s" % (PKG, mode), timeout=180)
    if code:
        sh(ip, "systemctl stop fc0013-guard")
        p = sh(ip, "python3 /usr/local/bin/fc0013_guard.py --pin %s" % code, timeout=60)
        sh(ip, "systemctl start fc0013-guard")
    else:
        p = "无部署码，未 pin"
    time.sleep(20)
    act = sh(ip, "systemctl is-active fc0013-guard").strip()
    log = sh(ip, "grep -a 'START' /tmp/fc_guard.log | tail -1").strip()
    ok = act == "active" and "START" in log
    rec(node, "P11", ok, "active=%s pin=%s | %s" % (act, p.strip()[-40:], log[-110:]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", default=".84,.85,.86")
    ap.add_argument("--from", dest="from_p", default="P1")
    ap.add_argument("--only", default="", help="只跑指定阶段，如 P8,P9")
    ap.add_argument("--keep", action="store_true", help="末尾不恢复（留在卸载态）")
    ap.add_argument("--modemap", default=".84=readsb,.85=readsb,.86=s1090")
    args = ap.parse_args()

    nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
    modes = dict(kv.split("=") for kv in args.modemap.split(",") if kv.strip())
    dep = {}
    try:
        dep = json.load(open(os.path.join(ROOT, "deployed.json")))["hosts"]
    except Exception:
        pass

    seq = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P11"]
    todo = ([p for p in seq if p in args.only.split(",")] if args.only
            else seq[seq.index(args.from_p):])

    for ph in todo:
        if ph == "P11" and args.keep:
            continue
        print("\n===== %s =====" % ph)
        ths = []
        for n in nodes:
            ip = nb_ssh.resolve(n)
            mode = modes.get(n, "readsb")
            code = dep.get(n, {}).get("code", "")
            cc = code or "0x0c"
            fn = {"P1": lambda i, x, m=mode: phase_upload(i, x, m),
                  "P2": lambda i, x, m=mode: phase_reboot(i, x, m),
                  "P3": lambda i, x, m=mode: phase_baseline(i, x, m),
                  "P4": lambda i, x, m=mode: phase_dry(i, x, m),
                  "P5": lambda i, x, m=mode: phase_install(i, x, m),
                  "P6": lambda i, x, m=mode: phase_placed(i, x, m),
                  "P7": lambda i, x, m=mode, c=cc: phase_pin(i, x, m, c),
                  "P8": lambda i, x, m=mode, c=cc: phase_writback(i, x, m, c),
                  "P9": lambda i, x, m=mode: phase_forget(i, x, m),
                  "P10": lambda i, x, m=mode: phase_uninstall(i, x, m),
                  "P11": lambda i, x, m=mode, c=code: phase_restore(i, x, m, c)}[ph]
            t = threading.Thread(target=fn, args=(ip, n), daemon=True)
            t.start()
            ths.append(t)
        for t in ths:
            t.join(900)

    print("\n===== 汇总 =====")
    for n in nodes:
        r = RES.get(n, {})
        bad = [k for k, v in r.items() if not v[0]]
        print("%s: %d/%d PASS %s" % (n, len(r) - len(bad), len(r),
                                     ("✗ " + ",".join(bad)) if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
