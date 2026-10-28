# tools — 工具索引

分三类：**离线回归**（本机跑，无节点）、**真机工具**（需 SSH 到节点）、**实验 harness**（调查用）。

## 1. 离线回归

| 文件 | 用途 | 依赖 |
|---|---|---|
| `replay_guard.py` | **主回归入口**。注入场景 + 真实轨迹回放 + I²C 传输层离线自检（假 libusb）+ 普适性 + 观测源。改判据后必跑，要求 `31/31` 全绿、`回放无误动作=是` | `data/归档轨迹/`、`fc0013_guard.py` |
| `build_state_table.py` | 从归档 1 s 轨迹统计各工作点的 `c` 分布，为阈值定标提供依据（`C_MIN = 1.3` 的来源之一） | `data/归档轨迹/` |

两者都在仓库根运行：`python tools/replay_guard.py`。

## 2. 真机工具（单节点）

| 文件 | 用途 | 典型用法 |
|---|---|---|
| `code_scan.py` | 扫 `0x13` 码 → `c_rms` 斜率，选最接近域几何中点（5.1 码）的落点。换机器/换天线后做标定 | `python3 code_scan.py <起> <止> <恢复>` |
| `code_probe.sh` | 单码工作点实证：钉码 → 等窗口 → 采样 → 恢复 | `sh code_probe.sh <node> <probe> <back>` |
| `inject_agc_test.py` | **注入验证**：第三方开 AGC 后守卫是否必然检出并写回 | `python tools/inject_agc_test.py .84 0x0c` |
| `soak_guard.py` | 长观测（本机 SSH 轮询两机）。查三件事：稳态不误动、进程不崩、越界能介入 | `python tools/soak_guard.py --minutes 90 --poll 30` |
| `soak_probe.py` | 节点侧一次性采样，输出一行 JSON，供 `soak_guard.py` 轮询（先传到节点 `/tmp`） | 由 `soak_guard.py` 调用 |
| `pipe_only.py` | **对照臂（OFF）专用**：只起 `rtl_sdr｜tee_power｜stream1090` 管道 + 转发，不碰芯片 | `python3 tools/pipe_only.py` |

## 3. 多节点访问

| 文件 | 用途 |
|---|---|
| `nb_ssh.py` | SSH 单一真源（paramiko + 线程级硬超时）。`exec` / `both` / `put` / `putbin` / `get` |
| `nodes.json` | 节点表（部署侧参数，不放代码里） |

口令走环境变量 `NODE_SSH_PASS`，不写入任何文件。

```bash
export NODE_SSH_PASS=...
python tools/nb_ssh.py both "systemctl is-active fc0013-guard"
```

## 4. 实验 harness（`bench/`，需节点）

用于重跑 `data/` 里的两类实验。**不是生产组件**，只在调查时用。

| 文件 | 对应实验 |
|---|---|
| `guard_ab_xover_readsb.py` | 2×2 交叉注入，readsb 直采拓扑（→ `data/交叉注入_readsb_20261024/` 的设计） |
| `guard_ab_xover_s1090.py` | 同上，s1090 管道拓扑 |
| `guard_ab_xover_topo_v3.py` | 四组合 × 两向背靠背（→ `data/背靠背共存开销_20261025/`） |
| `v3_decompose.py` | v3 数据的**独立复算 + 自由度分解**（不复用 harness 的 `analyze`）。SE 以此为准 |

`bench/` 里 `xover_stats.md` 的亚窗 SE 按"块内首尾点"切块（每块只覆盖 100 s 且漏掉窗口两端），
不作不确定度估计。定量结论一律取 `v3_decompose.py` 的输出（增量切块）。

## 5. 变更纪律

- 新增/删除/改名任何工具，**必须同步更新本索引**；
- 判据或协议相关的改动，`replay_guard.py` 必须保持全绿；
- 一次性调查脚本不进本目录 —— 只有**会被重复使用**的才归集到这里。
