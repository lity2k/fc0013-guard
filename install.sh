#!/bin/bash
# install.sh — 在目标节点上安装 fc0013_guard 全套组件。
#
# 组件与落位：
#   fc0013_guard.py            -> /usr/local/bin/fc0013_guard.py
#   tee_power（仅 s1090）       -> /usr/local/bin/tee_power
#   fc0013-guard.service       -> /etc/systemd/system/（unit 绑 readsb 生命周期）
#   /etc/fc0013-guard.conf     -> 起点码对齐结果 + 链路采样率（安装后按需写入）
#
# 取件顺序：本地优先 —— 脚本所在目录有同名文件（git clone / 发布目录）就直接用；
# 缺件才到 GitHub main 下载。两边都取不到 ⇒ 报错退出。
# 不处理编译环境：tee_power 只取预编译件，取不到即退出。
#
# 用法（在含本文件的目录里执行；目录里只有本 install.sh 也行，其余自动下载）：
#   sudo bash install.sh                 # 交互终端会询问模式（回车 = readsb）；
#                                        # 选 s1090 再询问链路采样率（默认取
#                                        # stream1090 支持表第一档）并落盘生效
#   sudo bash install.sh --mode s1090    # 显式指定，不再询问模式
#   sudo bash install.sh --align         # 安装后直接做起点码对齐（默认只在交互终端询问）
#   sudo bash install.sh --uninstall     # 卸载：停服务 + 删 unit + 组件 + 对齐结果
#   sudo bash install.sh --dry-run       # 只打印动作，不改系统
#
# 安装完会询问是否立即做起点码对齐（--code-scan），同意则停守卫跑一次并保存结果。
#
# 前置（脚本自检）：readsb 在跑、python3 与 libusb-1.0 存在；
# s1090 模式额外要求 rtl_sdr 与 stream1090 可执行。
#
# 退出码：0 成功；1 运行失败；2 用法/前置不满足。
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
BASE_URL=https://raw.githubusercontent.com/lity2k/fc0013-guard/main
MODE=readsb
ACTION=install
DRY=0
ALIGN=0

UNIT_NAME=fc0013-guard.service
UNIT_PATH=/etc/systemd/system/$UNIT_NAME
CAL_FILE=/etc/fc0013-guard.conf      # 对齐结果 + 采样率（逐机），守卫启动时优先取用

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }

for arg in "$@"; do
    case "$arg" in
        --mode)      MODE_PENDING=1; EXPLICIT_MODE=1 ;;
        --mode=*)    MODE=${arg#*=}; EXPLICIT_MODE=1 ;;
        --readsb)    MODE=readsb; EXPLICIT_MODE=1 ;;
        --s1090)     MODE=s1090; EXPLICIT_MODE=1 ;;
        --align)     ALIGN=1 ;;
        --uninstall) ACTION=uninstall ;;
        --dry-run)   DRY=1 ;;
        -h|--help)   sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)           [ -n "${MODE_PENDING:-}" ] && { MODE=$arg; MODE_PENDING=; EXPLICIT_MODE=1; } \
                     || die "未知参数：$arg（见 --help）" 2 ;;
    esac
done
# 部署模式：显式参数 > 交互询问（回车 = readsb）> 非交互默认 readsb
if [ -z "${EXPLICIT_MODE:-}" ] && [ "$ACTION" = install ] && [ -t 0 ]; then
    printf '部署模式？readsb = readsb 独占 SDR 直采（默认）；s1090 = stream1090 管道 [readsb]: '
    read -r ans || ans=""
    case "$ans" in s1090|S1090) MODE=s1090 ;; esac
fi
[ "$MODE" = "readsb" ] || [ "$MODE" = "s1090" ] || die "--mode 只支持 readsb|s1090" 2
run() { if [ "$DRY" = 1 ]; then say "  [dry-run] $*"; else "$@"; fi; }

# fetch <仓库相对路径> <落位路径>：本地 $HERE/<rel> 优先；缺件才从 GitHub main 下载
fetch() {
    local rel="$1" dst="$2"
    local local_file="$HERE/$rel"
    if [ "$DRY" = 1 ]; then
        say "  [dry-run] 取 $rel -> $dst"
        return 0
    fi
    if [ -f "$local_file" ]; then
        install -m 755 "$local_file" "$dst"
        say "已安装 $dst（本地）"
    elif curl -fsSL --connect-timeout 8 -m 60 "$BASE_URL/$rel" -o "$dst"; then
        chmod 755 "$dst"
        say "已安装 $dst（GitHub main）"
    else
        die "取件失败：$1（$HERE 里没有，且 $BASE_URL 不可达）" 1
    fi
}

# ---------------------------------------------------------------- 卸载
if [ "$ACTION" = uninstall ]; then
    step "卸载"
    run systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
    run rm -f "$UNIT_PATH" /usr/local/bin/fc0013_guard.py /usr/local/bin/tee_power \
             "$CAL_FILE" "$SR_CONF"
    run rmdir "$DROPIN_DIR" 2>/dev/null || true
    run systemctl daemon-reload
    say "已卸载：服务停止、unit 与组件已删"
    exit 0
fi

# ---------------------------------------------------------------- 前置检查
step "前置检查"
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

# ---------------------------------------------------------------- 组件
step "fc0013_guard"
fetch "fc0013_guard.py" /usr/local/bin/fc0013_guard.py

step "tee_power"
if [ "$MODE" = s1090 ]; then
    fetch "tee_power/tee_power-armhf" /usr/local/bin/tee_power
else
    say "readsb 形态不用 tee_power，跳过"
fi

# ---------------------------------------------------------------- 采样率（仅 s1090）
# stream1090 各版本支持的输入组合不同（经典版 2.4→8，turbo 版仅 2.56→12），
# 守卫默认按 2.4 起管道。这里把采样率经 --set-sample-rate 校验（对照支持表）后
# 落盘到 CAL_FILE 的 sample_rate 字段，守卫启动时整条链路按它对齐。
if [ "$MODE" = s1090 ]; then
    step "采样率"
    RATES=$(stream1090 -h 2>&1 | awk '/Supported sample rate/{f=1;next} f&&/^[[:space:]]*$/{f=0} f' | awk 'NR>0{print $1}')
    if [ -z "$RATES" ]; then
        say "WARN 读不到 stream1090 支持表，跳过采样率设置（守卫用默认 2.4 MHz）"
    else
        DEF=$(printf '%s\n' "$RATES" | head -1)
        SR=""
        if [ "$DRY" = 0 ] && [ -t 0 ]; then
            printf '链路采样率 MHz（stream1090 支持: %s；回车 = %s）: ' \
                "$(printf '%s ' $RATES)" "$DEF"
            read -r SR || SR=""
        fi
        [ -n "$SR" ] || SR=$DEF
        run python3 /usr/local/bin/fc0013_guard.py --set-sample-rate "$SR"
    fi
fi

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

# ---------------------------------------------------------------- 起点码对齐（可选）
# --align 直接做；未给且是交互终端则询问；非交互且未给 ⇒ 跳过（脚本化安装不阻塞）
DO_ALIGN=$ALIGN
if [ "$DO_ALIGN" = 0 ] && [ "$DRY" = 0 ] && [ -t 0 ]; then
    printf '\n现在做起点码对齐？（--code-scan，约 4-6 min，期间会停守卫）[y/N] '
    read -r ans || ans=N
    case "$ans" in y|Y|yes|YES) DO_ALIGN=1 ;; *) DO_ALIGN=0 ;; esac
fi
if [ "$DRY" = 0 ] && [ "$DO_ALIGN" = 1 ]; then
    step "起点码对齐"
    systemctl stop "$UNIT_NAME"
    python3 /usr/local/bin/fc0013_guard.py --code-scan --adopt --mode "$MODE" || true
    systemctl start "$UNIT_NAME"
else
    say "跳过起点码对齐。之后随时可跑："
    say "  systemctl stop $UNIT_NAME"
    say "  python3 /usr/local/bin/fc0013_guard.py --code-scan --mode $MODE"
fi

# ---------------------------------------------------------------- 收尾提示
step "完成"
say "自检：  systemctl is-active $UNIT_NAME && tail -3 /tmp/fc_guard.log"
say "期望：  active，且日志行含 fc0013=未访问（工作点在域内的正常态）"
if [ -f "$CAL_FILE" ]; then
    say "起点码： $(cat "$CAL_FILE")   （守卫启动默认用它）"
else
    say "起点码： 未对齐 ⇒ systemctl stop $UNIT_NAME && python3"
    say "         /usr/local/bin/fc0013_guard.py --code-scan --mode $MODE"
fi
