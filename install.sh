#!/bin/bash
# install.sh — 在目标节点上安装 fc0013_guard 全套组件。
#
# 组件与落位：
#   fc0013_guard.py            -> /usr/local/bin/fc0013_guard.py
#   tee_power（预编译优先）     -> /usr/local/bin/tee_power
#   fc0013-guard.service       -> /etc/systemd/system/（unit 绑 readsb 生命周期）
#
# 用法（在含本文件的目录里执行）：
#   sudo bash install.sh                 # 默认 --mode readsb，用预编译 tee_power
#   sudo bash install.sh --mode s1090    # stream1090 管道拓扑（unit 带该参数）
#   sudo bash install.sh --build         # 不用预编译，现场编译 tee_power.c
#   sudo bash install.sh --uninstall     # 卸载：停服务 + 删 unit + 删两件
#   sudo bash install.sh --dry-run       # 只打印动作，不改系统
#
# 前置（脚本自检）：readsb 在跑、python3 与 libusb-1.0 存在；
# s1090 模式额外要求 rtl_sdr 与 stream1090 可执行（tee_power 由本脚本负责）。
#
# 退出码：0 成功；1 运行失败；2 用法/前置不满足。
#
# 重启后（根 fs 非持久的节点）：重跑本脚本，再手工对齐起点码：
#   python3 /usr/local/bin/fc0013_guard.py --pin <deployed.json 里该机的码>
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX=/usr/local
MODE=readsb
ACTION=install
BUILD=0
DRY=0

GUARD=$HERE/fc0013_guard.py
TEE_SRC=$HERE/tee_power/tee_power.c
TEE_PRE=$HERE/tee_power/tee_power-armhf
UNIT_NAME=fc0013-guard.service
UNIT_PATH=/etc/systemd/system/$UNIT_NAME

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }

for arg in "$@"; do
    case "$arg" in
        --mode)      MODE_PENDING=1 ;;
        --mode=*)    MODE=${arg#*=} ;;
        --readsb)    MODE=readsb ;;
        --s1090)     MODE=s1090 ;;
        --build)     BUILD=1 ;;
        --uninstall) ACTION=uninstall ;;
        --dry-run)   DRY=1 ;;
        -h|--help)   sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)           [ -n "${MODE_PENDING:-}" ] && { MODE=$arg; MODE_PENDING=; } \
                     || die "未知参数：$arg（见 --help）" 2 ;;
    esac
done
[ "$MODE" = "readsb" ] || [ "$MODE" = "s1090" ] || die "--mode 只支持 readsb|s1090" 2
run() { if [ "$DRY" = 1 ]; then say "  [dry-run] $*"; else "$@"; fi; }

# ---------------------------------------------------------------- 卸载
if [ "$ACTION" = uninstall ]; then
    step "卸载"
    run systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
    run rm -f "$UNIT_PATH" /usr/local/bin/fc0013_guard.py /usr/local/bin/tee_power
    run systemctl daemon-reload
    say "已卸载：服务停止、unit 与组件已删"
    exit 0
fi

# ---------------------------------------------------------------- 前置检查
step "前置检查"
[ -f "$GUARD" ] || die "缺少 $GUARD（请在发布包目录里运行）" 2
if [ "$MODE" = s1090 ]; then
    for c in rtl_sdr stream1090; do
        command -v $c >/dev/null || die "s1090 模式需要 $c 可执行（PATH 里没有）" 2
    done
fi
systemctl is-active --quiet readsb || die "readsb 未在运行（systemctl is-active readsb）" 2
command -v python3 >/dev/null || die "缺少 python3" 2
ls /lib/*/libusb-1.0.so* >/dev/null 2>&1 || ls /usr/lib/*/libusb-1.0.so* >/dev/null 2>&1 \
    || die "缺少 libusb-1.0（apt install libusb-1.0-0）" 2
say "前置 OK（readsb active、python3、libusb-1.0、模式=$MODE）"

# ---------------------------------------------------------------- tee_power
step "tee_power"
if [ "$BUILD" = 1 ]; then
    [ -f "$TEE_SRC" ] || die "缺少 $TEE_SRC" 2
    command -v gcc >/dev/null || die "--build 需要 gcc" 2
    run gcc -O2 -o /tmp/tee_power "$TEE_SRC" -lm
    say "已现场编译 tee_power"
else
    [ -f "$TEE_PRE" ] || { say "无预编译件，回退现场编译"; run gcc -O2 -o /tmp/tee_power "$TEE_SRC" -lm; }
    [ -f /tmp/tee_power ] || run cp "$TEE_PRE" /tmp/tee_power
fi
run install -m 755 /tmp/tee_power /usr/local/bin/tee_power
say "已安装 /usr/local/bin/tee_power"

# ---------------------------------------------------------------- 守卫本体
step "fc0013_guard"
run install -m 755 "$GUARD" /usr/local/bin/fc0013_guard.py
say "已安装 /usr/local/bin/fc0013_guard.py"

# ---------------------------------------------------------------- systemd unit
step "systemd unit"
ARGS=""
[ "$MODE" = s1090 ] && ARGS=" --mode s1090"
cat > /tmp/$UNIT_NAME <<EOF
[Unit]
Description=FC0013 IF gain guard
After=readsb.service
BindsTo=readsb.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/fc0013_guard.py$ARGS
# 锁被占用（另有一份在跑）时脚本会正常退出，那不是故障：
# 用 on-failure 避免每 5 s 无意义重启（Restart=always 会造成 flapping）
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
run cp /tmp/$UNIT_NAME "$UNIT_PATH"
run systemctl daemon-reload
run systemctl enable --now "$UNIT_NAME"
say "已安装并启动：$UNIT_PATH（mode=$MODE）"

# ---------------------------------------------------------------- 收尾提示
step "完成"
say "自检：  systemctl is-active $UNIT_NAME && tail -3 /tmp/fc_guard.log"
say "期望：  active，且日志行含 fc0013=未访问（工作点在域内的正常态）"
say "起点码对齐（开机/重装后必做一次）："
say "  python3 /usr/local/bin/fc0013_guard.py --pin <deployed.json 里该机的码>"
