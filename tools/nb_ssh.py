#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nb_ssh.py — 网络对端 ADS-B 节点(84/85)的 SSH 访问单一真源。

封装要点(沿用 2026-10-07 事故教训):
  * paramiko(不用 plink)；connect 带**线程级硬超时**(自带 timeout 只管 TCP,
    半死 sshd 的 SSH 握手会无限挂)。
  * 口令走环境变量 NB2_SSH_PASS。
  * 每次操作新连接 + 重试, 不依赖长连接(systemd restart 后旧连接会断)。
  * 上传文件走 sftp.putfo(原始字节), 并强制 LF(Windows 文本写会带 CRLF,
    直接上到 Linux 会报 ambiguous redirect)。

CLI:
  python tools/nb_ssh.py exec  .85  "<cmd>"          # 单主机执行
  python tools/nb_ssh.py both  "<cmd>"               # 双机执行
  python tools/nb_ssh.py put   .85  <local> <remote> # 上传(强制 LF)
  python tools/nb_ssh.py putbin .85 <local> <remote> # 上传二进制(不转 LF)
  python tools/nb_ssh.py get   .85 <remote> <local>  # 下回文件(原始字节)
主机可用简写 .84 / .85 或完整 IP。
"""
import os, sys, time, threading, io
import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
# 节点表外置（部署侧参数不放代码里）；缺省为空表，此时只能用完整 IP
_NODES = os.path.join(HERE, "nodes.json")
try:
    import json as _json
    with open(_NODES, encoding="utf-8") as _f:
        HOSTS = _json.load(_f)["hosts"]
except Exception:
    HOSTS = {}
USER = os.environ.get("NODE_SSH_USER", "root")
PASS = os.environ.get("NODE_SSH_PASS", "")


def resolve(h):
    h = h.strip()
    # CLI 文档承诺可用简写 .84 / .85, 故去掉前导点后再查 HOSTS
    # (否则 ".85" 会被当成主机名直连而失败, 表现为 exec_host 返回 None)
    if h.lstrip(".") in HOSTS:
        return HOSTS[h.lstrip(".")]
    return h


def connect(host, timeout=20):
    """线程级硬超时 connect: 半死 sshd 也能在 timeout+5s 内返回 None。"""
    res = {}

    def worker():
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(host, username=USER, password=PASS, timeout=timeout,
                      auth_timeout=timeout, banner_timeout=timeout,
                      look_for_keys=False, allow_agent=False)
            res["c"] = c
        except Exception as e:
            res["err"] = str(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout + 5)
    if t.is_alive():
        return None
    return res.get("c")


def connect_retry(host, tries=3, timeout=20, gap=8):
    for _ in range(tries):
        c = connect(host, timeout=timeout)
        if c is not None:
            return c
        time.sleep(gap)
    return None


def run(c, cmd, timeout=60):
    if c is None:
        return None
    try:
        stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
        buf = {}

        def rd():
            try:
                buf["o"] = stdout.read().decode("utf-8", "replace")
            except Exception:
                buf["o"] = ""
            try:
                buf["e"] = stderr.read().decode("utf-8", "replace")
            except Exception:
                buf["e"] = ""

        th = threading.Thread(target=rd, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            try:
                stdout.channel.close()
            except Exception:
                pass
            return "EXEC_TIMEOUT"
        return buf.get("o", "") + buf.get("e", "")
    except Exception as e:
        return f"EXEC_ERR:{e}"


def exec_host(host, cmd, tries=3, gap=6, timeout=60):
    """按 host 中心执行命令(每次新连接+重试), 返回 stdout+stderr。"""
    last = None
    for _ in range(tries):
        c = connect(host, timeout=20)
        if c is None:
            time.sleep(gap)
            continue
        out = run(c, cmd, timeout=timeout)
        try:
            c.close()
        except Exception:
            pass
        if out is not None and not out.startswith("EXEC_ERR") and "EXEC_TIMEOUT" not in (out or ""):
            return out
        last = out
        time.sleep(gap)
    return last


def put_bytes(host, data: bytes, remote: str):
    """上传原始字节(不经本地文本转换)。"""
    c = connect_retry(host)
    if c is None:
        return False
    try:
        sftp = c.open_sftp()
        sftp.putfo(io.BytesIO(data), remote)
        sftp.close()
        return True
    except Exception as e:
        print(f"[put] ERR {e}", file=sys.stderr)
        return False
    finally:
        c.close()


def put_text(host, local: str, remote: str):
    """上传文本并强制 LF(Windows 文本写默认 CRLF 会炸远端 shell)。"""
    with open(local, "rb") as f:
        data = f.read()
    data = data.replace(b"\r\n", b"\n")
    return put_bytes(host, data, remote)


def _sftp_getfo(sftp, remote, buf):
    """在独立线程里跑 SFTP 读取, 便于外层 join 超时(参数超时不覆盖传输本身)。"""
    sftp.getfo(remote, buf)


def get_file(host, remote: str, tries: int = 3, gap: int = 6, timeout: int = 30):
    """从对端拉取文件内容(bytes)。连接失败返回 None(供调用方重试/续跑)。

    关键: sftp.getfo 本身无超时参数, 一旦传输卡死会永久阻塞 monitor 主循环 ——
    故用线程 + join(timeout) 做硬超时, 超时即放弃本次尝试(不抛异常, 由调用方续跑)。
    """
    last = None
    for _ in range(tries):
        c = connect(host, timeout=20)
        if c is None:
            time.sleep(gap)
            continue
        try:
            sftp = c.open_sftp()
            buf = io.BytesIO()
            th = threading.Thread(target=_sftp_getfo, args=(sftp, remote, buf), daemon=True)
            th.start()
            th.join(timeout + 5)
            if th.is_alive():
                # 传输超时: 放弃此次尝试, 关闭底层连接在独立线程里做(避免 c.close 也被拖死)
                try:
                    ct = threading.Thread(target=lambda: c.close(), daemon=True)
                    ct.start(); ct.join(3)
                except Exception:
                    pass
                last = TimeoutError("sftp getfo timeout (%ds)" % timeout)
                time.sleep(gap)
                continue
            return buf.getvalue()
        except Exception as e:
            last = e
            time.sleep(gap)
        finally:
            try:
                c.close()
            except Exception:
                pass
    return None if not isinstance(last, bytes) else last


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    mode = sys.argv[1]
    if mode == "exec":
        host = resolve(sys.argv[2])
        print(exec_host(host, " ".join(sys.argv[3:])))
    elif mode == "both":
        cmd = " ".join(sys.argv[2:])
        for k, ip in HOSTS.items():
            print(f"\n########## {ip} ##########")
            print(exec_host(ip, cmd))
    elif mode == "put":
        host = resolve(sys.argv[2])
        print("OK" if put_text(host, sys.argv[3], sys.argv[4]) else "FAIL")
    elif mode == "putbin":
        host = resolve(sys.argv[2])
        with open(sys.argv[3], "rb") as f:
            print("OK" if put_bytes(host, f.read(), sys.argv[4]) else "FAIL")
    elif mode == "get":
        host = resolve(sys.argv[2])
        data = get_file(host, sys.argv[3])
        if data is None:
            print("FAIL")
            return 1
        with open(sys.argv[4], "wb") as f:
            f.write(data)
        print("OK %d bytes" % len(data))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
