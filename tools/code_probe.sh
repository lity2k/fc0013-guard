#!/bin/sh
# code_probe.sh — 实证某个增益码在本机的实际工作点（钉码 → 等窗口 → 采样 → 恢复）
# 用法: sh /tmp/code_probe.sh <node> <probe_code> <back_code>
# 产出: /tmp/code_probe.log
NODE=$1; CODE=$2; BACK=$3
LOG=/tmp/code_probe.log
: > "$LOG"
py() {
  python3 -c "
import json,sys
sys.path.insert(0,'/tmp')
import fc0013_guard as G
l=json.load(open('/run/readsb/stats.json'))['last1min']['local']
r=G.Fc().read_013()
print('noise=%.1f peak=%.1f strong=%s c=%.2f regs=%s'%(
    l['noise'], l['peak_signal'], l.get('strong_signals'),
    G.c_rms(l['noise']), r))
"
}
echo "START $(date -u +%FT%TZ) node=$NODE probe=$CODE back=$BACK" >> "$LOG"
python3 /tmp/fc0013_guard.py --node "$NODE" --pin "$CODE" >> "$LOG" 2>&1
sleep 100; echo -n "probe t+100 " >> "$LOG"; py >> "$LOG" 2>&1
sleep  30; echo -n "probe t+130 " >> "$LOG"; py >> "$LOG" 2>&1
python3 /tmp/fc0013_guard.py --node "$NODE" --pin "$BACK" >> "$LOG" 2>&1
sleep  90; echo -n "back  t+90  " >> "$LOG"; py >> "$LOG" 2>&1
echo "DONE $(date -u +%FT%TZ)" >> "$LOG"
