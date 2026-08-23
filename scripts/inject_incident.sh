#!/usr/bin/env bash
# Create a real, observable fault for the sentinel to find — and clean it up.
#
# Nothing here is simulated into a fixture. The service really runs, really
# dies, and systemd really reports it as failed, because a demo that injects a
# fake reading proves only that the reading was faked. The blast radius is a
# unit that exists for this purpose and nothing depends on.
#
#   ./inject_incident.sh service-down   a watched unit crashes
#   ./inject_incident.sh bruteforce     repeated failed SSH auth from a fake source
#   ./inject_incident.sh recurring      the same unit crashes over and over until the
#                                       system refuses to keep fixing it (~9 min, hands off)
#   ./inject_incident.sh status         what is currently injected
#   ./inject_incident.sh cleanup        remove everything this script created
#
# The three scenarios are chosen to end in three different verdicts.
# service-down has a monotonic action and gets fixed unattended. bruteforce
# deliberately has none, so the system reports it and stops. recurring gets
# fixed three times and then refused — a fault that keeps coming back is not a
# fault a restart fixes, and the system that keeps restarting it is hiding the
# trend rather than handling it. A demo where everything auto-heals hides the
# more important half of the design.
set -euo pipefail

UNIT=demo-collector
UNIT_FILE="/etc/systemd/system/${UNIT}.service"
SUBJECT="${UNIT}.service"       # what systemd --failed prints, and what the timeline records
FAKE_SOURCE="203.0.113.77"   # RFC 5737 documentation range: never a real host

GATEWAY=netops-ops-console-backend
DROPIN_DIR="/etc/systemd/system/${GATEWAY}.service.d"
DROPIN_FILE="${DROPIN_DIR}/zz-demo-recurrence.conf"
DEMO_ENV_FILE="/etc/selfevo-console-demo-recurrence.env"
TIMELINE_DEFAULT="/data/autopoiesis-runtime/sentinel-timeline.jsonl"
HEALTHZ="http://127.0.0.1:8026/api/healthz"

# ── demo-only time compression ───────────────────────────────────────────────
#
# Production is a 24-hour recurrence window and a 600s base cooldown. No demo
# waits that out, so `recurring` compresses the CLOCK — and only the clock.
# The decision rule is untouched: the refusal threshold stays at 3, the same
# number the code defaults to, so what the audience watches is the production
# ladder running fast, not a special demo ladder.
#
# 3600 rather than something smaller because the escalation note prints
# `window_sec // 3600` hours: at one hour the sentinel says "在 1 小时内已经生效
# 过 3 次又复发" and that sentence is exactly true. A 30-minute window would put
# a rounded-up number on the screen, which is the kind of small lie that costs
# the whole demo when somebody checks.
DEMO_WINDOW=3600     # AUTOPOIESIS_RECURRENCE_WINDOW  (prod: 86400)
DEMO_LIMIT=3         # AUTOPOIESIS_RECURRENCE_LIMIT   (prod: 3 — deliberately unchanged)
DEMO_COOLDOWN=30     # AUTOPOIESIS_SENTINEL_COOLDOWN  (prod: 600)
DEMO_INTERVAL=10     # AUTOPOIESIS_SENTINEL_INTERVAL  (this box: 15)

# 30s base is picked against the 90s watch window: the ladder doubles it to 60s
# and then 120s, so the first two cooldowns expire while the sentinel is still
# watching its own repair and never stall the demo, while the third is long
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
# `systemctl show -p Environment` only lists Environment= lines, not what came
# from an EnvironmentFile, so ask the running process instead. This is also the
# only honest way to check whether an override actually took: the file being on
# disk proves nothing until the process has been restarted through it.
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

# ── the demo-only override, and how to take it back ──────────────────────────
#
# Why a drop-in with its own EnvironmentFile, and not the obvious alternatives:
#
#   - Editing /etc/selfevo-console.env in place would work, but that file holds
#     every provider credential on this box. A sed against a secrets file, with
#     a demo script that might be Ctrl-C'd halfway, is not a trade worth making
#     for four numbers.
#   - A drop-in with plain `Environment=` lines does NOT work here, and this is
#     worth knowing: systemd applies EnvironmentFile= over Environment=
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
# 演示专用 · 由 scripts/inject_incident.sh recurring 写入，cleanup 删除。
# 这里只压缩时间，不改判据：拒绝阈值 3 和生产默认一致。
# 如果这个文件在一台没人在演示的机器上留着，那就是配置事故 —— 删掉它，
# 然后 systemctl daemon-reload && systemctl restart ${GATEWAY}
AUTOPOIESIS_RECURRENCE_WINDOW=${DEMO_WINDOW}
AUTOPOIESIS_RECURRENCE_LIMIT=${DEMO_LIMIT}
AUTOPOIESIS_SENTINEL_COOLDOWN=${DEMO_COOLDOWN}
AUTOPOIESIS_SENTINEL_INTERVAL=${DEMO_INTERVAL}
ENVEOF
    cat > "$DROPIN_FILE" <<DROPEOF
# 演示专用覆盖。文件名必须排在 provider-env.conf 之后：drop-in 按文件名字典序
# 读入，后一个 EnvironmentFile 才盖得住前一个。cleanup 会删掉这一层。
[Service]
EnvironmentFile=-${DEMO_ENV_FILE}
DROPEOF
    systemctl daemon-reload
    echo "已写入演示覆盖，重启网关让它生效（环境变量在进程启动时读，改文件不重启等于没改）…"
    systemctl restart "$GATEWAY"
    wait_for_gateway || die "网关重启后 45 秒内没起来，先查 journalctl -u $GATEWAY -n 50"

    # Verify against the process, not the file. A drop-in that parsed wrong, or
    # sorted wrong, fails exactly here and nowhere else — and a half-applied
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
        die "已回滚演示覆盖，机器留在生产口径。"
    fi
    echo "覆盖已生效（窗口 ${DEMO_WINDOW}s / 阈值 ${DEMO_LIMIT} / 冷却 ${DEMO_COOLDOWN}s / 巡检 ${DEMO_INTERVAL}s）"
}

# Idempotent: says nothing and touches nothing when there is no override.
# Returns 0 if it removed one (and therefore restarted the gateway), 1 if there
# was nothing to remove.
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
    echo "已删掉演示覆盖，重启网关收回压缩窗口（不重启的话进程里那份还在跑）…"
    systemctl restart "$GATEWAY"
    wait_for_gateway || echo "警告：网关没在 45 秒内起来，查 journalctl -u $GATEWAY" >&2
    local left
    left=$(gateway_env AUTOPOIESIS_RECURRENCE_WINDOW)
    if [[ -n "$left" ]]; then
        echo "警告：网关进程里仍有 AUTOPOIESIS_RECURRENCE_WINDOW=$left，检查 /etc/selfevo-console.env 是不是也被人塞了一份" >&2
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
    echo "演示没跑完，先把压缩窗口收回去…" >&2
    revert_demo_override >/dev/null 2>&1 || true
    echo "已回到生产口径。demo-collector 可能还留着，用 ./scripts/inject_incident.sh cleanup 收尾。" >&2
}

# ── reading the ladder's own state out of the timeline ───────────────────────
# Same shape as the projection the backend runs: a cycle counts only when a
# `resolved` is followed by a LATER `detected` on the same subject. A repair
# that is still holding is not a recurrence. If this ever disagrees with the
# console, believe the console — this exists to warn the operator before an
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
        *'"kind": "detected"'*)             echo "   · 发现" ;;
        *'"kind": "awaiting_confirmation"'*) echo "   · 等第二轮确认（一次采样可能撞上部署瞬态）" ;;
        *'"kind": "preflight"'*)            echo "   · 前置校验：只有已经在最差状态的目标才放行" ;;
        *'"kind": "cooldown"'*)             echo "   · 还在冷却，本轮不动作" ;;
        *'"kind": "command"'*)
            # Preflight streams its read-only probes through the same channel;
            # only the restart itself is worth a line on stage.
            if [[ "$line" == *'"restart"'* ]]; then
                echo "   · 执行 systemctl restart，接下来进入快速回退窗和稳定性观察窗"
            fi ;;
        *'"kind": "declined"'*)             echo "   · 前置校验没过，拒绝执行" ;;
        *'"kind": "remediated"'*)           echo "   · 观察期结束，回读判定" ;;
        *'"kind": "resolved"'*)             echo "   · 判定恢复，这一轮闭环" ;;
        *'"kind": "escalated"'*)
            reason=${line#*'"reason": "'}; reason=${reason%%'"'*}
            echo "   · 拒绝执行 → 转人工"
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
    if sentinel_enabled; then
        echo "哨兵在自动巡检，不用手动做任何事。"
        echo
        echo "现在切到浏览器 → 诊断处置 页 → 往下滚到「系统自己做过什么」"
        echo "页面每 5 秒自己刷新，通常约 4 至 5 分钟会依次出现："
    else
        echo "哨兵没开（AUTOPOIESIS_SENTINEL=1 可开启自动巡检）。"
        echo "手动推进：curl -X POST localhost:8026/api/rca/sentinel/poll   （跑两次）"
        echo
        echo "会依次出现："
    fi
    echo "  发现 → 等确认 → 发现 → 前置校验 → 已执行 → 判定恢复"
    echo
    echo "当前默认观察为约 60 秒快速回退窗 + 180 秒稳定性窗口。"
    echo "关键保护指标恶化会快速失败；连续健康通过稳定性窗口后才记为恢复。"
    ;;

bruteforce)
    echo "向系统日志写入来自 $FAKE_SOURCE 的失败登录记录…"
    for i in $(seq 1 12); do
        logger -t sshd -p auth.warning \
            "Failed password for invalid user admin from ${FAKE_SOURCE} port $((40000 + i)) ssh2"
    done
    echo "注入完成：12 条失败登录"
    echo
    echo "同样切到 诊断处置 页看。这一条会被检测到，但不会自动处置——"
    echo "封禁是可撤销的，但封错来源就等于堵住自己的管理通道，所以只报不动。"
    echo "页面上它是琥珀色的「只报不动」，链条停在「无安全动作」。"
    echo
    echo "演示时这一条比自愈那一条更值钱：它证明分级不是摆设。"
    ;;

recurring)
    sentinel_enabled || die "哨兵没开，这个场景全靠它自己跑。
先在 /etc/selfevo-console.env 里设 AUTOPOIESIS_SENTINEL=1 再 systemctl restart $GATEWAY。"

    echo "复发升级演示：同一个故障反复发生，看系统在第几次停手。"
    echo
    echo "真实口径是 24 小时窗口 + 600 秒基础冷却，没有哪场演示等得起。"
    echo "所以这里压缩的是时钟，不是判据——拒绝阈值仍然是 $DEMO_LIMIT 次，"
    echo "和生产默认一模一样。台上跑的是同一把梯子，只是走得快。"
    echo

    # Checked before anything is applied: if the ladder is already at the top,
    # the run is pointless, and it should not cost a gateway restart to find out.
    resolve_timeline
    PRIOR=$(cycles_in_window "$DEMO_WINDOW")
    if (( PRIOR >= DEMO_LIMIT )); then
        die "时间线里 $SUBJECT 在 $((DEMO_WINDOW / 60)) 分钟内已经有 $PRIOR 个「修好又复发」的周期，
阶梯已经在顶端了——现在跑，第一次就会被拒绝，看不到「先修三次」那一段。

要么等窗口过期（最多 $((DEMO_WINDOW / 60)) 分钟），要么按 DEMO.md 开演前那一步清一次时间线：
  ./scripts/inject_incident.sh cleanup
  rm -f $TIMELINE
  systemctl restart $GATEWAY
时间线是审计日志，所以这一步只在开演前做，别当成日常操作。"
    fi
    ACT_ROUNDS=$((DEMO_LIMIT - PRIOR))
    if (( PRIOR > 0 )); then
        echo "注意：时间线里已经有 $PRIOR 个复发周期（$((DEMO_WINDOW / 60)) 分钟窗口内），"
        echo "所以这次只会修 $ACT_ROUNDS 次就拒绝，不是 $DEMO_LIMIT 次。想要完整的三次，先清时间线。"
        echo
    fi

    apply_demo_override
    trap revert_on_abort EXIT
    echo

    # 150s a round: ~20s to detect and confirm, ~95s of watch window, 30s pause.
    echo "接下来自动跑 $((ACT_ROUNDS + 1)) 轮，全程不用你动手，大约 $(( (ACT_ROUNDS * 150 + 100) / 60 )) 分钟："
    echo "  前 $ACT_ROUNDS 轮：杀掉 $UNIT → 哨兵发现 → 确认 → 重启 → 双窗口观察 → 判定恢复"
    echo "  最后 1 轮：同样杀掉，但这次系统拒绝重启，记 escalated，转人工"
    echo
    echo "边等边讲的话在 scripts/DEMO.md 第六幕。终端这里会同步打出每一步。"
    echo

    CURSOR=$(wc -l < "$TIMELINE" 2>/dev/null || echo 0)
    EARLY_ESCALATION=0
    install_unit

    for (( round = 1; round <= ACT_ROUNDS; round++ )); do
        echo "── 第 $round/$((ACT_ROUNDS + 1)) 次故障（预期：修好）──────────────────────────"
        crash_unit
        # Either terminal ends the round. The ladder can reach its limit earlier
        # than this loop expects — a cycle left in the window by an earlier run
        # counts too, and the count is a property of the log, not of this script.
        # Waiting only for `resolved` turns the mechanism working correctly into
        # a five-minute timeout.
        if follow_until escalated 5 >/dev/null 2>&1; then
            echo "   这一轮直接升级了——窗口里本来就有前次留下的复发。跳到终局。"
            EARLY_ESCALATION=1
            break
        fi
        follow_until resolved 300 || {
            if follow_until escalated 5 >/dev/null 2>&1; then
                echo "   它在这一轮就拒绝了：复发已经到限。这就是要演的那一幕，提前到了。"
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
        echo "   等 30 秒让这一轮的冷却过期，再制造下一次——否则下一次会被记成冷却拦截，不是新一级阶梯。"
        echo
        sleep 30
    done

    if [ "$EARLY_ESCALATION" = "1" ]; then
        DEMO_COMPLETE=1
        echo
        echo "── 终局：它已经拒绝了 ────────────────────────────────────────────"
        echo
        echo "现在屏幕上："
        echo "  · 态势页那一行变成红色「要人工」，并且排到了最前面"
        echo "  · 环节条只亮到「已确认」，后面是暗的——它没执行，不是执行失败"
        echo "  · 事故卡上有复发次数和引用链：前几次分别什么时候修好、什么时候又坏"
        echo
        echo "$UNIT 会一直是 failed，这是对的——「转人工」的意思就是它在等人。"
        echo
        echo "压缩窗口现在还在（不然重启网关会把你正要展示的这一屏冲掉）。"
        echo "讲完一定要收： ./scripts/inject_incident.sh cleanup"
    else

    echo "── 第 $((ACT_ROUNDS + 1))/$((ACT_ROUNDS + 1)) 次故障（预期：拒绝）──────────────────"
    echo "   前 $DEMO_LIMIT 次都是「修好了又坏」。这一次它应该不修了。"
    crash_unit
    if follow_until escalated 180; then
        # From here the compressed window stays until cleanup: the escalated
        # state is what the operator is about to show, and a revert would
        # restart the gateway out from under it.
        DEMO_COMPLETE=1
        echo
        echo "阶梯走完了。"
        echo
        echo "现在屏幕上："
        echo "  · 态势页那一行变成红色「要人工」，并且排到了最前面"
        echo "  · 环节条只亮到「已确认」，后面是暗的——它没执行，不是执行失败"
        echo "  · 事故卡上有复发次数和引用链：前 $DEMO_LIMIT 次分别什么时候修好、什么时候又坏"
        echo
        echo "$UNIT 会一直是 failed，这是对的——「转人工」的意思就是它在等人。"
        echo
        echo "压缩窗口现在还在（不然重启网关会把你正要展示的这一屏冲掉）。"
        echo "讲完一定要收： ./scripts/inject_incident.sh cleanup"
    else
        echo
        # 覆盖在 apply 时已经对着进程校验过，所以这里基本不会是覆盖的问题。
        echo "没等到 escalated（压缩覆盖会被自动收回，机器不会留在演示口径）。按顺序查：" >&2
        echo "  1. 后端记下这个事件了吗： grep '\"kind\": \"escalated\"' $TIMELINE" >&2
        echo "  2. 前几轮真的闭环了吗：   grep -c '\"kind\": \"resolved\"' $TIMELINE  应该 ≥ $DEMO_LIMIT" >&2
        echo "  3. 第四轮是不是被冷却拦了：时间线尾部找 cooldown；是的话把脚本里的 DEMO_COOLDOWN 调小重来" >&2
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
    echo "近期该来源的失败登录：$(journalctl --since -30m --no-pager 2>/dev/null | grep -c "$FAKE_SOURCE" || true) 条"
    echo
    echo "复发阶梯（recurring 场景）"
    if [[ -f "$DROPIN_FILE" || -f "$DEMO_ENV_FILE" ]]; then
        echo "  演示覆盖：已安装"
        [[ -f "$DROPIN_FILE" ]]   && echo "    drop-in   $DROPIN_FILE" \
                                  || echo "    drop-in   缺失（$DROPIN_FILE）——覆盖不会生效"
        [[ -f "$DEMO_ENV_FILE" ]] && echo "    env 文件  $DEMO_ENV_FILE" \
                                  || echo "    env 文件  缺失（$DEMO_ENV_FILE）——覆盖不会生效"
    else
        echo "  演示覆盖：未安装（生产口径）"
    fi
    WINDOW_NOW=$(gateway_env AUTOPOIESIS_RECURRENCE_WINDOW)
    LIMIT_NOW=$(gateway_env AUTOPOIESIS_RECURRENCE_LIMIT)
    COOLDOWN_NOW=$(gateway_env AUTOPOIESIS_SENTINEL_COOLDOWN)
    INTERVAL_NOW=$(gateway_env AUTOPOIESIS_SENTINEL_INTERVAL)
    echo "  网关进程实际读到的（不是文件里写的——环境变量在启动时读一次）："
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
        # `|| echo 0` — the latter prints the zero twice.
        echo "    已记录的 escalated：$(grep -c '"kind": "escalated"' "$TIMELINE" 2>/dev/null || true) 条"
    else
        echo "    还没有时间线文件（哨兵没跑过，或者被清过）"
    fi
    ;;

cleanup)
    systemctl stop "$UNIT" 2>/dev/null || true
    systemctl disable "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    echo "已清理 $UNIT。日志里的失败登录记录会随 journal 轮转自然过期，不影响任何东西。"
    # Done second and on purpose: the gateway restart brings the sentinel back,
    # and it should come back to a box where the demo unit is already gone.
    if revert_demo_override; then
        echo "时间线没动——它是审计日志，cleanup 不删。所以窗口内重跑 recurring 会从阶梯中间开始，"
        echo "status 会告诉你还剩几级。要从头演，按 DEMO.md 开演前那一步清一次时间线。"
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
