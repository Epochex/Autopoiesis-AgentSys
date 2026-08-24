#!/usr/bin/env bash
# Create controlled, observable conditions for sentinel response checks.
#
# The service scenario changes a dedicated systemd unit. The security scenario
# writes bounded authentication-log records from an RFC 5737 address. Cleanup
# removes the unit and restores recurrence parameters changed by this script.
#
#   ./inject_incident.sh service-down   a watched unit crashes
#   ./inject_incident.sh service-detect-only  leave a unique unit failed for read-only investigation
#   ./inject_incident.sh bruteforce     repeated failed SSH auth from a controlled source
#   ./inject_incident.sh recurring      the same unit crashes over and over until the
#                                       system refuses to keep fixing it (~9 min, hands off)
#   ./inject_incident.sh status         what is currently injected
#   ./inject_incident.sh cleanup        remove everything this script created
#
# The scenarios exercise verified recovery, a safety-gated security handoff,
# and recurrence escalation after the configured threshold.
set -euo pipefail

SCENARIO="${1:-}"
BASE_UNIT=demo-collector
RUN_STATE="/run/autopoiesis-service-down-unit"
if [[ "$SCENARIO" == "service-down" || "$SCENARIO" == "service-detect-only" ]]; then
    # A single-fault run represents a newly provisioned disposable service
    # instance. The fixed BASE_UNIT is reserved for the recurring scenario so
    # recurrence evidence from that scenario cannot alter this one's outcome.
    UNIT="${BASE_UNIT}-$(date -u +%Y%m%d%H%M%S)"
elif [[ ( "$SCENARIO" == "cleanup" || "$SCENARIO" == "status" ) && -s "$RUN_STATE" ]]; then
    UNIT=$(<"$RUN_STATE")
else
    UNIT="$BASE_UNIT"
fi
UNIT_FILE="/etc/systemd/system/${UNIT}.service"
SUBJECT="${UNIT}.service"       # what systemd --failed prints, and what the timeline records
CONTROLLED_SOURCE="203.0.113.77"   # RFC 5737 documentation range

GATEWAY=netops-ops-console-backend
DROPIN_DIR="/etc/systemd/system/${GATEWAY}.service.d"
DROPIN_FILE="${DROPIN_DIR}/zz-demo-recurrence.conf"
DEMO_ENV_FILE="/etc/selfevo-console-demo-recurrence.env"
TIMELINE_DEFAULT="/data/autopoiesis-runtime/sentinel-timeline.jsonl"
HEALTHZ="http://127.0.0.1:8026/api/healthz"

# Recurrence timing used by the controlled workflow.
#
# Production uses a 24-hour recurrence window and a 600s base cooldown. The
# `recurring` workflow shortens the time window and cooldown while retaining
# the production refusal threshold of three recurrences.
#
# 3600 rather than something smaller because the escalation note prints
# `window_sec // 3600` hours: at one hour the sentinel says "在 1 小时内已经生效
# 过 3 次又复发" and that sentence is exactly true. A 30-minute window would put
# a rounded-up number on the screen.
DEMO_WINDOW=3600     # AUTOPOIESIS_RECURRENCE_WINDOW  (prod: 86400)
DEMO_LIMIT=3         # AUTOPOIESIS_RECURRENCE_LIMIT   (prod: 3, unchanged)
DEMO_COOLDOWN=30     # AUTOPOIESIS_SENTINEL_COOLDOWN  (prod: 600)
DEMO_INTERVAL=10     # AUTOPOIESIS_SENTINEL_INTERVAL  (this box: 15)

# 30s base is picked against the 90s watch window: the ladder doubles it to 60s
# and then 120s, so the first two cooldowns expire while the sentinel is still
# observing its own repair, while the third is long
# enough to be visible in the timeline as a real escalating quiet period.

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

# ── reading the gateway's real environment ───────────────────────────────────
# `systemctl show -p Environment` omits EnvironmentFile values. Read the running
# process environment to verify the active configuration after restart.
gateway_env() {
    local pid value
    pid=$(systemctl show "$GATEWAY" -p MainPID --value 2>/dev/null || echo 0)
    [[ -n "$pid" && "$pid" != 0 ]] || return 0
    value=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep "^$1=" | head -1 || true)
    echo "${value#*=}"
}

sentinel_enabled() { [[ "$(gateway_env AUTOPOIESIS_SENTINEL)" == "1" ]]; }

resolve_timeline() {
    local configured
    configured=$(gateway_env AUTOPOIESIS_SENTINEL_TIMELINE)
    TIMELINE="${configured:-$TIMELINE_DEFAULT}"
}

wait_for_gateway() {
    local deadline=$((SECONDS + 45))
    while (( SECONDS < deadline )); do
        if curl -fsS -m 2 "$HEALTHZ" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    return 1
}

# Controlled recurrence override and restoration.
#
# Why a drop-in with its own EnvironmentFile, and not the obvious alternatives:
#
#   - Editing /etc/selfevo-console.env in place would work, but that file holds
#     every provider credential on this box. A sed against a secrets file, with
#     a demo script that might be Ctrl-C'd halfway, is not a trade worth making
#     for four numbers.
#   - A drop-in with plain `Environment=` lines does NOT work here, and this is
#     systemd applies EnvironmentFile= over Environment=
#     regardless of order, and AUTOPOIESIS_SENTINEL_INTERVAL is already set in
#     /etc/selfevo-console.env. Measured on this box (systemd 249), not assumed.
#     So the override has to arrive as an EnvironmentFile too.
#   - Drop-ins are read in lexical filename order and a later EnvironmentFile
#     wins over an earlier one, so the name must sort after `provider-env.conf`.
#     `zz-` does; a `99-` prefix would silently lose, because digits sort first.
#   - `systemctl set-environment` would leak the compressed window into every
#     unit on the box, which is a much worse thing to forget about.
#
# Two files, both created here and both deleted by cleanup, neither of them
# anything the box owns.
apply_demo_override() {
    mkdir -p "$DROPIN_DIR"
    cat > "$DEMO_ENV_FILE" <<ENVEOF
# 受控流程专用，由 scripts/inject_incident.sh recurring 写入，cleanup 删除。
# 这里只压缩时间，不改判据：拒绝阈值 3 和生产默认一致。
# 非受控流程期间发现该文件时应删除它，
# 然后 systemctl daemon-reload && systemctl restart ${GATEWAY}
AUTOPOIESIS_RECURRENCE_WINDOW=${DEMO_WINDOW}
AUTOPOIESIS_RECURRENCE_LIMIT=${DEMO_LIMIT}
AUTOPOIESIS_SENTINEL_COOLDOWN=${DEMO_COOLDOWN}
AUTOPOIESIS_SENTINEL_INTERVAL=${DEMO_INTERVAL}
ENVEOF
    cat > "$DROPIN_FILE" <<DROPEOF
# 受控流程覆盖。文件名必须排在 provider-env.conf 之后：drop-in 按文件名字典序
# 读入，后一个 EnvironmentFile 才盖得住前一个。cleanup 会删掉这一层。
[Service]
EnvironmentFile=-${DEMO_ENV_FILE}
DROPEOF
    systemctl daemon-reload
    echo "已写入受控流程参数，正在重启网关加载配置…"
    systemctl restart "$GATEWAY"
    wait_for_gateway || die "网关重启后 45 秒内没起来，先查 journalctl -u $GATEWAY -n 50"

    # Verify against the process, not the file. A drop-in that parsed wrong, or
# sorted wrong, fails here, and a half-applied
    # override is the one outcome worth rolling back automatically, because the
    # alternative is a box left with a compressed window and nobody watching.
    local got got_limit
    got=$(gateway_env AUTOPOIESIS_RECURRENCE_WINDOW)
    got_limit=$(gateway_env AUTOPOIESIS_RECURRENCE_LIMIT)
    if [[ "$got" != "$DEMO_WINDOW" || "$got_limit" != "$DEMO_LIMIT" ]]; then
        echo "覆盖没生效：网关进程里 窗口=${got:-未设置} 阈值=${got_limit:-未设置}，期望 $DEMO_WINDOW / $DEMO_LIMIT。" >&2
        echo "systemd 读到的 drop-in 顺序（$(basename "$DROPIN_FILE") 必须排在 provider-env.conf 之后）：" >&2
        systemctl cat "$GATEWAY" 2>/dev/null | grep '^# /etc' >&2 || true
        revert_demo_override >/dev/null || true
        die "已回滚受控流程参数，网关使用生产配置。"
    fi
    echo "覆盖已生效（窗口 ${DEMO_WINDOW}s / 阈值 ${DEMO_LIMIT} / 冷却 ${DEMO_COOLDOWN}s / 巡检 ${DEMO_INTERVAL}s）"
}

# Idempotent: returns without changes when there is no override.
# Returns 0 if it removed one (and therefore restarted the gateway), 1 if there
# no override was present.
revert_demo_override() {
    if [[ ! -e "$DROPIN_FILE" && ! -e "$DEMO_ENV_FILE" ]]; then
        # Belt and braces: the files can be gone while a process started through
        # them is still running with the compressed window in memory.
        if [[ -n "$(gateway_env AUTOPOIESIS_RECURRENCE_WINDOW)" ]]; then
            echo "覆盖文件已经不在，但网关进程里还留着压缩窗口，重启一次收回…"
            systemctl restart "$GATEWAY"
            wait_for_gateway || echo "警告：网关没在 45 秒内起来，查 journalctl -u $GATEWAY" >&2
            return 0
        fi
        return 1
    fi
    rm -f "$DROPIN_FILE" "$DEMO_ENV_FILE"
    systemctl daemon-reload
    echo "已删除受控流程参数，正在重启网关恢复生产窗口…"
    systemctl restart "$GATEWAY"
    wait_for_gateway || echo "警告：网关没在 45 秒内起来，查 journalctl -u $GATEWAY" >&2
    local left
    left=$(gateway_env AUTOPOIESIS_RECURRENCE_WINDOW)
    if [[ -n "$left" ]]; then
        echo "警告：网关进程里仍有 AUTOPOIESIS_RECURRENCE_WINDOW=$left，请检查 /etc/selfevo-console.env 的重复配置" >&2
    else
        echo "已恢复生产口径（窗口 24h / 基础冷却 600s）"
    fi
    return 0
}

# Ctrl-C in the middle of a nine-minute act, or a round that never closes, must
# not leave the compressed window behind: an unattended box running a 1-hour
# window and a 30s cooldown is a misconfiguration nobody would ever notice.
# Armed only once the override is actually in place.
DEMO_COMPLETE=0
revert_on_abort() {
    if (( DEMO_COMPLETE )); then return 0; fi
    echo >&2
    echo "受控流程中断，正在恢复生产窗口…" >&2
    revert_demo_override >/dev/null 2>&1 || true
    echo "已回到生产口径。demo-collector 可能还留着，用 ./scripts/inject_incident.sh cleanup 收尾。" >&2
}

# ── reading the ladder's own state out of the timeline ───────────────────────
# Same shape as the projection the backend runs: a cycle counts only when a
# `resolved` is followed by a LATER `detected` on the same subject. A repair
# that is still holding is not a recurrence. If this ever disagrees with the
# console, the console projection is authoritative. This check warns the operator before an
# eight-minute sequence starts from the wrong rung, not to be a second source
# of truth.
cycles_in_window() {
    local window=$1 line at ts now count=0 pending=0
    now=$(date +%s)
    [[ -f "$TIMELINE" ]] || { echo 0; return 0; }
    while IFS= read -r line; do
        at=${line#*'"at": "'}; at=${at%%'"'*}
        ts=$(date -d "$at" +%s 2>/dev/null || echo 0)
        if [[ "$line" == *'"kind": "resolved"'* ]]; then
            pending=$ts
        elif [[ "$line" == *'"kind": "detected"'* ]]; then
            if (( pending > 0 )); then
                if (( now - pending <= window )); then count=$((count + 1)); fi
                pending=0
            fi
        fi
    done < <(grep -F "\"subject\": \"$SUBJECT\"" "$TIMELINE" 2>/dev/null \
             | grep -E '"kind": "(resolved|detected)"' || true)
    echo "$count"
}

# ── crashing it, and watching the loop react ─────────────────────────────────
crash_unit() {
    systemctl start "$UNIT" >/dev/null
    sleep 1
    local pid
    pid=$(systemctl show "$UNIT" -p MainPID --value)
    [[ -n "$pid" && "$pid" != 0 ]] || die "$UNIT 没起来，注入失败"
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
    echo "   $UNIT 现在是 $(systemctl is-active "$UNIT" || true)"
}

CURSOR=0
narrate() {
    local line=$1 reason
    [[ "$line" == *"$SUBJECT"* ]] || return 0
    case "$line" in
        *'"kind": "detected"'*)             echo "   · 检测事实已记录" ;;
        *'"kind": "awaiting_confirmation"'*) echo "   · 二次确认：排除部署瞬态" ;;
        *'"kind": "preflight"'*)            echo "   · 安全门条件：目标状态与影响范围校验" ;;
        *'"kind": "remediation_committed"'*) echo "   · 动作已提交并完成回读，进入观察" ;;
        *'"kind": "bakein_opened"'*)         echo "   · 快速回退窗与稳定性观察窗已打开" ;;
        *'"kind": "bakein_passed"'*)         echo "   · 观察窗采样通过" ;;
        *'"kind": "cooldown"'*)             echo "   · 决策结果：冷却期内写操作未授权" ;;
        *'"kind": "command"'*)
            # Preflight streams its read-only probes through the same channel;
            # only the restart itself is worth a line on stage.
            if [[ "$line" == *'"restart"'* ]]; then
                echo "   · 动作回执：systemctl restart 已提交，进入回退窗和稳定性观察窗"
            fi ;;
        *'"kind": "declined"'*)             echo "   · 决策结果：安全门未放行" ;;
        *'"kind": "remediated"'*)           echo "   · 回读观察完成" ;;
        *'"kind": "resolved"'*)             echo "   · 决策结果：恢复已验证" ;;
        *'"kind": "escalated"'*)
            reason=${line#*'"reason": "'}; reason=${reason%%'"'*}
            echo "   · 决策结果：写操作未授权"
            echo "   · 后续责任：值班人员处置"
            echo "     「$reason」" ;;
    esac
}

# Follow the timeline until the kind that closes this round shows up.
follow_until() {
    local want=$1 timeout=$2 deadline=$((SECONDS + $2)) total line hit=1 consumed
    while (( SECONDS < deadline )); do
        total=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
        if (( total > CURSOR )); then
            consumed=0
            # Stop at the line that closes the round rather than draining the
            # whole tail, so the next round starts reading from just after it.
            while IFS= read -r line; do
                consumed=$((consumed + 1))
                narrate "$line"
                if [[ "$line" == *"\"kind\": \"$want\""* && "$line" == *"$SUBJECT"* ]]; then
                    hit=0
                    break
                fi
            done < <(tail -n "+$((CURSOR + 1))" "$TIMELINE" | head -n "$((total - CURSOR))")
            CURSOR=$((CURSOR + consumed))
        fi
        if (( hit == 0 )); then return 0; fi
        sleep 2
    done
    echo "   等了 ${timeout} 秒没等到 $want。" >&2
    return 1
}

trigger_security_poll() {
    # The endpoint is non-blocking when the background sentinel already owns
    # the poll lock.  A short client timeout is intentional: the detector writes
    # the timeline before incident consolidation finishes, and the timeline is
    # what the browser consumes.
    curl -fsS -m 3 -X POST \
        "http://127.0.0.1:8026/api/rca/sentinel/poll?detector=admin_bruteforce" \
        >/dev/null 2>&1 || true
}

security_event_since() {
    local kind=$1
    [[ -f "$TIMELINE" ]] || return 1
    tail -n "+$((CURSOR + 1))" "$TIMELINE" 2>/dev/null \
        | grep -F "\"subject\": \"$CONTROLLED_SOURCE\"" \
        | grep -q "\"kind\": \"$kind\""
}

wait_for_security_event() {
    local kind=$1 timeout=$2 deadline
    deadline=$((SECONDS + timeout))
    while (( SECONDS < deadline )); do
        security_event_since "$kind" && return 0
        trigger_security_poll
        sleep 2
    done
    return 1
}

# Observe the unit healthy once before injecting the crash. This clears any
# in-process confirmation streak left by an earlier rehearsal, so this round
# must earn its own two consecutive detections.
reset_service_confirmation() {
    local deadline=$((SECONDS + 60)) body start_line
    start_line=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
    while (( SECONDS < deadline )); do
        # failed_units invokes real systemctl probes; on a loaded host one poll
        # can legitimately take more than five seconds. Keep the client alive
        # long enough to receive the completed response instead of repeatedly
        # abandoning successful health observations.
        body=$(curl -fsS -m 15 -X POST \
            "http://127.0.0.1:8026/api/rca/sentinel/poll?detector=failed_units" \
            2>/dev/null || true)
        if [[ -n "$body" ]] && python3 -c \
            'import json,sys; raise SystemExit(1 if json.load(sys.stdin).get("busy") else 0)' \
            <<<"$body"; then
            return 0
        fi
        # The poll records `cycle` immediately after detector state has been
        # updated. Durable-memory follow-up may keep the HTTP request open
        # longer, but that does not invalidate the completed healthy sample.
        if systemctl is-active --quiet "$UNIT" \
            && tail -n "+$((start_line + 1))" "$TIMELINE" 2>/dev/null \
                | grep -q '"kind": "cycle"'; then
            return 0
        fi
        sleep 2
    done
    return 1
}

service_event_after() {
    local kind=$1 start_line=$2
    [[ -f "$TIMELINE" ]] || return 1
    tail -n "+$((start_line + 1))" "$TIMELINE" 2>/dev/null \
        | grep -F "\"subject\": \"$SUBJECT\"" \
        | grep -q "\"kind\": \"$kind\""
}

wait_for_service_event() {
    local kind=$1 start_line=$2 timeout=$3 deadline=$((SECONDS + $3))
    while (( SECONDS < deadline )); do
        service_event_after "$kind" "$start_line" && return 0
        sleep 1
    done
    return 1
}

# LiveAlerts opens by subject. The landed-situation feed and suggestion must
# carry that exact same deviceKey for the click to select the matching record.
live_service_card_visible() {
    curl -fsS -m 5 "http://127.0.0.1:8026/api/rca/live-situation?lang=zh" 2>/dev/null \
        | python3 -c '
import json, sys
subject = sys.argv[1]
payload = json.load(sys.stdin)
card = next((row for row in payload.get("suggestions", [])
             if row.get("scope") == "sentinel" and row.get("deviceKey") == subject), None)
feed = next((row for row in payload.get("feed", [])
             if row.get("scope") == "sentinel" and row.get("deviceKey") == subject), None)
raise SystemExit(0 if card is not None and feed is not None else 1)
' "$SUBJECT"
}

wait_for_live_service_card() {
    local timeout=$1 deadline=$((SECONDS + $1))
    while (( SECONDS < deadline )); do
        live_service_card_visible && return 0
        sleep 1
    done
    return 1
}

# Follow the current round and stop on every real terminal branch. Waiting only
# for resolved would hide a prompt preflight refusal behind a six-minute timeout.
SERVICE_OUTCOME=""
follow_service_round() {
    local timeout=$1 deadline=$((SECONDS + $1)) total line consumed
    while (( SECONDS < deadline )); do
        total=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
        if (( total > CURSOR )); then
            consumed=0
            while IFS= read -r line; do
                consumed=$((consumed + 1))
                narrate "$line"
                [[ "$line" == *"$SUBJECT"* ]] || continue
                case "$line" in
                    *'"kind": "resolved"'*) SERVICE_OUTCOME=resolved; break ;;
                    *'"kind": "declined"'*) SERVICE_OUTCOME=declined; break ;;
                    *'"kind": "cooldown"'*) SERVICE_OUTCOME=cooldown; break ;;
                    *'"kind": "escalated"'*) SERVICE_OUTCOME=escalated; break ;;
                    *'"kind": "remediated"'*'"outcome": "refused"'*)
                        SERVICE_OUTCOME=refused; break ;;
                    *'"kind": "remediated"'*'"needs_human": true'*)
                        SERVICE_OUTCOME=needs_human; break ;;
                esac
            done < <(tail -n "+$((CURSOR + 1))" "$TIMELINE" | head -n "$((total - CURSOR))")
            CURSOR=$((CURSOR + consumed))
        fi
        [[ -z "$SERVICE_OUTCOME" ]] || return 0
        sleep 2
    done
    return 1
}

# Verify actual durable timestamps for every stage. The detailed follow-up
# events prove ACT and WATCH happened; neither is inferred from the final row.
verify_service_chain() {
    local start_line=$1
    python3 - "$TIMELINE" "$start_line" "$SUBJECT" <<'PY'
import json
import sys

path, cursor, subject = sys.argv[1], int(sys.argv[2]), sys.argv[3]
rows = []
with open(path, encoding="utf-8") as handle:
    for line in list(handle)[cursor:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("subject") == subject:
            rows.append(row)

required = [
    "detected", "awaiting_confirmation", "detected", "preflight",
    "remediation_committed", "bakein_opened", "remediated", "resolved",
]
position = 0
evidence = []
for wanted in required:
    while position < len(rows) and rows[position].get("kind") != wanted:
        position += 1
    if position >= len(rows):
        print(f"缺少链路事件 {wanted}；已看到 {[row.get('kind') for row in rows]}", file=sys.stderr)
        raise SystemExit(1)
    evidence.append((wanted, rows[position].get("at")))
    position += 1

print("   · 持久化链路顺序已核对：")
for kind, at in evidence:
    print(f"     {at}  {kind}")
PY
}

case "${1:-}" in
service-down|service-detect-only)
    printf '%s\n' "$UNIT" > "$RUN_STATE"
    install_unit
    systemctl start "$UNIT"
    sleep 1
    systemctl is-active --quiet "$UNIT" || die "单元没起来，注入失败"
    resolve_timeline
    if sentinel_enabled; then
        reset_service_confirmation \
            || die "60 秒内未完成故障前健康观测；本次未触发故障，请检查巡检锁和网关日志"
    fi
    CURSOR=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
    ROUND_START=$CURSOR
    echo "已启动 $UNIT，正在制造崩溃…"
    # SIGKILL the main process: systemd records a failed unit, which is exactly
    # what a real crash looks like to every layer above.
    MAIN_PID=$(systemctl show "$UNIT" -p MainPID --value)
    kill -9 "$MAIN_PID" 2>/dev/null || true
    sleep 2
    STATE=$(systemctl is-active "$UNIT" || true)
    echo "故障条件已触发：$UNIT 当前状态为 $STATE"
    echo
    if sentinel_enabled; then
        echo "哨兵自动巡检已启用。"
        echo
        echo "正在等待首页 LiveAlerts 先收到检测事实…"
        wait_for_service_event detected "$ROUND_START" 45 \
            || die "45 秒内首页告警源没有 detected。检查 $TIMELINE 和网关日志。"
        echo "  · 首页 LiveAlerts 的后端时间线已出现 $SUBJECT"
        wait_for_live_service_card 30 \
            || die "detected 已落盘，但没有投影出同 deviceKey 的态势卡和 feed 记录"
        echo "  · 点击该提醒可按 $SUBJECT 定位同一态势记录"
        if [[ "$SCENARIO" == "service-detect-only" ]]; then
            echo
            echo "只读调查条件已就绪：$SUBJECT 保持 failed，实际对象已写入 $RUN_STATE。"
            echo "页面调查完成后运行 cleanup，或解除暂停让哨兵重新评估。"
            exit 0
        fi
        echo
        echo "现在进入拓扑剧场观察：DETECT → CONFIRM → PREFLIGHT → ACT → WATCH → VERIFY"
        echo "脚本继续等待真实双观察窗闭环，并在结束时核对持久化事件顺序。"
    else
        echo "哨兵没开（AUTOPOIESIS_SENTINEL=1 可开启自动巡检）。"
        echo "手动推进：curl -X POST localhost:8026/api/rca/sentinel/poll   （跑两次）"
        echo
        echo "会依次出现："
    fi
    echo "  检测事实 → 二次确认 → 安全门条件 → 动作回执 → 回读观察 → 恢复已验证"
    echo
    echo "当前默认观察为约 60 秒快速回退窗 + 180 秒稳定性窗口。"
    echo "关键保护指标恶化会快速失败；连续健康通过稳定性窗口后才记为恢复。"
    if sentinel_enabled; then
        follow_service_round 360 || die "360 秒内未形成终态；检查网关日志和 $TIMELINE。"
        case "$SERVICE_OUTCOME" in
            resolved)
                verify_service_chain "$ROUND_START" \
                    || die "服务恢复了，但持久化阶段顺序不完整"
                echo "service-down 端到端闭环通过，$UNIT 当前为 $(systemctl is-active "$UNIT" || true)。"
                ;;
            declined)
                die "安全门拒绝本轮执行；检查全局暂停、预算和 preflight reason。" ;;
            refused)
                die "处置预算未放行本轮执行；检查 remediated 记录中的 budget_decision。" ;;
            cooldown)
                die "本轮处于冷却期，写操作未授权；等待时间线中的 remaining_sec 归零后重试。" ;;
            escalated)
                die "本轮进入复发升级，不能作为首次 service-down 自愈演示；先检查 status 中的复发计数。" ;;
            needs_human)
                die "本轮观察或回滚需要人工确认；检查 remediated detail。" ;;
        esac
    fi
    ;;

bruteforce)
    resolve_timeline
    CURSOR=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
    echo "检测事实准备：向认证日志写入来自 $CONTROLLED_SOURCE 的 12 条失败登录记录…"
    for i in $(seq 1 12); do
        logger -t sshd -p auth.warning \
            "Failed password for invalid user admin from ${CONTROLLED_SOURCE} port $((40000 + i)) ssh2"
    done
    echo "日志已写入：12 条失败登录。正在触发真实巡检并等待首页收到事件…"
    if wait_for_security_event detected 30; then
        echo "  · 检测事实已记录：来源 $CONTROLLED_SOURCE，12 条失败登录"
    else
        die "30 秒内没有生成 detected。检查：journalctl -t sshd --since -10m；再看 $TIMELINE"
    fi
    if wait_for_security_event no_safe_action 30; then
        echo "  · 决策结果：写操作未授权，事件已交接安全运营"
    else
        die "事件已经出现，但 30 秒内没有形成 no_safe_action。检查网关日志和哨兵时间线。"
    fi
    echo "受控事件已记录：12 条失败登录，前端事件链已更新。"
    echo
    echo "保持在“内网实时”页，选择 $CONTROLLED_SOURCE 进入对应事件记录。"
    echo "检测事实：$CONTROLLED_SOURCE 产生重复失败登录记录，来源归属和活动会话待核验。"
    echo "候选动作：临时防火墙封禁。"
    echo "安全门条件：活动会话、管理地址豁免、封禁 TTL、提交后回读、超时自动回滚。"
    echo "决策结果：缺少活动会话证据，写操作未授权，防火墙配置保持原版本。"
    echo "后续责任：安全运营核验来源、活动会话和影响范围后处置。"
    echo
    echo "收尾命令：sudo ./scripts/inject_incident.sh cleanup"
    ;;

recurring)
    sentinel_enabled || die "哨兵未启用，recurring 流程需要后台巡检。
先在 /etc/selfevo-console.env 里设 AUTOPOIESIS_SENTINEL=1 再 systemctl restart $GATEWAY。"

    echo "复发升级流程：重复触发同一故障并记录阈值决策。"
    echo
    echo "生产参数：24 小时窗口，600 秒基础冷却，$DEMO_LIMIT 次复发触发升级。"
    echo "本流程参数：${DEMO_WINDOW} 秒窗口，${DEMO_COOLDOWN} 秒基础冷却，"
    echo "复发阈值保持 $DEMO_LIMIT 次。"
    echo

    # Checked before anything is applied: if the ladder is already at the top,
    # the run is pointless, and it should not cost a gateway restart to find out.
    resolve_timeline
    PRIOR=$(cycles_in_window "$DEMO_WINDOW")
    if (( PRIOR >= DEMO_LIMIT )); then
        die "时间线里 $SUBJECT 在 $((DEMO_WINDOW / 60)) 分钟内已经有 $PRIOR 个「修好又复发」的周期，
当前复发计数已经达到阈值，本轮将直接进入人工升级。

完整执行三次恢复周期需要等待窗口过期（最多 $((DEMO_WINDOW / 60)) 分钟），或按 DEMO.md 执行前检查清理时间线：
  ./scripts/inject_incident.sh cleanup
  rm -f $TIMELINE
  systemctl restart $GATEWAY
时间线是审计日志，清理操作仅用于受控流程初始化。"
    fi
    ACT_ROUNDS=$((DEMO_LIMIT - PRIOR))
    if (( PRIOR > 0 )); then
        echo "注意：时间线里已经有 $PRIOR 个复发周期（$((DEMO_WINDOW / 60)) 分钟窗口内），"
        echo "本次剩余自动处置轮数为 $ACT_ROUNDS；完整流程需要先清理时间线。"
        echo
    fi

    apply_demo_override
    trap revert_on_abort EXIT
    echo

    # 150s a round: ~20s to detect and confirm, ~95s of watch window, 30s pause.
    echo "接下来自动执行 $((ACT_ROUNDS + 1)) 轮，预计 $(( (ACT_ROUNDS * 150 + 100) / 60 )) 分钟："
    echo "  前 $ACT_ROUNDS 轮：杀掉 $UNIT → 哨兵发现 → 确认 → 重启 → 双窗口观察 → 判定恢复"
    echo "  最后 1 轮：触发复发阈值，写操作未授权，记录 escalated 并转人工"
    echo
    echo "操作说明见 scripts/DEMO.md，终端同步输出每个处置阶段。"
    echo

    CURSOR=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
    EARLY_ESCALATION=0
    install_unit

    for (( round = 1; round <= ACT_ROUNDS; round++ )); do
        echo "── 第 $round/$((ACT_ROUNDS + 1)) 次故障（预期：修好）──────────────────────────"
        crash_unit
        # Either terminal ends the round. The ladder can reach its limit earlier
        # than this loop expects because a cycle left by an earlier run
        # counts too, and the count is a property of the log, not of this script.
        # Waiting only for `resolved` turns the mechanism working correctly into
        # a five-minute timeout.
        if follow_until escalated 5 >/dev/null 2>&1; then
            echo "   本轮直接升级：窗口内已有前序复发记录。"
            EARLY_ESCALATION=1
            break
        fi
        follow_until resolved 300 || {
            if follow_until escalated 5 >/dev/null 2>&1; then
                echo "   本轮触发复发阈值：写操作未授权，事件升级人工。"
                EARLY_ESCALATION=1
                break
            fi
            die "这一轮既没走到判定恢复，也没有升级。
先看 ./scripts/inject_incident.sh status，再看 journalctl -u $GATEWAY -n 50。"
        }
        # The cooldown is set when the action starts, and the ladder doubles it
        # each round: 30s, 60s, then 120s against a 90s watch window. Only the
        # third one outlives its own repair, so a short pause here keeps the
        # next kill from landing inside it and being recorded as `cooldown`
        # instead of the next rung.
        echo "   等待 30 秒冷却期结束；冷却期间触发的事件归类为冷却拦截。"
        echo
        sleep 30
    done

    if [ "$EARLY_ESCALATION" = "1" ]; then
        DEMO_COMPLETE=1
        echo
        echo "── 决策结果：复发阈值触发 ────────────────────────────────────────"
        echo
        echo "前端核对字段："
        echo "  · 状态：已升级人工处置"
        echo "  · 决策结果：复发阈值触发，写操作未授权"
        echo "  · 检测事实：复发次数和引用链"
        echo
        echo "$UNIT 保持 failed；后续责任已交接值班人员。"
        echo
        echo "受控流程参数保持生效，cleanup 将恢复生产窗口。"
        echo "收尾命令：./scripts/inject_incident.sh cleanup"
    else

    echo "── 第 $((ACT_ROUNDS + 1))/$((ACT_ROUNDS + 1)) 次故障（预期：拒绝）──────────────────"
    echo "   已记录 $DEMO_LIMIT 个恢复后复发周期，本轮预期触发升级。"
    crash_unit
    if follow_until escalated 180; then
        # From here the compressed window stays until cleanup: the escalated
        # state is what the operator is about to show, and a revert would
        # restart the gateway out from under it.
        DEMO_COMPLETE=1
        echo
        echo "复发阈值流程完成。"
        echo
        echo "前端核对字段："
        echo "  · 状态：已升级人工处置"
        echo "  · 决策结果：复发阈值触发，写操作未授权"
        echo "  · 检测事实：前 $DEMO_LIMIT 次恢复和复发引用链"
        echo
        echo "$UNIT 保持 failed；后续责任已交接值班人员。"
        echo
        echo "受控流程参数保持生效，cleanup 将恢复生产窗口。"
        echo "收尾命令：./scripts/inject_incident.sh cleanup"
    else
        echo
        # 覆盖在 apply 时已经对着进程校验过，所以这里基本不会是覆盖的问题。
        echo "未收到 escalated；受控流程参数将自动回滚。按顺序检查：" >&2
        echo "  1. 后端记下这个事件了吗： grep '\"kind\": \"escalated\"' $TIMELINE" >&2
        echo "  2. 前几轮真的闭环了吗：   grep -c '\"kind\": \"resolved\"' $TIMELINE  应该 ≥ $DEMO_LIMIT" >&2
        echo "  3. 检查第四轮是否为 cooldown；若命中，调整 DEMO_COOLDOWN 后重试" >&2
        exit 1
    fi
    fi
    ;;

status)
    # `is-active` prints "inactive" and exits non-zero for a unit that was never
    # installed, so ask about the file first rather than reporting a state for
    # something that does not exist.
    if [[ -f "$UNIT_FILE" ]]; then
        echo "注入单元：$(systemctl is-active "$UNIT" 2>/dev/null || true)"
        echo "单元文件：存在"
    else
        echo "注入单元：未安装"
        echo "单元文件：无"
    fi
    echo "近期该来源的失败登录：$(journalctl --since -30m --no-pager 2>/dev/null | grep -c "$CONTROLLED_SOURCE" || true) 条"
    echo
    echo "复发阶梯（recurring 场景）"
    if [[ -f "$DROPIN_FILE" || -f "$DEMO_ENV_FILE" ]]; then
        echo "  受控流程参数：已安装"
        [[ -f "$DROPIN_FILE" ]]   && echo "    drop-in   $DROPIN_FILE" \
                                  || echo "    drop-in   缺失（$DROPIN_FILE），受控流程参数无法加载"
        [[ -f "$DEMO_ENV_FILE" ]] && echo "    env 文件  $DEMO_ENV_FILE" \
                                  || echo "    env 文件  缺失（$DEMO_ENV_FILE），受控流程参数无法加载"
    else
        echo "  受控流程参数：未安装（生产配置）"
    fi
    WINDOW_NOW=$(gateway_env AUTOPOIESIS_RECURRENCE_WINDOW)
    LIMIT_NOW=$(gateway_env AUTOPOIESIS_RECURRENCE_LIMIT)
    COOLDOWN_NOW=$(gateway_env AUTOPOIESIS_SENTINEL_COOLDOWN)
    INTERVAL_NOW=$(gateway_env AUTOPOIESIS_SENTINEL_INTERVAL)
    echo "  网关进程当前环境（进程启动时加载）："
    echo "    窗口   ${WINDOW_NOW:-未设置 → 代码默认 86400}s"
    echo "    阈值   ${LIMIT_NOW:-未设置 → 代码默认 3} 次"
    echo "    冷却   ${COOLDOWN_NOW:-未设置 → 代码默认 600}s（基础值，每复发一次翻倍）"
    echo "    巡检   ${INTERVAL_NOW:-未设置 → 代码默认 20}s"
    if [[ -f "$DROPIN_FILE" && "$WINDOW_NOW" != "$DEMO_WINDOW" ]]; then
        echo "    ⚠ 文件在但进程没读到：需要 systemctl restart $GATEWAY"
    fi
    resolve_timeline
    echo "  时间线：$TIMELINE"
    if [[ -f "$TIMELINE" ]]; then
        WINDOW_FOR_COUNT=${WINDOW_NOW:-86400}
        [[ "$WINDOW_FOR_COUNT" =~ ^[0-9]+$ ]] || WINDOW_FOR_COUNT=86400
        echo "    $SUBJECT 在窗口内「修好又复发」的周期：$(cycles_in_window "$WINDOW_FOR_COUNT") 次（$((WINDOW_FOR_COUNT / 60)) 分钟窗口，阈值 ${LIMIT_NOW:-3} 次时拒绝）"
        # grep -c prints 0 and exits 1 when nothing matches, so `|| true`, not
        # `|| echo 0`, which prints the zero twice.
        echo "    已记录的 escalated：$(grep -c '"kind": "escalated"' "$TIMELINE" 2>/dev/null || true) 条"
    else
        echo "    还没有时间线文件（哨兵没跑过，或者被清过）"
    fi
    ;;

cleanup)
    systemctl stop "$UNIT" 2>/dev/null || true
    systemctl disable "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    rm -f "$RUN_STATE"
    systemctl daemon-reload
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    echo "已清理 $UNIT。本轮注入日志保留在 journal 审计记录中，并按系统轮转策略到期。"
    # Done second and on purpose: the gateway restart brings the sentinel back,
    # and it should come back to a box where the demo unit is already gone.
    if revert_demo_override; then
        echo "时间线作为审计日志继续保留；窗口内重跑 recurring 会沿用当前复发计数，"
        echo "status 会显示剩余级数；完整流程初始化步骤见 DEMO.md 的执行前检查。"
    fi
    ;;

*)
    # The header comment block is the usage text: everything from line 2 down to
    # the first line that is not a comment, so it cannot drift out of sync with
    # the scenarios below it.
    awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"
    exit 1
    ;;
esac
