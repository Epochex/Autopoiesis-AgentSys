#!/usr/bin/env bash
# Create a real, observable fault for the sentinel to find — and clean it up.
#
# Nothing here is simulated into a fixture. The service really runs, really
# dies, and systemd really reports it as failed, because a demo that injects a
# fake reading proves only that the reading was faked. The blast radius is a
# unit that exists for this purpose and nothing depends on.
#
#   ./inject_incident.sh service-down     a watched unit crashes
#   ./inject_incident.sh bruteforce       repeated failed SSH auth from a fake source
#   ./inject_incident.sh status           what is currently injected
#   ./inject_incident.sh cleanup          remove everything this script created
#
# The two scenarios are chosen to show opposite verdicts: service-down has a
# monotonic action and gets fixed unattended; bruteforce deliberately has none,
# so the system reports it and stops. A demo where everything auto-heals hides
# the more important half of the design.
set -euo pipefail

UNIT=demo-collector
UNIT_FILE="/etc/systemd/system/${UNIT}.service"
FAKE_SOURCE="203.0.113.77"   # RFC 5737 documentation range: never a real host

die() { echo "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "需要 root（systemctl 与 journal 写入）"

install_unit() {
    cat > "$UNIT_FILE" <<'UNITEOF'
[Unit]
Description=Demo collector for incident-response rehearsal

[Service]
Type=simple
# Sleeps until told otherwise. Restart=no so a crash stays crashed and the
# sentinel is the thing that brings it back, not systemd.
ExecStart=/bin/sh -c 'while true; do sleep 5; done'
Restart=no

[Install]
WantedBy=multi-user.target
UNITEOF
    systemctl daemon-reload
}

case "${1:-}" in
service-down)
    install_unit
    systemctl start "$UNIT"
    sleep 1
    systemctl is-active --quiet "$UNIT" || die "单元没起来，注入失败"
    echo "已启动 $UNIT，正在制造崩溃…"
    # SIGKILL the main process: systemd records a failed unit, which is exactly
    # what a real crash looks like to every layer above.
    MAIN_PID=$(systemctl show "$UNIT" -p MainPID --value)
    kill -9 "$MAIN_PID" 2>/dev/null || true
    sleep 2
    STATE=$(systemctl is-active "$UNIT" || true)
    echo "注入完成：$UNIT 现在是 $STATE"
    echo
    echo "接下来会发生什么："
    echo "  1. 哨兵下一轮轮询看到它 failed（需要连续 2 轮确认）"
    echo "  2. preflight 检查：是不是真 failed、重启次数够不够、是不是依赖挂了"
    echo "  3. 通过后重启，回读确认起来了"
    echo "  4. 进观察期，期间同时盯这个单元和网关健康"
    echo "  5. 没有回归就判定 resolved"
    echo
    echo "看过程：curl -s localhost:8026/api/rca/sentinel/timeline | python3 -m json.tool"
    ;;

bruteforce)
    echo "向系统日志写入来自 $FAKE_SOURCE 的失败登录记录…"
    for i in $(seq 1 12); do
        logger -t sshd -p auth.warning \
            "Failed password for invalid user admin from ${FAKE_SOURCE} port $((40000 + i)) ssh2"
    done
    echo "注入完成：12 条失败登录"
    echo
    echo "这一条会被检测到，但不会自动处置——封禁是可撤销的，"
    echo "但封错来源就等于堵住自己的管理通道，所以设计上只报不动。"
    echo "时间线里会看到 detected 后面跟着 no_safe_action，这是对的。"
    ;;

status)
    echo "注入单元：$(systemctl is-active "$UNIT" 2>/dev/null || echo '未安装')"
    [[ -f "$UNIT_FILE" ]] && echo "单元文件：存在" || echo "单元文件：无"
    echo "近期该来源的失败登录：$(journalctl --since -30m --no-pager 2>/dev/null | grep -c "$FAKE_SOURCE" || echo 0) 条"
    ;;

cleanup)
    systemctl stop "$UNIT" 2>/dev/null || true
    systemctl disable "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    echo "已清理 $UNIT。日志里的失败登录记录会随 journal 轮转自然过期，不影响任何东西。"
    ;;

*)
    grep '^#' "$0" | sed 's/^# \?//' | head -18
    exit 1
    ;;
esac
