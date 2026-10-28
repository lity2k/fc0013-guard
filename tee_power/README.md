# tee_power — SDR 管道内的 I/Q 功率观测器

生产组件。插在 SDR 独占者与解调器之间：

```
rtl_sdr -f 1090M -s 2.4M -g <gain> - | tee_power --out /tmp/tee_power.csv | stream1090 ...
```

两件事：

1. 把 stdin 的 UC8 采样**一个字节不改**透传到 stdout（必须插在 stream1090 之前）；
2. 边透传边把底噪估计按 ≤200 ms 粒度追加写 CSV，供 `fc0013_guard.py`（`--s1090` 模式）作**唯一观测源**。

## 编译 / 部署

```bash
gcc -O2 -o tee_power tee_power.c -lm      # 目标机（armhf）上编译
```

`tee_power-armhf` 为已编译的 armhf 二进制（gcc -O2，无外部依赖，仅 libc/libm），
可直接 `install.sh` 部署到 `/usr/local/bin/`。换架构或改源码后重编译即可。

## 用法

```
tee_power [--out CSV] [--sub-samples N] [--report-ms MS] [--sps HZ]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--out` | `/tmp/tee_power.csv` | 追加写；每次启动写一行 `# tee_power start ...` 头 |
| `--sub-samples` | 2400 | 1 ms 子块（@2.4 Msps），分位数的基本单元 |
| `--report-ms` | 200 | 报告窗粒度（墙钟 + 样本数双触发，离线回放也能出数） |
| `--sps` | 2400000 | 采样率，仅用于按样本数触发 |

## CSV 列

```
epoch_ms, p10_dbfs, p50_dbfs, mean_dbfs, peak_dbfs, clip_pct, subblocks
```

## 口径约定（与守卫判据的接口）

- 观测定义：单分量 RMS，`c_rms = sqrt(mean(I²+Q²)/2)`，`FS = 128`；
  `noise_dbfs = 20·log10(c_rms/FS)`，与守卫标度 `c = 128·10^(noise/20)` 完全等价
  ——即 **tee 口径的 `c` 就是真实 ADC 码值**，无需换算（readsb stats 口径须 ÷1.24）。
- **判据列是 `mean_dbfs`（第 4 列）**：子块功率均值，与 readsb `noise`
  同口径（同码同机同时段残差 0.14 dB），阈值原样沿用。
  `p10_dbfs`/`p50_dbfs` 只输出、不作守卫判据：分位数取"最安静子块"，
  与判据口径差 2–7 dB，且随空情漂（同码两分钟 4.4 dB），
  会同时偏移工作点与触发收敛门禁。
  `peak_dbfs` 在 tee 口径下未与 readsb 标定对齐 ⇒ 不参与判据（守卫日志打 `peak=n/a`）。
- 守卫侧聚合：最近 30 s 行取中位数（观测窗 30 s，比 readsb 60 s 窗收敛快）。
- 统计用**子块级**而非样本级：样本级分位会被 I=Q=0 打穿到 −inf（实测无效）；
  1 ms 子块内有 ~2400 样本，无此问题。
- 守卫每小时把 CSV 截到最近 3000 行（`S1090_TRIM_ROWS`，≈20 MB/天，防撑满 /tmp）。
