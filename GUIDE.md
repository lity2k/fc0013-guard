# GUIDE.md — 权威行动指南

**只记做法。行动前先读本文件。**
失效做法立即删除；新做法经真机验证后按同样简洁方式补入。

---

## 0. 普适性（改代码前必读）

- 脚本内**不得**出现任何与特定机器、天线、标定结果绑定的取值；不得以"未知机器"拒绝启动。
- 起点码解析顺序：显式 `--cal` > 启动读一次 `0x13` > 默认码 `0x0E`。
- 逐机取值只放 `deployed.json` / `tools/nodes.json`，代码与 unit 模板里不出现。

## 1. 安装 / 卸载（install.sh，唯一安装入口）

```bash
# 发布目录整体传到节点后，在节点上执行：
sudo bash install.sh                     # 默认 --mode readsb
sudo bash install.sh --mode s1090        # stream1090 管道拓扑
sudo bash install.sh --uninstall         # 卸载：停服务、删 unit 与两件
```

- 安装器做四件事：前置检查 → 部署 tee_power（预编译优先，`--build` 现场编译）→
  部署主脚本到 `/usr/local/bin` → 写 unit + `enable --now`。
- 主脚本不含安装逻辑；不装 unit 的降级方案：直接前台跑 `python3 fc0013_guard.py`。
- 锁被占用时打印 `LOCK 已有实例在跑，退出` 并正常退出(0)，
  unit 必须 `Restart=on-failure`，否则会反复重启。

## 2. 开机 / 重启后重新拉起（必做）

根 fs 是 tmpfs overlay ⇒ **重启后 `/tmp` 与 unit 全丢**，守卫不会自启。

```bash
sudo bash install.sh
ssh root@<节点> 'python3 /usr/local/bin/fc0013_guard.py --pin 0x0c'   # 对齐部署起点码
```

- 起点码取 `deployed.json` 里该机的值，**显式给**，不靠默认。
- 校验通过标志：`/tmp/fc_guard.log` 末行 `OK c=… code=…`，且回读 `0x0d=09`。
- 只重启 readsb（未重启机器）时也要重跑一次：readsb 启动会 reset tuner 冲掉钉码。
- 守卫在域内**不会**主动钉码，所以开机这一步不能省。

## 3. 常用命令

```bash
python3 /tmp/fc0013_guard.py --once                        # 干跑一轮，只读观测
python3 /tmp/fc0013_guard.py --interval 15 --log /tmp/fc_guard.log   # 前台常驻（调试用；unit 用默认值即可）
python3 /tmp/fc0013_guard.py --selftest 20                 # I2C 通道自检（真写 0x13，生产低峰做）
python3 /tmp/fc0013_guard.py --pin 0x0c                    # 无条件钉码（关 AGC + 钉 0x13）
python tools/replay_guard.py                               # 离线回归，改判据后必跑
python tools/soak_guard.py --minutes 90 --poll 30          # 长观测（先传 tools/soak_probe.py 到 /tmp）
python tools/inject_agc_test.py .84 0x0c                   # 真机注入 AGC，验证必被检出
```

## 4. 写芯片（脚本自身完成，不经外部二进制）

- 单次控制传输必须传 `[reg, val]` **两个字节**，`wIndex=0x0610`。拆成两次 ⇒ USB 成功但 I2C 静默失败。
- 严格顺序：**先 `0x0d=09`（关 AGC），后 `0x13=CODE`**。
- 每步**写后 20 ms 读回校验**，不符重试 ≤ 4 次。
- `0x13` 连读 2–3 次取稳定值，首读可能是上一状态。
- 句柄**每事务开关**（只常驻 libusb context）；跨进程用 `/tmp/fc_daemon.lock` 串行化。
- `0x0c` 绝不可写 `0xF8`/`0xF9`（bit2=0）⇒ 60 s 内 noise 掉 24 dB、报文归零，只能物理拔插恢复。
- 与 `fc_daemon watch` **不可同时跑**。

## 5. 观测源（`--mode`，严格单源、互不回退）

| `--mode` | 观测源 | 日志标记 |
|---|---|---|
| `readsb`（默认） | `stats.json` 的 `last1min.local.noise` / `peak_signal` | `src=stats` |
| `s1090` | 管道内 `tee_power` CSV 的 `mean_dbfs`（最近 30 s 中位数） | `src=tee` |

- `--mode s1090` 取 **`mean_dbfs`**，不取 `p50_dbfs`（后者低 2–7 dB 且随空情漂）。
- stats 读不到就报"不可读"跳过本轮，**不回退**到别的源。
- `tee_power` 必须插在 `stream1090` 之前（它透传 UC8 不改字节）；stream1090 输出 AVR 文本不带 RSSI。
- tee CSV 由守卫自己原地截断同一 inode（不用 `os.replace`，否则观测值冻结）。
- 只起管道不管芯片（对照臂）：`python3 tools/pipe_only.py`。

## 6. 标定（换机器 / 换天线后必须重做）

1. `python tools/code_scan.py 0x0e 0x10 0x12 0x0c` —— 依次钉给定码（可给任意多个），
   每码等 70 s 让 readsb 窗口收敛后采 3 次；看 `c=` 落在哪。
2. 选 `c` 最接近域几何中点（5.1 码）的码；
3. 写入 `deployed.json`，并同步开机 `--pin <码>`。

- 码**跨机不可移植**：同码跨件差 2–3 dB。
- `deployed.json` 的码是**部署起点，不是当前值** —— 守卫会按工况自行升降，不要拿它当现状读。

## 7. 排障顺序

```bash
tail -5 /tmp/fc_guard.log          # 看 src= / c= / fc0013= 三字段
journalctl -u fc0013-guard -n 50   # 看进程是否反复重启
```
1. `src=` 与部署形态不符 ⇒ 改 `--mode`，不要指望回退。
2. 每行 `fc0013=未访问` 且 `c` 在 [1.3, 20] ⇒ **正常**，不是"没干活"。
3. `c` 长期越界但无 `ACT` ⇒ 检查收敛门禁（近 5 样本极差 ≤ 3 dB、上次写入后 ≥ 70 s）。
4. 写入反复失败 ⇒ `--selftest 20`；`ok < 20` 说明 I2C 链路有问题，先查 `0x13` 是否连读取稳。

## 8. systemd unit 与部署侧工具

unit 由 `install.sh` 生成并写入 `/etc/systemd/system/fc0013-guard.service`，
带 `BindsTo=readsb.service` + `After=`（缺了它，钉码会被 readsb 的 reset tuner 静默冲掉）。

```bash
systemctl is-active fc0013-guard
systemctl restart fc0013-guard
journalctl -u fc0013-guard -n 50
```

多节点批量操作用 `tools/nb_ssh.py`（节点表在 `tools/nodes.json`，口令走 `NODE_SSH_PASS`）：

```bash
python tools/nb_ssh.py exec .85 "tail -3 /tmp/fc_guard.log"
python tools/nb_ssh.py both  "systemctl is-active fc0013-guard"
python tools/nb_ssh.py put   .85 fc0013_guard.py /tmp/fc0013_guard.py
```

- 停守卫时 **kill 与 rm 必须分两次 exec**：`pgrep -f 'fc0013_gua[r]d'` 会匹配到
  `rm /tmp/fc0013_guard.lock` 这条命令自身（路径含子串），放一起会自杀且锁删不掉。
- 日志归档为 `fc_guard.prev.log`，不要删。

## 9. 改代码后的回归门禁

1. `python tools/replay_guard.py` 全绿；
2. 真机 `--selftest 20` 得 `ok=20 fail=0 first_try=20`；
3. 至少一台机器跑满 90 min 长观测，确认稳态 `fc0013=未访问`、无谓写 0。
