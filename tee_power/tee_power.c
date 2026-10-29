/* tee_power.c — I/Q 功率观测器（不依赖任何解调器）
 *
 * 位置：SDR 独占者 → [tee_power] → 解调器
 *   rtl_sdr -f 1090M -s 2.4M -g 19.7 - | tee_power --out /tmp/pw.csv | stream1090 ...
 *
 * 1) stdin 的 UC8 采样原样透传到 stdout（一个字节不改）；
 * 2) 透传同时按 ≤200ms 粒度把底噪估计追加写 CSV。
 *    stdin/stdout 均为管道时走零拷贝透传（tee+splice）；
 *    任一端非管道或 TEE_POWER_NO_ZC=1 时走 read/write 循环。
 *
 * 观测定义：单分量 rms，c_rms = sqrt(mean(I²+Q²)/2)，FS = 128；
 *   noise_dbfs = 20·log10(c_rms/FS)，与守卫标度 c = 128·10^(noise/20) 等价。
 *
 * CSV 列： epoch_ms, p10_dbfs, p50_dbfs, mean_dbfs, peak_dbfs, clip_pct, subblocks
 * 编译： gcc -O2 -o tee_power tee_power.c -lm
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
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

/* ---- 统计状态（零拷贝排空与回退循环共用）。
 * 栈上结构体经指针传入，热循环可充分寄存器化。 ---- */
struct tp_stat {
    double *buf;
    long cap, sub_samples;
    long sub_n, nsub, clip, samples_win;
    int peak;
    int32_t acc;        /* sum of v² (v=2b-255, |v|≤255); 2400×255² = 1.56e8 < 2^31 */
    int64_t total_bytes;
};

__attribute__((always_inline))
static inline void process_bytes(struct tp_stat *st, const unsigned char *chunk, ssize_t n) {
    st->total_bytes += n;
    const long sub_samples = st->sub_samples;
    long sub_n = st->sub_n;
    long nsub = st->nsub;
    long clip = st->clip;
    long samples_win = st->samples_win;
    int peak = st->peak;
    int32_t acc = st->acc;
    double *buf = st->buf;
    const long cap = st->cap;
    /* 按段推进：段内无边界检查，段尾一次 sub_n += take；
     * 切段不改变累加顺序与子块切分点，统计语义与逐字节版本等价 */
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
            double pw = (double)acc / (4.0 * (double)sub_n);
            if (nsub < cap) buf[nsub] = 10.0 * log10(pw > 1e-12 ? pw : 1e-12) - 20.0 * log10(FS);
            nsub++;
            samples_win += sub_n;
            acc = 0; sub_n = 0;
        }
    }
    st->sub_n = sub_n;
    st->nsub = nsub;
    st->clip = clip;
    st->samples_win = samples_win;
    st->peak = peak;
    st->acc = acc;
}

int main(int argc, char **argv) {
    const char *out_path = "/tmp/tee_power.csv";
    long report_ms = 200;
    long sps = 2400000;          /* 采样率，仅用于"按样本数触发"（离线回放必需） */
    struct tp_stat st = {0};
    st.sub_samples = 2400;       /* 1ms @ 2.4 Msps（--sub-samples 可覆盖） */

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--out") && i + 1 < argc) out_path = argv[++i];
        else if (!strcmp(argv[i], "--sub-samples") && i + 1 < argc) st.sub_samples = atol(argv[++i]);
        else if (!strcmp(argv[i], "--report-ms") && i + 1 < argc) report_ms = atol(argv[++i]);
        else if (!strcmp(argv[i], "--sps") && i + 1 < argc) sps = atol(argv[++i]);
        else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            fprintf(stderr,
                "usage: tee_power [--out CSV] [--sub-samples N] [--report-ms MS] [--sps HZ]\n"
                "  reads UC8 I/Q on stdin, writes the SAME bytes to stdout,\n"
                "  and appends noise estimates to CSV every MS milliseconds\n"
                "  (also every MS*sps/1000 samples, so offline replay reports too).\n"
                "  zero-copy passthrough (tee+splice) is used when both stdin and\n"
                "  stdout are pipes; TEE_POWER_NO_ZC=1 forces the read/write loop.\n");
            return 0;
        }
    }

    FILE *of = fopen(out_path, "a");
    if (!of) { perror("open out"); return 1; }
    fprintf(of, "# tee_power start %.0f sub=%ld report_ms=%ld\n", now_ms(), st.sub_samples, report_ms);
    fflush(of);

    /* 报告窗内最多容纳的子块数（留 8 倍余量） */
    st.cap = (report_ms * 2400L) / st.sub_samples * 8 + 64;
    st.buf = malloc(sizeof(double) * (size_t)st.cap);
    if (!st.buf) { fprintf(stderr, "oom\n"); return 1; }

    /* 零拷贝判定：stdin/stdout 均为管道且未禁用 */
    struct stat st_in, st_out;
    int zc = 0;
    int stat_r = -1, stat_w = -1;
    int sv[2];
    if (!getenv("TEE_POWER_NO_ZC")
        && fstat(STDIN_FILENO, &st_in) == 0 && S_ISFIFO(st_in.st_mode)
        && fstat(STDOUT_FILENO, &st_out) == 0 && S_ISFIFO(st_out.st_mode)
        && pipe(sv) == 0) {
        stat_r = sv[0]; stat_w = sv[1];
        zc = 1;
    }
    fprintf(stderr, "tee_power: mode=%s\n",
            zc ? "zero-copy tee+splice" : "read/write fallback");

    static unsigned char chunk[1 << 16];
    double t_report = now_ms();
    int64_t sps_target = (int64_t)report_ms * sps / 1000;   /* int64：ILP32 下 report_ms*sps 会回绕 */

    if (zc) {
        /* ---- 零拷贝路径：tee 内核内复制到 stat 管道（不消费输入），
         *      splice 把输入零拷贝移到 stdout，统计从 stat 管道排空 ---- */
        for (;;) {
            ssize_t n = tee(STDIN_FILENO, stat_w, (size_t)1 << 20, 0);
            if (n < 0) {
                if (errno == EINTR) continue;
                perror("tee");
                break;
            }
            if (n == 0) break; /* EOF */
            size_t left = (size_t)n;
            int werr = 0;
            while (left) {
                ssize_t w = splice(STDIN_FILENO, NULL, STDOUT_FILENO, NULL, left, SPLICE_F_MOVE);
                if (w < 0) {
                    if (errno == EINTR) continue;
                    perror("splice");
                    werr = 1;
                    break;
                }
                if (w == 0) { fprintf(stderr, "tee_power: short splice at EOF\n"); werr = 1; break; }
                left -= (size_t)w;
            }
            if (werr) return 1;
            /* 统计：从 stat 管道恰好排空 n 字节 */
            size_t got = 0;
            while (got < (size_t)n) {
                ssize_t r = read(stat_r, chunk, sizeof(chunk));
                if (r < 0) {
                    if (errno == EINTR) continue;
                    perror("stat read");
                    return 1;
                }
                if (r == 0) { fprintf(stderr, "tee_power: stat pipe EOF early\n"); return 1; }
                process_bytes(&st, chunk, r);
                got += (size_t)r;
            }
            double t = now_ms();
            if (st.nsub > 0 && ((t - t_report >= (double)report_ms) ||
                                (st.samples_win >= sps_target))) {
                emit_report(of, st.buf, st.nsub, st.cap, st.peak, st.clip, st.samples_win, t);
                st.nsub = 0; st.samples_win = 0; st.clip = 0; st.peak = 0;
                t_report = t;
            }
        }
    } else {
        /* ---- 回退路径：read/write 透传 + 同一统计函数 ---- */
        for (;;) {
            ssize_t n = read(STDIN_FILENO, chunk, sizeof(chunk));
            if (n < 0) { if (errno == EINTR) continue; perror("read"); break; }
            if (n == 0) break;
            ssize_t off = 0;
            while (off < n) {
                ssize_t w = write(STDOUT_FILENO, chunk + off, (size_t)(n - off));
                if (w < 0) { if (errno == EINTR) continue; perror("write"); return 1; }
                off += w;
            }
            process_bytes(&st, chunk, n);
            double t = now_ms();
            if (st.nsub > 0 && ((t - t_report >= (double)report_ms) ||
                                (st.samples_win >= sps_target))) {
                emit_report(of, st.buf, st.nsub, st.cap, st.peak, st.clip, st.samples_win, t);
                st.nsub = 0; st.samples_win = 0; st.clip = 0; st.peak = 0;
                t_report = t;
            }
        }
    }
    /* UC8 偶数字节守卫：累计口径（tee 复制边界任意，单块奇偶不可靠） */
    if (st.total_bytes & 1)
        fprintf(stderr, "tee_power: odd total byte count %lld, stream is not UC8?\n",
                (long long)st.total_bytes);
    emit_report(of, st.buf, st.nsub, st.cap, st.peak, st.clip, st.samples_win, now_ms());
    fclose(of);
    free(st.buf);
    return 0;
}
