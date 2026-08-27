#!/usr/bin/env bash
# 真实运维记忆闭环演示。只读取线上接口并调用 inject_incident.sh 制造真实本机故障。
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INJECT="$ROOT_DIR/scripts/inject_incident.sh"
API_BASE=${AUTOPOIESIS_DEMO_API:-http://127.0.0.1:8026}
ATTACK_GROWTH_TIMEOUT=${AUTOPOIESIS_DEMO_ATTACK_TIMEOUT:-180}
ATTACK_SAMPLE_INTERVAL=${AUTOPOIESIS_DEMO_ATTACK_INTERVAL:-15}
TIMELINE_TIMEOUT=${AUTOPOIESIS_DEMO_TIMELINE_TIMEOUT:-420}
SUBJECT=demo-collector.service
FIREWALL=FG100ETK20014183
GATEWAY_UNIT=netops-ops-console-backend.service

TMP_DIR=$(mktemp -d /tmp/autopoiesis-memory-demo.XXXXXX)
CONTROL_BACKUP="$TMP_DIR/emergency-stop.backup"
CONTROL_PATH=""
CONTROL_MOVED=0
PAUSE_ISSUED=0
INCIDENT_TOUCHED=0
FINISHED=0

die() {
    echo >&2
    echo "演示失败：$*" >&2
    exit 1
}

cleanup_on_exit() {
    local rc=$?
    set +e
    if (( CONTROL_MOVED )) && [[ -n "$CONTROL_PATH" && -e "$CONTROL_BACKUP" ]]; then
        mv -f -- "$CONTROL_BACKUP" "$CONTROL_PATH"
        CONTROL_MOVED=0
        echo "异常收尾：已原样恢复急停状态文件。" >&2
    fi
    if (( PAUSE_ISSUED )) && [[ -n "${RESUME_PATH:-}" ]]; then
        curl -fsS -m 15 -X POST "${API_BASE}${RESUME_PATH}" \
            -H 'Content-Type: application/json' \
            -d '{"actor":"memory-demo-cleanup","reason":"演示异常退出，恢复动作入口"}' \
            >/dev/null 2>&1 || true
        echo "异常收尾：已请求恢复动作入口。" >&2
    fi
    if (( INCIDENT_TOUCHED )) && (( ! FINISHED )); then
        "$INJECT" cleanup >/dev/null 2>&1 || true
        echo "异常收尾：已请求清理本机演示故障与复发时间压缩配置。" >&2
    fi
    rm -rf -- "$TMP_DIR"
    exit "$rc"
}
trap cleanup_on_exit EXIT

幕() {
    echo
    echo "=============================================================================="
    echo "$1"
    echo "=============================================================================="
}

敲() { echo; echo "敲什么：$*"; }
说() { echo; echo "说：$*"; }

get_json() {
    local path=$1 output=$2 show=${3:-1}
    敲 "curl -fsS '${API_BASE}${path}'"
    curl -fsS -m 30 "${API_BASE}${path}" -o "$output" \
        || die "GET $path 没有返回成功响应"
    python3 - "$output" <<'PY' || die "GET $path 返回的不是 ok JSON"
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("ok") is False:
    raise SystemExit(f"接口返回 ok=false: {payload}")
PY
    if [[ "$show" == 1 ]]; then
        echo "屏幕/接口出什么："
        python3 -m json.tool "$output"
    fi
}

post_json() {
    local path=$1 body=$2 output=$3 show=${4:-1} timeout=${5:-30}
    敲 "curl -fsS -X POST '${API_BASE}${path}' -H 'Content-Type: application/json' -d '$body'"
    curl -fsS -m "$timeout" -X POST "${API_BASE}${path}" \
        -H 'Content-Type: application/json' -d "$body" -o "$output" \
        || die "POST $path 没有返回成功响应"
    python3 - "$output" <<'PY' || die "POST $path 返回的不是 ok JSON"
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("ok") is False:
    raise SystemExit(f"接口返回 ok=false: {payload}")
PY
    if [[ "$show" == 1 ]]; then
        echo "屏幕/接口出什么："
        python3 -m json.tool "$output"
    fi
}

path_exists() {
    local path=$1
    python3 - "$TMP_DIR/openapi.json" "$path" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    paths = json.load(handle).get("paths", {})
raise SystemExit(0 if sys.argv[2] in paths else 1)
PY
}

choose_path() {
    local preferred=$1 fallback=$2
    if path_exists "$preferred"; then
        printf '%s\n' "$preferred"
    elif path_exists "$fallback"; then
        printf '%s\n' "$fallback"
    else
        die "OpenAPI 中既没有 $preferred，也没有 $fallback"
    fi
}

control_state_assert() {
    local file=$1 want_paused=$2 want_fail_closed=${3:-any}
    python3 - "$file" "$want_paused" "$want_fail_closed" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
state = payload.get("emergency_stop") or payload.get("safety") or payload
want_paused = sys.argv[2] == "true"
if state.get("paused") is not want_paused:
    raise SystemExit(f"急停状态不符，期望 paused={want_paused}，实际={state}")
if sys.argv[3] != "any":
    want_fail_closed = sys.argv[3] == "true"
    if state.get("fail_closed") is not want_fail_closed:
        raise SystemExit(
            f"fail_closed 状态不符，期望 {want_fail_closed}，实际={state}"
        )
print(json.dumps(state, ensure_ascii=False, indent=2))
PY
}

urlencode() {
    python3 - "$1" <<'PY'
import sys
from urllib.parse import quote
print(quote(sys.argv[1], safe=""))
PY
}

refresh_operational_memory() {
    post_json "/api/rca/operational-memory/refresh" '{}' "$1" 0 90
}

fetch_subject_memory() {
    local subject=$1 output=$2 show=${3:-0}
    get_json "/api/rca/operational-memory?subject=$(urlencode "$subject")" "$output" "$show"
}

admin_risk_count() {
    python3 - "$1" "$FIREWALL" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("durable") is not True:
    raise SystemExit("operational-memory 没有使用持久化存储，不能演示长期记忆")
rows = [
    row for row in payload.get("risks", [])
    if row.get("title") == "admin_login_failed"
    and row.get("source") == "real"
    and (row.get("scope") == sys.argv[2] or sys.argv[2] in str(row))
]
if not rows:
    raise SystemExit(
        f"没有找到 {sys.argv[2]} 上 source=real 的 admin_login_failed 风险"
    )
row = max(rows, key=lambda item: int(item.get("evidence_count") or 0))
count = row.get("evidence_count")
if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
    raise SystemExit(f"evidence_count 不是正整数: {row}")
print(count)
PY
}

show_admin_risk() {
    python3 - "$1" "$FIREWALL" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("durable") is not True:
    raise SystemExit("operational-memory 没有使用持久化存储，不能演示长期记忆")
rows = [
    row for row in payload.get("risks", [])
    if row.get("title") == "admin_login_failed"
    and row.get("source") == "real"
    and (row.get("scope") == sys.argv[2] or sys.argv[2] in str(row))
]
if not rows:
    raise SystemExit("真实 admin_login_failed 风险缺失")
row = max(rows, key=lambda item: int(item.get("evidence_count") or 0))
print(json.dumps(row, ensure_ascii=False, indent=2))
PY
}

timeline_has() {
    local file=$1 since=$2 terminal=$3
    python3 - "$file" "$since" "$SUBJECT" "$terminal" <<'PY'
import json, sys
from datetime import datetime
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
since = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
for row in payload.get("events", []):
    if row.get("subject") != sys.argv[3] or row.get("kind") != sys.argv[4]:
        continue
    try:
        at = datetime.fromisoformat(str(row.get("at", "")).replace("Z", "+00:00"))
    except ValueError:
        continue
    if at >= since:
        raise SystemExit(0)
raise SystemExit(1)
PY
}

wait_for_timeline() {
    local since=$1 terminal=$2 timeout=$3 output=$4
    local deadline=$((SECONDS + timeout))
    echo "屏幕/接口出什么：等待 $SUBJECT 出现 $terminal，轮询的是实时审计时间线。"
    while (( SECONDS < deadline )); do
        curl -fsS -m 30 "${API_BASE}/api/rca/sentinel/timeline?limit=2000" -o "$output" \
            || die "轮询 sentinel/timeline 失败"
        if timeline_has "$output" "$since" "$terminal"; then
            echo "已看到 $terminal。"
            return 0
        fi
        printf '.'
        sleep 5
    done
    echo
    die "等待 $terminal 超过 ${timeout}s；请检查 sentinel 是否开启及网关日志"
}

resolve_control_file() {
    local pid configured
    pid=$(systemctl show netops-ops-console-backend -p MainPID --value 2>/dev/null || true)
    [[ -n "$pid" && "$pid" != 0 ]] || die "无法取得网关 MainPID，不能安全定位急停状态文件"
    configured=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
        | sed -n 's/^AUTOPOIESIS_REMEDIATION_STOP=//p' | head -1)
    CONTROL_PATH=${configured:-/data/autopoiesis-runtime/remediation-emergency-stop.json}
    [[ "$CONTROL_PATH" == /* ]] || die "急停状态路径不是绝对路径：$CONTROL_PATH"
    [[ "$CONTROL_PATH" != / && "$CONTROL_PATH" != /data && "$CONTROL_PATH" != /tmp ]] \
        || die "拒绝操作过宽的急停状态路径：$CONTROL_PATH"
}

command -v curl >/dev/null || die "缺少 curl"
command -v python3 >/dev/null || die "缺少 python3"
command -v systemctl >/dev/null || die "缺少 systemctl"
[[ -x "$INJECT" ]] || die "$INJECT 不可执行"
[[ $EUID -eq 0 ]] || die "需要 root，真实故障注入和急停缺失验证都要访问 systemd 状态"

敲 "curl -fsS '${API_BASE}/api/openapi.json'"
curl -fsS -m 30 "${API_BASE}/api/openapi.json" -o "$TMP_DIR/openapi.json" \
    || die "无法读取运行中服务的 OpenAPI"
SAFETY_PATH=$(choose_path "/api/rca/safety" "/api/rca/remediation/safety")
PAUSE_PATH=$(choose_path "/api/rca/pause" "/api/rca/remediation/pause")
RESUME_PATH=$(choose_path "/api/rca/resume" "/api/rca/remediation/resume")
EMERGENCY_PATH=""
if path_exists "/api/rca/emergency-stop"; then
    EMERGENCY_PATH=/api/rca/emergency-stop
fi
echo "屏幕/接口出什么：控制状态=$SAFETY_PATH，暂停=$PAUSE_PATH，恢复=$RESUME_PATH"
if [[ -n "$EMERGENCY_PATH" ]]; then
    echo "屏幕/接口出什么：独立急停入口=$EMERGENCY_PATH"
else
    echo "屏幕/接口出什么：当前服务把急停状态实现于 remediation pause/resume，没有独立 POST 路由。"
fi

get_json "/api/healthz" "$TMP_DIR/health.json" 0
get_json "$SAFETY_PATH" "$TMP_DIR/safety-start.json" 0
control_state_assert "$TMP_DIR/safety-start.json" false false >/dev/null \
    || die "开演前动作入口不是已恢复状态；保留现场并退出"

幕 "第一幕【长期记忆·真实攻击】"
refresh_operational_memory "$TMP_DIR/refresh-attack-1.json"
fetch_subject_memory "$FIREWALL" "$TMP_DIR/attack-1.json"
echo "屏幕/接口出什么："
show_admin_risk "$TMP_DIR/attack-1.json" || die "第一幕真实攻击锚点缺失"
FIRST_COUNT=$(admin_risk_count "$TMP_DIR/attack-1.json") \
    || die "无法读取第一次数量"

deadline=$((SECONDS + ATTACK_GROWTH_TIMEOUT))
LATEST_COUNT=$FIRST_COUNT
while (( SECONDS < deadline )); do
    sleep "$ATTACK_SAMPLE_INTERVAL"
    refresh_operational_memory "$TMP_DIR/refresh-attack-next.json"
    fetch_subject_memory "$FIREWALL" "$TMP_DIR/attack-next.json"
    LATEST_COUNT=$(admin_risk_count "$TMP_DIR/attack-next.json") \
        || die "刷新后真实攻击锚点缺失"
    echo "本次刷新：evidence_count=$LATEST_COUNT"
    if (( LATEST_COUNT > FIRST_COUNT )); then
        break
    fi
done
(( LATEST_COUNT > FIRST_COUNT )) \
    || die "${ATTACK_GROWTH_TIMEOUT}s 内证据数没有增长，不能把‘仍在增长’讲给观众"
show_admin_risk "$TMP_DIR/attack-next.json"
说 "这是这台防火墙此刻真实发生的登录爆破。系统把当前接口读到的 ${LATEST_COUNT} 条分散日志聚合成一条持续更新的风险记录；source=real、起止时间、趋势和证据数都来自刚才两次真实刷新。"

幕 "第二幕【故障档案·不伪造】"
敲 "curl -fsS '${API_BASE}/api/rca/operational-memory?subject=$(urlencode "$FIREWALL")'"
echo "屏幕/接口出什么："
python3 - "$TMP_DIR/attack-next.json" "$FIREWALL" <<'PY' \
    || die "没有找到保持假设状态的真实攻击档案"
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
rows = [
    row for row in payload.get("dossiers", [])
    if "admin_login_failed campaign" in str(row.get("title"))
    and sys.argv[2] in str(row.get("title"))
    and row.get("source") in {"real", "live"}
]
if not rows:
    raise SystemExit("真实攻击档案不存在")
row = rows[0]
if row.get("status") != "open":
    raise SystemExit(f"档案已经不是 open，不能讲待确认假设: {row}")
if "causal confirmation" not in str(row.get("reason", "")):
    raise SystemExit(f"档案没有公开因果确认边界: {row}")
print(json.dumps(row, ensure_ascii=False, indent=2))
print("根因状态：待确认假设（open；reason 明确要求 causal confirmation）")
PY
说 "探测器给出的名字只是一条待确认假设。档案保持 open，reason 明确要求独立因果确认；只有人工确认或独立证据支持后，结论才有资格进入长期知识。"

幕 "第三幕【自愈闭环】"
SERVICE_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
INCIDENT_TOUCHED=1
敲 "cd '$ROOT_DIR' && ./scripts/inject_incident.sh service-down"
(cd "$ROOT_DIR" && "$INJECT" service-down) \
    || die "service-down 真实故障注入失败"
敲 "curl -fsS '${API_BASE}/api/rca/sentinel/timeline?limit=2000'"
wait_for_timeline "$SERVICE_STARTED_AT" resolved "$TIMELINE_TIMEOUT" "$TMP_DIR/service-timeline.json"
echo "屏幕/接口出什么："
python3 - "$TMP_DIR/service-timeline.json" "$SERVICE_STARTED_AT" "$SUBJECT" <<'PY' \
    || die "自愈时间线缺少必需阶段或双观察窗口证据"
import json, sys
from datetime import datetime
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
since = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
rows = []
for row in payload.get("events", []):
    if row.get("subject") != sys.argv[3]:
        continue
    try:
        at = datetime.fromisoformat(str(row.get("at", "")).replace("Z", "+00:00"))
    except ValueError:
        continue
    if at >= since:
        rows.append(row)
kinds = [row.get("kind") for row in rows]
required = {"detected", "awaiting_confirmation", "preflight", "command", "remediated", "resolved"}
missing = sorted(required - set(kinds))
if missing:
    raise SystemExit(f"时间线缺少阶段: {missing}; 实际={kinds}")
if sum(kind == "detected" for kind in kinds) < 2:
    raise SystemExit("没有看到连续两次检测确认")
if not any(row.get("kind") == "preflight" and row.get("eligible") is True for row in rows):
    raise SystemExit("前置校验没有放行")
if not any(row.get("kind") == "command" and "restart" in " ".join(map(str, row.get("argv", []))) for row in rows):
    raise SystemExit("没有真实 restart 命令回执")
remediated = next(row for row in reversed(rows) if row.get("kind") == "remediated")
resolved = next(row for row in reversed(rows) if row.get("kind") == "resolved")
if remediated.get("outcome") != "passed" or resolved.get("outcome") != "passed":
    raise SystemExit("处置没有通过回读")
for key in ("fast_samples", "stability_samples"):
    if not isinstance(remediated.get(key), int) or remediated[key] <= 0:
        raise SystemExit(f"缺少 {key}: {remediated}")
for key in ("fast_window_seconds", "stability_window_seconds"):
    if not isinstance(resolved.get(key), (int, float)) or resolved[key] <= 0:
        raise SystemExit(f"缺少 {key}: {resolved}")
print(json.dumps(rows, ensure_ascii=False, indent=2))
print("闭环摘要:")
print("  fast_window_seconds=", resolved["fast_window_seconds"])
print("  stability_window_seconds=", resolved["stability_window_seconds"])
print("  fast_samples=", remediated["fast_samples"])
print("  stability_samples=", remediated["stability_samples"])
print("  execution_id=", resolved.get("execution_id"))
PY

refresh_operational_memory "$TMP_DIR/refresh-service.json"
fetch_subject_memory "$SUBJECT" "$TMP_DIR/service-memory.json"
get_json "/api/rca/remediation/runs?limit=100" "$TMP_DIR/remediation-runs.json" 0
echo "屏幕/接口出什么：故障档案与真实动作回执的同一执行标识。"
python3 - "$TMP_DIR/service-memory.json" "$TMP_DIR/service-timeline.json" \
    "$TMP_DIR/remediation-runs.json" "$SERVICE_STARTED_AT" "$SUBJECT" <<'PY' \
    || die "处置回执没有进入可审计故障档案链"
import json, sys
from datetime import datetime
memory, timeline, runs = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:4])
since = datetime.fromisoformat(sys.argv[4].replace("Z", "+00:00"))
subject = sys.argv[5]
dossiers = [row for row in memory.get("dossiers", []) if subject in str(row)]
if not dossiers:
    raise SystemExit("operational-memory 中没有本轮故障档案")
dossier = next((row for row in dossiers if row.get("status") == "resolved"), None)
if dossier is None or int(dossier.get("evidence_count") or 0) <= 0:
    raise SystemExit(f"档案未 resolved 或没有证据: {dossiers}")
resolved = []
for row in timeline.get("events", []):
    if row.get("subject") != subject or row.get("kind") != "resolved":
        continue
    try:
        if datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00")) >= since:
            resolved.append(row)
    except (KeyError, ValueError):
        pass
execution_id = resolved[-1].get("execution_id") if resolved else None
if not execution_id:
    raise SystemExit("resolved 时间线没有 execution_id")
receipts = [row for row in runs.get("runs", []) if row.get("execution_id") == execution_id]
if not receipts:
    raise SystemExit(f"remediation/runs 找不到 execution_id={execution_id}")
print(json.dumps({"dossier": dossier, "action_receipt": receipts[0]}, ensure_ascii=False, indent=2))
PY
说 "同一条链从连续检测、确认、前置校验、真实 restart 命令，走过快窗口和稳定窗口回读后才标记恢复。execution_id 同时出现在时间线和动作回执里；刷新后，resolved 故障档案带着本轮证据进入运维记忆。当前可自动处置范围只限本机 L1，远程设备保持只读分析并转人工。"

幕 "第四幕【记忆改变动作】"
敲 "cd '$ROOT_DIR' && ./scripts/inject_incident.sh recurring"
(cd "$ROOT_DIR" && "$INJECT" recurring) \
    || die "recurring 真实复发演示失败"
get_json "/api/rca/sentinel/recurrence" "$TMP_DIR/recurrence.json" 0
get_json "/api/rca/sentinel/timeline?limit=2000" "$TMP_DIR/recurrence-timeline.json" 0
echo "屏幕/接口出什么："
python3 - "$TMP_DIR/recurrence.json" "$TMP_DIR/recurrence-timeline.json" "$SUBJECT" <<'PY' \
    || die "复发投影没有形成拒绝与前三次引用链"
import json, sys
projection = json.load(open(sys.argv[1], encoding="utf-8"))
timeline = json.load(open(sys.argv[2], encoding="utf-8"))
subject = sys.argv[3]
rows = [row for row in projection.get("keys", []) if row.get("subject") == subject]
if not rows:
    raise SystemExit("recurrence 中没有演示服务")
row = rows[0]
limit = projection.get("limit")
if not isinstance(limit, int) or limit <= 0:
    raise SystemExit(f"无效复发阈值: {limit}")
if row.get("escalated") is not True or int(row.get("recurrences") or 0) < limit:
    raise SystemExit(f"没有达到拒绝条件: {row}")
if len(row.get("cycles") or []) < limit:
    raise SystemExit(f"引用链短于阈值: {row}")
escalated = [event for event in timeline.get("events", []) if event.get("subject") == subject and event.get("kind") == "escalated"]
if not escalated or len(escalated[-1].get("prior_cycles") or []) < limit:
    raise SystemExit("escalated 时间线没有列出先前闭环引用")
print(json.dumps({
    "window_sec": projection.get("window_sec"),
    "limit": limit,
    "repair_then_recur_cycles": row.get("cycles"),
    "refusal": escalated[-1],
}, ensure_ascii=False, indent=2))
print(f"现场读法：前 {limit} 次处置修好后又复发，第 {limit + 1} 次请求被拒绝并转人工。")
PY
说 "这是记忆真正改变动作的地方。系统从审计时间线重建同一对象、同一动作的修好后复发链；达到接口现场给出的阈值后，下一次不再执行，拒绝原因旁边直接列出此前每次恢复和再次故障的时间。当前可自动处置范围只限本机 L1，远程设备保持只读分析并转人工。"

幕 "第五幕【自动处置有界·安全门】"
get_json "/api/rca/remediation/actions" "$TMP_DIR/actions.json" 0
get_json "/api/rca/remediation/safety" "$TMP_DIR/remediation-safety.json" 0
fetch_subject_memory "$FIREWALL" "$TMP_DIR/action-scope.json"
echo "屏幕/接口出什么："
python3 - "$TMP_DIR/actions.json" "$TMP_DIR/remediation-safety.json" "$TMP_DIR/action-scope.json" <<'PY' \
    || die "动作目录、预算或远程只读边界与演示口径不符"
import json, sys
actions, safety, memory = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:])
rows = actions.get("actions", [])
names = {row.get("name") for row in rows}
if names != {"restart_unit", "bounce_interface"}:
    raise SystemExit(f"自动动作闭集发生变化: {names}")
if any((row.get("policy") or {}).get("level") != "L1" for row in rows):
    raise SystemExit(f"动作目录出现非 L1 自动动作: {rows}")
limit = (safety.get("limits") or {}).get("max_actions_per_incident")
if not isinstance(limit, int) or limit <= 0:
    raise SystemExit(f"动作预算缺失: {safety}")
scope = memory.get("action_scope") or {}
if "remote firewall" not in scope.get("escalation_only", []):
    raise SystemExit(f"远程设备没有保持 escalation_only: {scope}")
print(json.dumps({
    "automatic_action_count": len(rows),
    "actions": rows,
    "max_actions_per_incident": limit,
    "budget": safety.get("budget"),
    "domain_locks": safety.get("domain_locks"),
    "action_scope": scope,
}, ensure_ascii=False, indent=2))
PY

post_json "/api/rca/remediation/preflight" \
    "{\"action\":\"restart_unit\",\"target\":\"$GATEWAY_UNIT\"}" \
    "$TMP_DIR/healthy-preflight.json" 1
python3 - "$TMP_DIR/healthy-preflight.json" <<'PY' \
    || die "健康服务的前置校验没有正确拒绝"
import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
if p.get("eligible") is not False or "only a failed unit qualifies" not in str(p.get("reason")):
    raise SystemExit(f"健康目标拒绝结果不符: {p}")
PY

post_json "$PAUSE_PATH" \
    '{"actor":"memory-demo","reason":"演示全局暂停会挡住后续动作"}' \
    "$TMP_DIR/paused.json" 1
PAUSE_ISSUED=1
control_state_assert "$TMP_DIR/paused.json" true false >/dev/null \
    || die "pause 没有进入 paused=true"
post_json "/api/rca/remediation/preflight" \
    "{\"action\":\"restart_unit\",\"target\":\"$SUBJECT\"}" \
    "$TMP_DIR/paused-preflight.json" 1
python3 - "$TMP_DIR/paused-preflight.json" <<'PY' \
    || die "暂停后动作请求没有被挡住"
import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
if p.get("eligible") is not False or p.get("refused") is not True:
    raise SystemExit(f"暂停未阻断动作: {p}")
if "pause" not in str(p.get("reason", "")).lower():
    raise SystemExit(f"拒绝原因没有指向全局暂停: {p}")
PY
post_json "$RESUME_PATH" \
    '{"actor":"memory-demo","reason":"暂停分支验证完成，恢复动作入口"}' \
    "$TMP_DIR/resumed.json" 1
PAUSE_ISSUED=0
control_state_assert "$TMP_DIR/resumed.json" false false >/dev/null \
    || die "resume 没有恢复 paused=false"

if [[ -n "$EMERGENCY_PATH" ]]; then
    post_json "$EMERGENCY_PATH" \
        '{"actor":"memory-demo","reason":"演示独立急停入口"}' \
        "$TMP_DIR/emergency-post.json" 1
    PAUSE_ISSUED=1
    get_json "$SAFETY_PATH" "$TMP_DIR/emergency-status.json" 0
    control_state_assert "$TMP_DIR/emergency-status.json" true any >/dev/null \
        || die "独立 emergency-stop 没有阻断动作"
    post_json "$RESUME_PATH" \
        '{"actor":"memory-demo","reason":"独立急停入口验证完成"}' \
        "$TMP_DIR/emergency-resumed.json" 0
    PAUSE_ISSUED=0
fi

resolve_control_file
[[ -f "$CONTROL_PATH" ]] || die "resume 后急停状态文件仍不存在，无法进行可恢复的缺失验证"
敲 "mv '$CONTROL_PATH' '$CONTROL_BACKUP'  # 暂存原文件，退出陷阱保证恢复"
mv -- "$CONTROL_PATH" "$CONTROL_BACKUP"
CONTROL_MOVED=1
get_json "/api/rca/remediation/safety" "$TMP_DIR/fail-closed.json" 1
control_state_assert "$TMP_DIR/fail-closed.json" true true >/dev/null \
    || die "急停状态文件缺失时没有 fail-closed"
post_json "/api/rca/remediation/preflight" \
    "{\"action\":\"restart_unit\",\"target\":\"$SUBJECT\"}" \
    "$TMP_DIR/fail-closed-preflight.json" 1
python3 - "$TMP_DIR/fail-closed-preflight.json" <<'PY' \
    || die "fail-closed 没有阻断动作"
import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
if p.get("eligible") is not False or p.get("refused") is not True:
    raise SystemExit(f"状态文件缺失时动作未被拒绝: {p}")
if "state_read_error" not in str(p.get("reason", "")):
    raise SystemExit(f"拒绝原因没有指出状态读取错误: {p}")
PY
敲 "mv '$CONTROL_BACKUP' '$CONTROL_PATH'"
mv -- "$CONTROL_BACKUP" "$CONTROL_PATH"
CONTROL_MOVED=0
get_json "/api/rca/remediation/safety" "$TMP_DIR/safety-restored.json" 0
control_state_assert "$TMP_DIR/safety-restored.json" false false >/dev/null \
    || die "急停状态文件恢复后状态不正确"
说 "自动动作目录和预算都以刚才接口现场值为准。健康服务会被前置校验拒绝，全局 pause 会挡住后续动作，状态文件缺失会 fail-closed。低风险动作自动执行，首次恶化进入回退或转人工，预算耗尽后停止。当前自动范围只限本机 L1；远程防火墙、交换机和终端保持只读分析并转人工。"

幕 "第六幕【收尾 cleanup】"
敲 "cd '$ROOT_DIR' && ./scripts/inject_incident.sh cleanup"
(cd "$ROOT_DIR" && "$INJECT" cleanup) \
    || die "cleanup 失败"
INCIDENT_TOUCHED=0
get_json "$SAFETY_PATH" "$TMP_DIR/safety-final.json" 0
control_state_assert "$TMP_DIR/safety-final.json" false false >/dev/null \
    || die "收尾后动作入口没有恢复"
敲 "cd '$ROOT_DIR' && ./scripts/inject_incident.sh status"
(cd "$ROOT_DIR" && "$INJECT" status) \
    || die "cleanup 后状态检查失败"
FINISHED=1
说 "演示结束。刚才展示的是机制在真实攻击、真实本机故障、真实拒绝和真实审计数据上的运转。长期收益需要后续真实运行持续累积，本次演示没有给出未经长期测量的节省数字。"
