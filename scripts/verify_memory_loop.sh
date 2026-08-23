#!/usr/bin/env bash
# 在真机上证明一条完整链路：故障被哨兵处置后，新的持久化记忆确实可见、
# 可检索、不会被同一条时间线重复固化，并且完整溯源可从网关读回。
#
# 这个脚本只读记忆。cleanup 只撤掉 demo-collector 故障，不碰时间线，
# 更不会尝试删除已经写入的记忆。
set -euo pipefail

GATEWAY="netops-ops-console-backend"
BASE_URL="http://127.0.0.1:8026"
HEALTH_URL="${BASE_URL}/api/healthz"
MEMORY_URL="${BASE_URL}/api/rca/memory"
ENV_FILE="/etc/selfevo-console.env"
TIMELINE_DEFAULT="/data/autopoiesis-runtime/sentinel-timeline.jsonl"
SUBJECT="demo-collector.service"
QUERY="demo-collector.service"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
INJECT="$SCRIPT_DIR/inject_incident.sh"

# 900 秒允许紧接着上一轮重跑：上一轮默认冷却最长还会剩约 600 秒，
# 冷却结束后仍要给两轮确认和 90 秒观察期留时间。
RESOLVED_TIMEOUT=${VERIFY_MEMORY_RESOLVED_TIMEOUT:-900}
MEMORY_TIMEOUT=${VERIFY_MEMORY_APPEAR_TIMEOUT:-120}

die() {
    echo "错误：$*" >&2
    exit 1
}

require_uint() {
    [[ $2 =~ ^[1-9][0-9]*$ ]] || die "$1 必须是正整数，实际是：$2"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "缺少命令 $1，无法做真实验证"
}

gateway_env() {
    local pid value
    pid=$(systemctl show "$GATEWAY" -p MainPID --value 2>/dev/null || echo 0)
    [[ -n $pid && $pid != 0 ]] || return 0
    value=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
        | grep -m1 "^$1=" || true)
    echo "${value#*=}"
}

TMP_DIR=$(mktemp -d /tmp/verify-memory-loop.XXXXXX)
INCIDENT_ARMED=0
CLEANUP_DONE=0

cleanup_on_exit() {
    local status=$?
    trap - EXIT
    if (( INCIDENT_ARMED && ! CLEANUP_DONE )); then
        echo >&2
        echo "验证中途退出，先撤掉 demo-collector 故障；记忆和时间线保持原样。" >&2
        "$INJECT" cleanup >&2 || echo "警告：自动 cleanup 失败，请手动运行 $INJECT cleanup" >&2
    fi
    case "$TMP_DIR" in
        /tmp/verify-memory-loop.*) rm -rf -- "$TMP_DIR" ;;
    esac
    exit "$status"
}
trap cleanup_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

read_memory_dsn() {
    python3 - "$ENV_FILE" <<'PY'
import shlex
import sys

path = sys.argv[1]
value = None
with open(path, encoding="utf-8") as handle:
    for raw in handle:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() != "AUTOPOIESIS_MEMORY_DSN":
            continue
        parts = shlex.split(candidate, comments=True, posix=True)
        if len(parts) != 1:
            raise SystemExit("AUTOPOIESIS_MEMORY_DSN 的值无法按 systemd EnvironmentFile 规则解析")
        value = parts[0]
if not value:
    raise SystemExit("没有找到非空的 AUTOPOIESIS_MEMORY_DSN")
print(value)
PY
}

db_snapshot() {
    local output=$1
    python3 - "$output" <<'PY'
import json
import os
import sys

from core.memory.postgres_repository import PostgresMemoryRepository

output = sys.argv[1]
repository = PostgresMemoryRepository(os.environ["AUTOPOIESIS_MEMORY_DSN"])
records = repository.load_records(include_quarantined=True)
records = [
    record for record in records
    if "memory_retention_checkpoint" not in record.tags
]
active = [record for record in records if not record.quarantined]
tiers = ("episodic", "semantic", "procedural", "asset_profile")
counts = {tier: sum(record.tier == tier for record in active) for tier in tiers}
counts["quarantined"] = sum(record.quarantined for record in records)
payload = {
    "source": "postgres",
    "active_count": len(active),
    "counts": counts,
    "records": [record.model_dump(mode="json") for record in active],
}
with open(output, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
PY
}

# 列表端点是首选，因为它证明网关对外看到的就是这份持久化记忆。
# 404 表示正在运行的网关还没有这条新路由，此时才直接只读 PostgreSQL。
memory_snapshot() {
    # Split, not one `local`: bash expands every word on a `local` line before
    # binding any of them, so "${output}.response" reads an unset variable and
    # `set -u` aborts. Classic and silent until the first run.
    local output=$1
    local response="${output}.response"
    local normalized="${output}.new"
    local code rc
    if ! code=$(curl -sS -m 15 -o "$response" -w '%{http_code}' \
        "${MEMORY_URL}?limit=1000"); then
        echo "读取记忆端点失败：$MEMORY_URL" >&2
        return 1
    fi

    case "$code" in
    200)
        if python3 - "$response" "$normalized" <<'PY'
import json
import sys

source, output = sys.argv[1:]
try:
    with open(source, encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, json.JSONDecodeError) as error:
    print(f"记忆端点没有返回合法 JSON：{error}", file=sys.stderr)
    raise SystemExit(1)
if payload.get("ok") is not True or payload.get("durable") is not True:
    print("记忆端点没有确认 durable=true", file=sys.stderr)
    raise SystemExit(1)
records = payload.get("records")
counts = payload.get("counts")
active_count = (payload.get("budget") or {}).get("active")
if not isinstance(records, list) or not isinstance(counts, dict) or not isinstance(active_count, int):
    print("记忆端点缺少 records、counts 或 budget.active", file=sys.stderr)
    raise SystemExit(1)
if len(records) < active_count:
    # 端点上限是 1000。差集验证必须看见完整 ID 集，不能拿截断列表猜。
    raise SystemExit(42)
normalized = {
    "source": "api",
    "active_count": active_count,
    "counts": counts,
    "records": records,
}
with open(output, "w", encoding="utf-8") as handle:
    json.dump(normalized, handle, ensure_ascii=False, sort_keys=True)
PY
        then
            mv -- "$normalized" "$output"
            return 0
        else
            rc=$?
            if (( rc != 42 )); then
                return "$rc"
            fi
            echo "   记忆端点的 1000 条上限截断了 ID 集，改用只读 DB 取得完整差集。"
            db_snapshot "$output"
            return
        fi
        ;;
    404)
        echo "   /api/rca/memory 返回 404，按约定改用只读 DB。"
        db_snapshot "$output"
        ;;
    *)
        echo "记忆端点返回 HTTP $code：$(head -c 300 "$response")" >&2
        return 1
        ;;
    esac
}

fetch_bm25_live_documents() {
    local response="$TMP_DIR/health-now.json"
    curl -fsS -m 15 "$HEALTH_URL" -o "$response" || return 1
    python3 - "$response" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
value = (((payload.get("runtime") or {}).get("memory_index") or {}).get("live_documents"))
if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    raise SystemExit("healthz 缺少 runtime.memory_index.live_documents")
print(value)
PY
}

print_snapshot() {
    local label=$1 snapshot=$2 bm25
    bm25=$(fetch_bm25_live_documents) || die "读取在线 BM25 live_documents 失败"
    python3 - "$label" "$snapshot" "$bm25" <<'PY'
import json
import sys

label, path, bm25 = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
counts = payload["counts"]
print(f"   {label}：活跃 {payload['active_count']} 条，读取来源 {payload['source']}")
print(
    "   分层："
    f"episodic={counts.get('episodic', 0)}，"
    f"semantic={counts.get('semantic', 0)}，"
    f"procedural={counts.get('procedural', 0)}，"
    f"asset_profile={counts.get('asset_profile', 0)}，"
    f"quarantined={counts.get('quarantined', 0)}"
)
print(f"   BM25 live_documents={bm25}")
PY
}

growth_ready() {
    python3 - "$1" "$2" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    before = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    after = json.load(handle)
old_ids = {record["memory_id"] for record in before["records"]}
new = [record for record in after["records"] if record["memory_id"] not in old_ids]
related = [
    record for record in new
    if any("demo-collector" in str(value).lower()
           for value in [*(record.get("tags") or []), *(record.get("asset_ids") or [])])
]
raise SystemExit(0 if after["active_count"] > before["active_count"] and related else 1)
PY
}

assert_growth() {
    local before=$1 after=$2 new_ids=$3 related_ids=$4
    python3 - "$before" "$after" "$new_ids" "$related_ids" <<'PY'
import json
import sys

before_path, after_path, ids_path, related_path = sys.argv[1:]
with open(before_path, encoding="utf-8") as handle:
    before = json.load(handle)
with open(after_path, encoding="utf-8") as handle:
    after = json.load(handle)
old_ids = {record["memory_id"] for record in before["records"]}
new = [record for record in after["records"] if record["memory_id"] not in old_ids]
related = [
    record for record in new
    if any("demo-collector" in str(value).lower()
           for value in [*(record.get("tags") or []), *(record.get("asset_ids") or [])])
]
if after["active_count"] <= before["active_count"]:
    print(
        f"错误：活跃记忆没有增加，基线 {before['active_count']} 条，当前 {after['active_count']} 条",
        file=sys.stderr,
    )
    raise SystemExit(1)
if not new:
    print("错误：活跃条数虽然变化，但完整 ID 差集中没有新记录", file=sys.stderr)
    raise SystemExit(1)
if not related:
    print("错误：本次新增记录的 tags/asset_ids 都没有 demo-collector", file=sys.stderr)
    raise SystemExit(1)
with open(ids_path, "w", encoding="utf-8") as handle:
    handle.writelines(f"{record['memory_id']}\n" for record in new)
with open(related_path, "w", encoding="utf-8") as handle:
    handle.writelines(f"{record['memory_id']}\n" for record in related)
print(f"   活跃记忆：{before['active_count']} -> {after['active_count']}，净增 {after['active_count'] - before['active_count']} 条")
print(f"   ID 差集里有 {len(new)} 条新记录，其中 {len(related)} 条通过 tags/asset_ids 指向 demo-collector：")
for record in related:
    print(f"     · {record['memory_id']} [{record.get('tier')}] tags={record.get('tags') or []} assets={record.get('asset_ids') or []}")
PY
}

narrate_timeline_line() {
    local line=$1 reason remaining
    [[ $line == *"$SUBJECT"* ]] || return 0
    case "$line" in
        *'"kind": "detected"'*) echo "   · 哨兵发现 $SUBJECT" ;;
        *'"kind": "awaiting_confirmation"'*) echo "   · 第一轮只确认，继续等第二轮，避免把瞬态当故障" ;;
        *'"kind": "cooldown"'*)
            remaining=${line#*'"remaining_sec": '}; remaining=${remaining%%,*}
            echo "   · 上一轮冷却还剩约 ${remaining:-未知} 秒，继续等，不把冷却误报成闭环" ;;
        *'"kind": "preflight"'*) echo "   · 前置校验通过后才允许处置" ;;
        *'"kind": "command"'*)
            [[ $line == *'restart'* ]] && echo "   · 已执行 restart，开始观察修复是否站得住" ;;
        *'"kind": "remediated"'*) echo "   · 观察期回读完成，等待最终 resolved" ;;
        *'"kind": "resolved"'*) echo "   · 时间线出现 resolved，本次故障链闭合" ;;
        *'"kind": "declined"'*) echo "   · 前置校验拒绝了本次处置" ;;
        *'"kind": "escalated"'*)
            reason=${line#*'"reason": "'}; reason=${reason%%'"'*}
            echo "   · 复发保护已转人工：${reason:-未给出原因}" ;;
    esac
}

CURSOR=0
follow_subject_until_resolved() {
    # Same bash trap as memory_snapshot(): every word on a `local` line is
    # expanded before any of them is bound, so `timeout` is unset inside the
    # arithmetic on the same line and `set -u` aborts.
    local timeout=$1
    local deadline=$((SECONDS + timeout))
    local total line
    while (( SECONDS < deadline )); do
        total=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
        if (( total > CURSOR )); then
            while IFS= read -r line; do
                narrate_timeline_line "$line"
                if [[ $line == *'"kind": "resolved"'* && $line == *"$SUBJECT"* ]]; then
                    CURSOR=$total
                    return 0
                fi
                if [[ $line == *'"kind": "escalated"'* && $line == *"$SUBJECT"* ]]; then
                    CURSOR=$total
                    return 2
                fi
                if [[ $line == *'"kind": "declined"'* && $line == *"$SUBJECT"* ]]; then
                    CURSOR=$total
                    return 3
                fi
            done < <(sed -n "$((CURSOR + 1)),${total}p" "$TIMELINE")
            CURSOR=$total
        fi
        sleep 2
    done
    return 1
}

wait_for_next_cycle() {
    local timeout=$1 deadline=$((SECONDS + timeout)) total line
    while (( SECONDS < deadline )); do
        total=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
        if (( total > CURSOR )); then
            while IFS= read -r line; do
                if [[ $line == *'"kind": "cycle"'* ]]; then
                    CURSOR=$total
                    return 0
                fi
            done < <(sed -n "$((CURSOR + 1)),${total}p" "$TIMELINE")
            CURSOR=$total
        fi
        sleep 1
    done
    return 1
}

retrieve_new_memory() {
    local new_ids=$1 hit_ids=$2
    python3 - "$new_ids" "$hit_ids" "$QUERY" <<'PY'
import os
import sys

from core.memory.postgres_repository import PostgresMemoryRepository
from core.memory.store import TieredMemoryStore

ids_path, hits_path, query = sys.argv[1:]
with open(ids_path, encoding="utf-8") as handle:
    new_ids = {line.strip() for line in handle if line.strip()}
repository = PostgresMemoryRepository(os.environ["AUTOPOIESIS_MEMORY_DSN"])
# from_repository 只做 SELECT 并在本进程重建派生索引；不调用 flush，也不写数据库。
store = TieredMemoryStore.from_repository(repository, enabled=True)
result = store.retrieve([query], [], limit_per_tier=max(10, len(store.active())))
hits = [record for tier_hits in result.values() for record in tier_hits]
matched = [record for record in hits if record.memory_id in new_ids]
if not matched:
    print(f"错误：ASCII 查询 {query!r} 没有命中本次新增记录", file=sys.stderr)
    raise SystemExit(1)
diagnostics = {item["memory_id"]: item for item in store.retrieval_diagnostics()}
with open(hits_path, "w", encoding="utf-8") as handle:
    handle.writelines(f"{record.memory_id}\n" for record in matched)
print(f"   查询 {query!r} 共命中 {len(hits)} 条，其中 {len(matched)} 条是本次新增：")
for record in matched:
    detail = diagnostics.get(record.memory_id, {})
    print(
        f"     · {record.memory_id} [{record.tier}] "
        f"lexical={detail.get('lexical_score', 0)} "
        f"asset_hits={detail.get('asset_hits', 0)} "
        f"entity_hits={detail.get('entity_hits', [])}"
    )
PY
}

assert_idempotent() {
    python3 - "$1" "$2" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    before = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    after = json.load(handle)
before_ids = {record["memory_id"] for record in before["records"]}
after_ids = {record["memory_id"] for record in after["records"]}
unexpected = sorted(after_ids - before_ids)
if after["active_count"] > before["active_count"] or unexpected:
    print(
        f"错误：又过一个哨兵轮次后记忆仍在增长，活跃条数 {before['active_count']} -> {after['active_count']}，新增 ID={unexpected}",
        file=sys.stderr,
    )
    raise SystemExit(1)
print(f"   又过一个完整轮次，活跃记忆 {before['active_count']} -> {after['active_count']}，新增 ID=0")
PY
}

show_provenance() {
    local memory_id=$1 encoded response="$TMP_DIR/detail.json" code
    encoded=$(python3 - "$memory_id" <<'PY'
import sys
from urllib.parse import quote
print(quote(sys.argv[1], safe=""))
PY
)
    if ! code=$(curl -sS -m 15 -o "$response" -w '%{http_code}' \
        "${MEMORY_URL}/${encoded}"); then
        die "读取记忆详情失败：${MEMORY_URL}/${encoded}"
    fi
    [[ $code == 200 ]] || die "记忆详情端点返回 HTTP $code：$(head -c 300 "$response")"
    python3 - "$response" "$memory_id" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
record = payload.get("record") or {}
if payload.get("ok") is not True or record.get("memory_id") != sys.argv[2]:
    raise SystemExit("错误：详情端点返回的不是请求的新记录")
traces = record.get("source_trace_ids")
tags = record.get("tags")
assets = record.get("asset_ids")
if not isinstance(traces, list) or not traces:
    raise SystemExit("错误：新记录没有 source_trace_ids，溯源链为空")
if not isinstance(tags, list) or not isinstance(assets, list):
    raise SystemExit("错误：新记录详情缺少 tags 或 asset_ids")
print(f"   记录：{record['memory_id']} [{record.get('tier')}] ")
print(f"   source_trace_ids：{len(traces)} 条")
print(f"   tags：{tags}")
print(f"   asset_ids：{assets}")
PY
}

require_uint VERIFY_MEMORY_RESOLVED_TIMEOUT "$RESOLVED_TIMEOUT"
require_uint VERIFY_MEMORY_APPEAR_TIMEOUT "$MEMORY_TIMEOUT"

echo "[0/7] 前置检查：先证明网关、持久化记忆和哨兵都真的在工作。"
[[ $EUID -eq 0 ]] || die "需要 root；故障注入会操作专用 systemd 单元"
for command in systemctl curl python3 sed wc tr grep; do
    require_command "$command"
done
[[ -x $INJECT ]] || die "故障注入脚本不存在或不可执行：$INJECT"
[[ -r $ENV_FILE ]] || die "读不到 $ENV_FILE，无法取得持久化记忆 DSN"
systemctl is-active --quiet "$GATEWAY" || die "网关单元 $GATEWAY 没在运行"
curl -fsS -m 15 "$HEALTH_URL" -o "$TMP_DIR/health-preflight.json" \
    || die "网关单元虽然 active，但 $HEALTH_URL 不可用"
python3 - "$TMP_DIR/health-preflight.json" <<'PY' \
    || die "healthz 没有给出可验证的 durableMemory=true"
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("durableMemory") is not True:
    raise SystemExit("错误：healthz 的 durableMemory 不是 true，继续跑会得到误导性结果")
print(f"   网关 healthz={payload.get('status')}，durableMemory=true")
PY

[[ $(gateway_env AUTOPOIESIS_SENTINEL) == 1 ]] \
    || die "网关进程没有读到 AUTOPOIESIS_SENTINEL=1，哨兵未开启"
SENTINEL_INTERVAL=$(gateway_env AUTOPOIESIS_SENTINEL_INTERVAL)
SENTINEL_INTERVAL=${SENTINEL_INTERVAL:-20}
TIMELINE=$(gateway_env AUTOPOIESIS_SENTINEL_TIMELINE)
TIMELINE=${TIMELINE:-$TIMELINE_DEFAULT}
[[ -r $TIMELINE ]] || die "哨兵虽已配置开启，但时间线不可读：$TIMELINE"
HEARTBEAT=$(python3 - "$TIMELINE" "$SENTINEL_INTERVAL" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, interval_raw = sys.argv[1:]
try:
    interval = float(interval_raw)
except ValueError:
    raise SystemExit(f"哨兵巡检间隔不是数字：{interval_raw}")
if interval <= 0:
    raise SystemExit(f"哨兵巡检间隔必须大于 0：{interval_raw}")
latest = None
with open(path, encoding="utf-8") as handle:
    for line in handle:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("kind") in {"sentinel_started", "cycle", "cycle_failed"}:
            latest = event
if latest is None:
    raise SystemExit("时间线里没有哨兵启动或巡检心跳")
at = datetime.fromisoformat(str(latest.get("at", "")).replace("Z", "+00:00"))
if at.tzinfo is None:
    at = at.replace(tzinfo=timezone.utc)
age = (datetime.now(timezone.utc) - at).total_seconds()
allowed = max(120.0, interval * 3 + 30)
if age < -30 or age > allowed:
    raise SystemExit(f"哨兵最后心跳距今 {age:.0f}s，超过允许的 {allowed:.0f}s")
print(f"{latest.get('kind')} @ {at.isoformat()}（距今 {max(age, 0):.0f}s）")
PY
) || die "哨兵配置虽打开，但没有看到新鲜心跳"
echo "   哨兵进程环境已开启，巡检间隔 ${SENTINEL_INTERVAL}s"
echo "   时间线心跳：$HEARTBEAT"

MEMORY_DSN=$(read_memory_dsn) || die "无法从 $ENV_FILE 读取 AUTOPOIESIS_MEMORY_DSN"
export AUTOPOIESIS_MEMORY_DSN="$MEMORY_DSN"
cd -- "$REPO_ROOT"
DB_RECORDS=$(python3 - <<'PY'
import os
from core.memory.postgres_repository import PostgresMemoryRepository
repository = PostgresMemoryRepository(os.environ["AUTOPOIESIS_MEMORY_DSN"])
print(len(repository.load_records(include_quarantined=True)))
PY
) || die "PostgreSQL 记忆库不可读，无法完成后面的真实检索"
echo "   PostgreSQL 只读连接成功，当前物理快照 $DB_RECORDS 条"

echo
echo "[1/7] 记忆基线：先冻结本次运行之前的 ID 集，历史记忆不能冒充新写入。"
BASELINE="$TMP_DIR/baseline.json"
memory_snapshot "$BASELINE" || die "无法取得记忆基线"
print_snapshot "基线" "$BASELINE"

echo
echo "[2/7] 注入故障：真杀掉 demo-collector，再盯住本次新增时间线直到 resolved。"
CURSOR=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
INCIDENT_ARMED=1
"$INJECT" service-down || die "service-down 注入失败"
echo "   从时间线第 $((CURSOR + 1)) 行开始等，历史 resolved 不算。"
set +e
follow_subject_until_resolved "$RESOLVED_TIMEOUT"
FOLLOW_STATUS=$?
set -e
case "$FOLLOW_STATUS" in
    0) ;;
    2) die "本次故障被复发保护转人工，没有 resolved，不能声称记忆闭环" ;;
    3) die "本次处置被前置校验拒绝，没有 resolved，不能声称记忆闭环" ;;
    *) die "${RESOLVED_TIMEOUT}s 内没看到本次 $SUBJECT 的 resolved" ;;
esac

echo
echo "[3/7] 断言写入：resolved 只是处置闭环，数据库里出现本次新记录才是记忆闭环。"
POST="$TMP_DIR/post.json"
deadline=$((SECONDS + MEMORY_TIMEOUT))
while (( SECONDS < deadline )); do
    if memory_snapshot "$POST" && growth_ready "$BASELINE" "$POST"; then
        break
    fi
    sleep 2
done
[[ -s $POST ]] || die "resolved 后仍读不到记忆状态"
NEW_IDS="$TMP_DIR/new-ids.txt"
RELATED_IDS="$TMP_DIR/related-ids.txt"
assert_growth "$BASELINE" "$POST" "$NEW_IDS" "$RELATED_IDS" \
    || die "resolved 后 ${MEMORY_TIMEOUT}s 内没有形成合格的新记忆"

echo
echo "[4/7] 断言检索：只给 ASCII 查询，确认中文摘要之外的 tags/asset_ids 路径能召回。"
RETRIEVED_IDS="$TMP_DIR/retrieved-new-ids.txt"
retrieve_new_memory "$NEW_IDS" "$RETRIEVED_IDS" \
    || die "新记忆已经存在，但真实 TieredMemoryStore.retrieve 检索不到它"

echo
echo "[5/7] 断言幂等：再等一个哨兵轮次，同一条 resolved 链不能再次长出记录。"
CURSOR=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
CYCLE_TIMEOUT=$(python3 - "$SENTINEL_INTERVAL" <<'PY'
import math
import sys
print(max(60, math.ceil(float(sys.argv[1]) * 3 + 15)))
PY
)
wait_for_next_cycle "$CYCLE_TIMEOUT" \
    || die "${CYCLE_TIMEOUT}s 内没看到下一条 cycle，无法验证幂等"
# cycle 行先落盘，随后同一个 poll_once 才做记忆固化。给事务提交留一个短窗口。
sleep 3
IDEMPOTENT="$TMP_DIR/idempotent.json"
memory_snapshot "$IDEMPOTENT" || die "下一轮后无法读取记忆状态"
assert_idempotent "$POST" "$IDEMPOTENT" || die "同一条链被重复固化"

echo
echo "[6/7] 断言溯源：从详情端点读回本次可检索记录的完整引用链。"
DETAIL_ID=$(head -n 1 "$RETRIEVED_IDS")
[[ -n $DETAIL_ID ]] || die "检索结果文件为空，无法选择新记录做溯源"
show_provenance "$DETAIL_ID" || die "新记录的详情或溯源不完整"

echo
echo "[7/7] 清理故障：只撤 demo-collector，不删时间线，也不删已经写入的记忆。"
"$INJECT" cleanup || die "故障 cleanup 失败"
CLEANUP_DONE=1
FINAL="$TMP_DIR/final.json"
memory_snapshot "$FINAL" || die "cleanup 后无法读取最终记忆状态"
print_snapshot "最终状态" "$FINAL"
echo
echo "验证通过：故障 -> 处置 -> 持久化新增 -> ASCII 检索 -> 幂等 -> 溯源，整条记忆闭环成立。"
