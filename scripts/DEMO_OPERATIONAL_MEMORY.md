# 记忆系统能力展示：六幕真实数据操作稿

这份稿子配合 `scripts/demo_memory_loop.sh` 使用。脚本会逐幕打印“敲什么、屏幕/接口出什么、说哪句”，台上可以直接照终端提示念。

整个演示只使用运行中服务的接口、FortiGate 当前真实攻击聚合、systemd 真实故障和哨兵真实审计时间线。脚本不装载 fixture，不写死风险数量、动作预算、观察窗口、复发窗口或复发阈值。接口现场返回多少，就展示多少。

## 开演前

```bash
cd /data/Autopoiesis-AgentSys
sudo ./scripts/demo_memory_loop.sh
```

预计耗时主要来自三段真实等待：攻击证据增长、自愈的双观察窗口、复发升级的多轮处置。复发幕通常耗时最长。终端安静时，系统正在等待下一次真实采样或观察窗口结束。

脚本要求开演前满足以下条件：

- 网关健康，OpenAPI 和相关接口可访问。
- 急停状态是已恢复且没有 fail-closed。
- 哨兵已开启，`inject_incident.sh` 能制造真实本机故障。
- `FG100ETK20014183` 上存在 `source=real` 的 `admin_login_failed` 风险和自动开立的档案。
- 使用 root 运行，因为 systemd 故障注入和急停状态文件缺失验证需要主机权限。

任一条件不满足，脚本会说明失败点并以非零状态退出。退出陷阱会恢复被暂存的急停状态文件、请求解除本轮 pause，并调用事故清理脚本，不会把时间压缩配置留在网关进程中。

当前仓库服务把控制接口暴露为 `/api/rca/remediation/pause`、`/api/rca/remediation/resume` 和 `/api/rca/remediation/safety`。脚本先读运行中服务的 OpenAPI；如果部署端暴露 `/api/rca/pause`、`/api/rca/resume`、`/api/rca/safety` 或独立 `/api/rca/emergency-stop`，会使用并显示实际路由。它不会把 404 当成成功，也不会静默跳过实际选中的接口。

## 第一幕：长期记忆，真实攻击

### 敲什么

脚本先同步刷新真实来源，再按防火墙序列号精确查询：

```bash
curl -fsS -X POST http://127.0.0.1:8026/api/rca/operational-memory/refresh \
  -H 'Content-Type: application/json' -d '{}'
curl -fsS 'http://127.0.0.1:8026/api/rca/operational-memory?subject=FG100ETK20014183'
```

它记录第一次 `evidence_count`，随后按现场配置的间隔继续刷新和查询，直到同一条风险的证据数增长。等待超时会直接失败，因此台上只有真实增长发生后才会说“还在增长”。

### 屏幕指哪里

指着 `admin_login_failed` 风险的这些字段：

- `source=real`
- `status`
- `first_seen` 和 `last_seen`
- `evidence_count`
- `reason` 中接口给出的活跃天数和趋势
- `scope=FG100ETK20014183`

第一次和第二次证据数由终端现场打印。不要提前背数字。

### 照着念

> 这是这台防火墙此刻真实发生的登录爆破。系统把当前接口读到的证据数条分散日志聚合成一条持续更新的风险记录。source、起止时间、趋势和证据数都来自刚才两次真实刷新。

## 第二幕：故障档案，不伪造根因

### 敲什么

继续使用同一份精确查询结果，展开标题包含 `admin_login_failed campaign on FG100ETK20014183` 的档案。

### 屏幕指哪里

指着：

- `status=open`
- `source=real` 或 `source=live`
- `reason` 明确包含仍需独立因果确认的说明
- 当前真实 `evidence_count`

接口的审计投影把探测器给出的候选根因放在 `reason` 中，没有把它显示为已确认结论。脚本要求档案仍为 `open`，并要求 `reason` 明确出现因果确认边界；任一条件不满足就停止这一幕。

### 照着念

> 探测器给出的名字只是一条待确认假设。档案保持 open，reason 明确要求独立因果确认；只有人工确认或独立证据支持后，结论才有资格进入长期知识。

## 第三幕：自愈闭环

### 敲什么

```bash
./scripts/inject_incident.sh service-down
curl -fsS 'http://127.0.0.1:8026/api/rca/sentinel/timeline?limit=2000'
```

`service-down` 会真的启动 `demo-collector`，杀掉主进程，让 systemd 把它记录为 failed。脚本记下本幕开始时间，只接受这个时间以后、subject 为 `demo-collector.service` 的事件，旧时间线不能冒充本轮结果。

### 屏幕指哪里

脚本会检查并打印完整链：

1. 第一次 `detected`。
2. `awaiting_confirmation`，避免一次采样撞上部署瞬态。
3. 第二次 `detected`，完成连续检测确认。
4. `preflight` 且 `eligible=true`。
5. `command` 中真实出现 `systemctl restart`。
6. `remediated` 中 `outcome=passed`，同时有快窗口和稳定窗口的真实采样数。
7. `resolved` 中有两个观察窗口的现场秒数和 `execution_id`。

闭环完成后，脚本调用 operational-memory refresh，再查询 `demo-collector.service`。随后它用同一个 `execution_id` 对照 `/api/rca/remediation/runs` 的动作回执，并要求 operational-memory 中出现 `resolved` 档案和非零证据数。

### 照着念

> 同一条链从连续检测、确认、前置校验、真实 restart 命令，走过快窗口和稳定窗口回读后才标记恢复。execution_id 同时出现在时间线和动作回执里；刷新后，resolved 故障档案带着本轮证据进入运维记忆。当前可自动处置范围只限本机 L1，远程设备保持只读分析并转人工。

## 第四幕：记忆改变动作

### 敲什么

```bash
./scripts/inject_incident.sh recurring
curl -fsS http://127.0.0.1:8026/api/rca/sentinel/recurrence
curl -fsS 'http://127.0.0.1:8026/api/rca/sentinel/timeline?limit=2000'
```

`recurring` 只压缩演示时钟，复发判据保持服务当前配置。它会反复制造同一个 systemd 故障，等待每轮真实处置和观察窗口完成，达到复发阈值后再制造下一次故障。

### 屏幕指哪里

指着 recurrence 投影中的：

- `window_sec`
- `limit`
- `recurrences`
- `cycles[]`，每项都是此前恢复后又复发的真实引用
- `escalated=true`

再指向时间线最后一条 `escalated`：动作没有进入 `preflight` 和执行阶段，`prior_cycles[]` 直接解释它为什么停手。脚本用接口现场的 `limit` 计算读法，例如现场阈值为三，就显示“前三次修好后复发，第四次拒绝”。

### 照着念

> 这是记忆真正改变动作的地方。系统从审计时间线重建同一对象、同一动作的修好后复发链；达到接口现场给出的阈值后，下一次不再执行，拒绝原因旁边直接列出此前每次恢复和再次故障的时间。当前可自动处置范围只限本机 L1，远程设备保持只读分析并转人工。

## 第五幕：自动处置有界，安全门

### 敲什么

先看动作闭集和预算：

```bash
curl -fsS http://127.0.0.1:8026/api/rca/remediation/actions
curl -fsS http://127.0.0.1:8026/api/rca/remediation/safety
```

脚本要求动作目录当前只有：

- `restart_unit`，本机故障服务重启
- `bounce_interface`，本机失载波物理网口重置

两项都必须是 L1。预算、并发、冷却、故障域锁和观察窗口全部念接口现场值。

然后对健康的网关服务做只读前置校验：

```bash
curl -fsS -X POST http://127.0.0.1:8026/api/rca/remediation/preflight \
  -H 'Content-Type: application/json' \
  -d '{"action":"restart_unit","target":"autopoiesis-gateway.service"}'
```

必须返回 `eligible=false`，理由说明只有 failed 服务才符合条件。

接着真实 pause，再提交一次动作前置校验：

```bash
curl -fsS -X POST http://127.0.0.1:8026/api/rca/remediation/pause \
  -H 'Content-Type: application/json' \
  -d '{"actor":"memory-demo","reason":"演示全局暂停会挡住后续动作"}'
curl -fsS -X POST http://127.0.0.1:8026/api/rca/remediation/preflight \
  -H 'Content-Type: application/json' \
  -d '{"action":"restart_unit","target":"demo-collector.service"}'
curl -fsS -X POST http://127.0.0.1:8026/api/rca/remediation/resume \
  -H 'Content-Type: application/json' \
  -d '{"actor":"memory-demo","reason":"暂停分支验证完成，恢复动作入口"}'
```

暂停后的请求必须返回 `eligible=false`、`refused=true`，原因必须指向全局 pause。resume 必须恢复 `paused=false`。

最后验证急停状态缺失时 fail-closed。脚本从网关进程环境读取真实状态文件路径，先把原文件移动到临时目录，再通过 `/api/rca/remediation/safety` 和 preflight 读取结果。必须同时看到：

- `paused=true`
- `fail_closed=true`
- reason 包含 `state_read_error`
- preflight 被拒绝

随后脚本把原文件原样移回。退出陷阱也保存了同一恢复动作，因此中途 Ctrl-C 不会留下缺失状态文件。

### 照着念

> 自动动作目录和预算都以刚才接口现场值为准。健康服务会被前置校验拒绝，全局 pause 会挡住后续动作，状态文件缺失会 fail-closed。低风险动作自动执行，首次恶化进入回退或转人工，预算耗尽后停止。当前自动范围只限本机 L1；远程防火墙、交换机和终端保持只读分析并转人工。

这里必须把远程边界说完整。当前远程设备只有只读证据适配器，没有获批的写适配器和设备级回滚契约。L2 配置类动作对未注册设备由安全门拒绝并转人工。

## 第六幕：收尾

### 敲什么

```bash
./scripts/inject_incident.sh cleanup
./scripts/inject_incident.sh status
```

cleanup 删除演示 systemd 单元，并收回复发幕的时间压缩配置。哨兵时间线继续保留，因为它是审计日志。

### 照着念

> 演示结束。刚才展示的是机制在真实攻击、真实本机故障、真实拒绝和真实审计数据上的运转。长期收益需要后续真实运行持续累积，本次演示没有给出未经长期测量的节省数字。

## 现场故障口径

脚本失败时，直接念终端的“演示失败”行，不要圆场：

- 风险证据数在等待窗口内没有增长，就不说“仍在增长”。
- 档案已经被人工确认，就按接口当前状态讲，不能继续念“待确认假设”。
- 时间线缺阶段或观察窗口采样，就不把该轮称为完整闭环。
- recurrence 没有达到阈值，就不说记忆已经改变动作。
- pause、resume、健康目标拒绝或 fail-closed 任一断言不成立，就停止安全门这一幕。

这套演示证明的是机制在真数据上运转。长期减少了多少人工时间、降低了多少恢复时间，需要后续真实运行、明确统计窗口和可复核样本才能回答。
