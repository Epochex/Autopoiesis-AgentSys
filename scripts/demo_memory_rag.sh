#!/usr/bin/env bash
# Terminal-only controller for the live memory + knowledge retrieval demo.
# It adds no frontend control.  The operator runs it over Tailscale, then watches
# the ordinary production pages consume the resulting detector and memory data.
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
API=http://127.0.0.1:8026/api/rca
SUBJECT=demo-collector.service
MARKER=/run/autopoiesis-memory-rag-demo.paused
ACTOR=memory-rag-demo

die() { echo "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "需要 root：演示会暂停自动写操作并制造一个隔离服务故障。"

post() {
    local path=$1 body=$2
    curl -fsS -m 15 -X POST "$API/$path" \
        -H 'Content-Type: application/json' -H 'Accept: application/json' \
        --data "$body"
}

safety_json() { curl -fsS -m 10 "$API/remediation/safety"; }

paused_field() {
    safety_json | python3 -c 'import json,sys; print(str(bool(json.load(sys.stdin)["emergency_stop"]["paused"])).lower())'
}

pause_actor() {
    safety_json | python3 -c 'import json,sys; print(json.load(sys.stdin)["emergency_stop"].get("actor") or "")'
}

memory_ready() {
    python3 - <<'PY'
import json, urllib.request
payload = json.load(urllib.request.urlopen(
    "http://127.0.0.1:8026/api/rca/memory?limit=500&include_quarantined=false",
    timeout=10,
))
records = {row["memory_id"]: row for row in payload.get("records", [])}
procedure = records.get("proc-sentinel.failed_units") or {}
semantic = records.get("sem-sentinel.failed_units") or {}
ready = (
    "skill:failed_services" in procedure.get("tags", [])
    and bool(semantic)
)
if ready:
    print("ready")
else:
    raise SystemExit(1)
PY
}

resume_if_owned() {
    if [[ -f "$MARKER" ]] || [[ "$(pause_actor)" == "$ACTOR" ]]; then
        post remediation/resume \
            '{"actor":"memory-rag-demo","reason":"记忆与知识检索演示结束，恢复有界自动处置"}' \
            >/dev/null
        rm -f "$MARKER"
        echo "已恢复自动处置入口。"
    fi
}

arm() {
    curl -fsS -m 10 http://127.0.0.1:8026/api/healthz >/dev/null \
        || die "网关不可达。"
    memory_ready >/dev/null || die "缺少可检索的 failed_units 程序性记忆。先完整跑一次 Demo 1 的 service-down，等恢复结束和记忆刷新完成。"
    [[ "$(paused_field)" == false ]] \
        || die "全局写操作已经处于暂停状态，actor=$(pause_actor)。先确认暂停来源，脚本不会覆盖别人的安全开关。"

    post remediation/pause \
        '{"actor":"memory-rag-demo","reason":"保留 failed 单元供只读调查，防止哨兵先于页面调查完成重启"}' \
        >/dev/null
    touch "$MARKER"
    trap 'resume_if_owned >/dev/null || true' ERR INT TERM

    "$ROOT_DIR/scripts/inject_incident.sh" service-down
    trap - ERR INT TERM

    echo
    echo "记忆 + 知识检索演示已布置："
    echo "  1. 自动写操作已暂停，检测和只读调查继续工作。"
    echo "  2. $SUBJECT 保持 failed，避免调查开始前被恢复。"
    echo "  3. 等态势页出现该对象，点入对应态势记录。"
    echo "  4. 点『看处置链路』进入诊断处置，调查会自动开始。"
    echo "  5. 看『本次调查上下文回执』：记忆命中、探针顺序、RAG 文档和 influence。"
    echo
    echo "看完执行：sudo $ROOT_DIR/scripts/demo_memory_rag.sh resume"
}

resume_demo() {
    resume_if_owned
    echo "$SUBJECT 当前状态：$(systemctl is-active "$SUBJECT" 2>/dev/null || true)"
    echo "后台哨兵会在后续巡检中重新评估；Demo 1 已经展示过完整处置时，可以直接 cleanup 收尾。"
}

status() {
    local service_state
    service_state=$(systemctl is-active "$SUBJECT" 2>/dev/null || true)
    [[ -n "$service_state" ]] || service_state="未安装"
    echo "自动写操作暂停：$(paused_field)"
    echo "暂停 actor：$(pause_actor)"
    echo "本脚本 marker：$([[ -f "$MARKER" ]] && echo 存在 || echo 无)"
    echo "$SUBJECT：$service_state"
    if memory_ready >/dev/null 2>&1; then
        echo "failed_units 记忆：可用于探针排序"
    else
        echo "failed_units 记忆：尚未就绪"
    fi
}

cleanup() {
    "$ROOT_DIR/scripts/inject_incident.sh" cleanup
    resume_if_owned
    echo "演示服务和本脚本暂停状态已收回。"
}

case "${1:-}" in
    arm) arm ;;
    resume) resume_demo ;;
    status) status ;;
    cleanup) cleanup ;;
    *)
        echo "用法：sudo $0 {arm|resume|status|cleanup}" >&2
        exit 2
        ;;
esac
