/* tee_power.c — I/Q 功率观测器（不依赖任何解调器）
 *
 * 位置：SDR 独占者 → [tee_power] → 解调器
 *   rtl_sdr -f 1090M -s 2.4M -g 19.7 - | tee_power --out /tmp/pw.csv | stream1090 ...
 *
 * 干两件事：
 *   1) 把 stdin 的 UC8 采样原样透传到 stdout（一个字节不改）；
 *   2) 边算边把"底噪码值"按 ≤200ms 粒度写 CSV，供控制器读。
 *
 * 性能优化（2026-10-29，.84/.85 双机实测）：
 *   a) 子块边界检查外提：内层每字节 ++sub_n/cmp/branch (3 op) → 按段推进；
 *   b) 累加器 int64 → int32（子块最大 2400×255² = 1.56e8 < 2^31）；
 *   c) sps_target 乘法改 int64（ILP32 下 report_ms*sps 会回绕）。
 *   合计：吞吐 +37%（46.2→63.4 MB/s 双机一致），实时率 CPU −27%/−26%；
 *   统计语义与逐字节版本等价（切段不改累加顺序与子块切分点）。
 *
 * 为什么需要它（对照 readsb 的 60s 窗口）
 *   readsb 的 noise 是 60s 统计量（每 10s 刷新一次），控制环因此被锁在 ~60s。
 *   本程序手上是逐样本数据，能把观测延迟压到 --report-ms（默认 200ms）。
 *
 * 观测定义（与已实测口径一致）
 *   单分量 rms： c_rms = sqrt( mean(I^2 + Q^2) / 2 )       单位 = LSB
 *               noise_dbfs = 20*log10( c_rms / FS ),  FS = 2^(bits-1) = 128
 *   与 c = 128 * 10^(noise/20) 完全等价。
 *   注：矢量模口径会整体高 10*log10(2)=3.01 dB，已由 .uc8 复算确认，此处不用。
 *
 * 判据取哪一列
 *   控制器判据取 **mean_dbfs**（子块功率均值）：
 *   同码同机同时段对照，它与 readsb stats.noise 残差仅 0.14 dB —— 同口径，
 *   C_MIN/C_MAX（按 readsb noise 标定）可原样沿用。
 *   p10/p50 只输出、不作判据：分位数取"最安静子块"，与判据口径差 2–7 dB，
 *   且随空情漂（同码两分钟 4.4 dB），会同时偏移工作点与触发收敛门禁。
 *   （样本级分位则无效——低码值时大量 I=Q=0 会把低分位打穿到 -inf；
 *     子块级分位无此问题，1ms 内有 ~2400 个样本，故保留输出。）
 *
 * CSV 列： epoch_ms, p10_dbfs, p50_dbfs, mean_dbfs, peak_dbfs, clip_pct, subblocks
 * 编译： gcc -O2 -o tee_power tee_power.c -lm
 */
#define _GNU_SOURCE
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define FS 128.0

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
}

static int cmp_double(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

/* 输出一笔报告。抽出成函数是为了三处调用：墙钟触发、样本数触发、EOF 补最后一笔。 */
static void emit_report(FILE *of, const double *buf, long nsub, long cap,
                        int peak, long clip, long samples_win, double t) {
    if (nsub <= 0) return;
    long m = nsub < cap ? nsub : cap;
    double pm = 0.0;
    for (long j = 0; j < m; j++) pm += pow(10.0, buf[j] / 10.0);
    pm /= (double)m;
    double mean_db = 10.0 * log10(pm > 1e-12 ? pm : 1e-12);
    double *sorted = malloc(sizeof(double) * (size_t)m);
    if (!sorted) return;
    memcpy(sorted, buf, sizeof(double) * (size_t)m);
    qsort(sorted, (size_t)m, sizeof(double), cmp_double);
    double p10 = sorted[(long)(0.10 * (double)(m - 1))];
    double p50 = sorted[(long)(0.50 * (double)(m - 1))];
    free(sorted);
    if (peak < 1) peak = 1;                  /* 全零输入时避免 log10(0) */
    double peak_db = 20.0 * log10((double)peak / 2.0 / FS);
    double clip_pct = samples_win > 0
        ? 100.0 * (double)clip / (2.0 * (double)samples_win) : 0.0;
    fprintf(of, "%.0f,%.2f,%.2f,%.2f,%.2f,%.3f,%ld\n",
            t, p10, p50, mean_db, peak_db, clip_pct, nsub);
    fflush(of);
}

int main(int argc, char **argv) {
    const char *out_path = "/tmp/tee_power.csv";
    long sub_samples = 2400;     /* 1ms @ 2.4 Msps */
    long report_ms = 200;
    long sps = 2400000;          /* 采样率，仅用于"按样本数触发"（离线回放必需） */

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--out") && i + 1 < argc) out_path = argv[++i];
        else if (!strcmp(argv[i], "--sub-samples") && i + 1 < argc) sub_samples = atol(argv[++i]);
        else if (!strcmp(argv[i], "--report-ms") && i + 1 < argc) report_ms = atol(argv[++i]);
        else if (!strcmp(argv[i], "--sps") && i + 1 < argc) sps = atol(argv[++i]);
        else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            fprintf(stderr,
                "usage: tee_power [--out CSV] [--sub-samples N] [--report-ms MS] [--sps HZ]\n"
                "  reads UC8 I/Q on stdin, writes the SAME bytes to stdout,\n"
                "  and appends noise estimates to CSV every MS milliseconds\n"
                "  (also every MS*sps/1000 samples, so offline replay reports too).\n");
            return 0;
        }
    }

    FILE *of = fopen(out_path, "a");
    if (!of) { perror("open out"); return 1; }
    fprintf(of, "# tee_power start %.0f sub=%ld report_ms=%ld\n", now_ms(), sub_samples, report_ms);
    fflush(of);

    /* 报告窗内最多容纳的子块数（留 8 倍余量） */
    long cap = (report_ms * 2400L) / sub_samples * 8 + 64;
    double *buf = malloc(sizeof(double) * (size_t)cap);
    if (!buf) { fprintf(stderr, "oom\n"); return 1; }

    static unsigned char chunk[1 << 16];
    int32_t acc = 0;             /* sum of v² (v=2b-255, |v|≤255) in current subblock; 2400×255² = 1.56e8 < 2^31 */
    long sub_n = 0;              /* bytes in current subblock */
    long nsub = 0;               /* subblocks accumulated in this report window */
    long clip = 0, samples_win = 0;
    int peak = 0;
    double t_report = now_ms();

    for (;;) {
        ssize_t n = read(STDIN_FILENO, chunk, sizeof(chunk));
        if (n < 0) { if (errno == EINTR) continue; perror("read"); break; }
        if (n == 0) break;
        if (n & 1) { /* UC8 必须是偶数字节；出现奇数说明流错位，直接报错退出 */
            fprintf(stderr, "tee_power: odd byte count %zd, stream is not UC8\n", n);
            break;
        }
        /* 透传：原样写回 stdout */
        ssize_t off = 0;
        while (off < n) {
            ssize_t w = write(STDOUT_FILENO, chunk + off, (size_t)(n - off));
            if (w < 0) { if (errno == EINTR) continue; perror("write"); return 1; }
            off += w;
        }
        /* 统计（按段推进：段内无边界检查，段尾一次 sub_n += take；
         * 切段不改变累加顺序与子块切分点，统计语义与逐字节版本等价） */
        ssize_t k = 0;
        while (k < n) {
            long room = sub_samples - sub_n;
            ssize_t take = (n - k < room) ? (n - k) : (ssize_t)room;
            const unsigned char *p = chunk + k;
            const unsigned char *end = p + take;
            for (; p < end; p++) {
                int v = (int)*p * 2 - 255;      /* = 2*(b-127.5)，范围 -255..255 */
                int a = v < 0 ? -v : v;
                if (a > peak) peak = a;
                if (*p == 0 || *p == 255) clip++;
                acc += v * v;                   /* int32：≤ 2400×65025 = 1.56e8 */
            }
            sub_n += take;
            k += take;
            if (sub_n == sub_samples) {
                /* mean(I^2+Q^2) = acc / (4 * sub_n) */
                double p = (double)acc / (4.0 * (double)sub_n);
                if (nsub < cap) buf[nsub] = 10.0 * log10(p > 1e-12 ? p : 1e-12) - 20.0 * log10(FS);
                nsub++;
                samples_win += sub_n;
                acc = 0; sub_n = 0;
            }
        }
        double t = now_ms();
        int64_t sps_target = (int64_t)report_ms * sps / 1000;   /* int64：ILP32 下 report_ms*sps 会回绕 */
        if (nsub > 0 && ((t - t_report >= (double)report_ms) ||
                         (samples_win >= sps_target))) {
            emit_report(of, buf, nsub, cap, peak, clip, samples_win, t);
            /* 重置报告窗（peak/clip 也随窗重置，便于观察实时削波） */
            nsub = 0; samples_win = 0; clip = 0; peak = 0;
            t_report = t;
        }
    }
    emit_report(of, buf, nsub, cap, peak, clip, samples_win, now_ms());
    fclose(of);
    free(buf);
    return 0;
}
