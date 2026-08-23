# Autopoiesis 现场演示操作稿

这份文档只保留两套主演示。事件注入全部通过 Tailscale 登录 R450 后运行脚本，前端只观察真实接口和审计记录，没有演示注入按钮。

访问地址：`http://<R450 的 Tailscale IP>:2026`

终端准备：

```bash
ssh <user>@<R450 的 Tailscale IP>
cd /data/Autopoiesis-AgentSys
```

## 开演前检查

先开两个窗口：

1. 浏览器停在“内网实时 / 态势”。
2. Tailscale SSH 终端停在项目目录。

终端执行：

```bash
sudo ./scripts/demo_memory_rag.sh cleanup
sudo ./scripts/inject_incident.sh status
curl -fsS http://127.0.0.1:8026/api/healthz | python3 -m json.tool
```

应当看到：

- 演示服务显示 `inactive` 或未安装。
- 复发时间压缩覆盖未安装。
- 网关健康接口可读。
- `durableMemory` 为 `true`。
- 前端顶部实时速率仍在变化。

这里说明三件事：

- SSH 终端负责受控触发。
- 网关、哨兵、记忆库和前端是独立组件。
- 前端展示来自轮询接口和审计账本。

## Demo 1：实时态势与有界处置

这套演示包含两个短场景。安全事件展示“发现后克制”，服务故障展示“低影响动作自动执行并验证”。

### 场景 1A：受控安全事件，发现后停止写操作

终端执行：

```bash
sudo ./scripts/inject_incident.sh bruteforce
```

脚本向本机认证日志写入 12 条来自 RFC 5737 文档地址的失败登录记录。它验证安全事件采集和检测链，没有向路由器发送登录请求。

脚本会定向触发真实的 `admin_bruteforce` 巡检，并等待时间线出现 `detected` 和 `no_safe_action` 后再返回。终端出现下面两行时，首页提醒链已经就绪：

```text
· 态势首页已收到 203.0.113.77，页面会在 5 秒轮询内显示提醒
· 安全门判定完成：只报不动
```

#### 0 至约 10 秒：看态势首页

保持浏览器停在“态势”。实时提示区每 5 秒刷新。

注意：

- 出现来源 `203.0.113.77` 的新记录。
- 状态先显示“刚发现”，随后显示“只报不动”。
- 记录位于拓扑上方，操作员无需切换到后台日志页。

它说明：

- 哨兵从当前认证日志独立发现了事件。
- 页面 1 已经接收实时检测结果。
- 该安全事件没有登记自动封禁动作，系统保留管理通道。

#### 提示出现后：点击记录

点击实时提示行。页面自动切换到“多轮故障回放”，并选中 `203.0.113.77` 对应态势记录。

注意：

- 顶部处理轨迹只点亮“巡检发现”和“二次确认”。
- 处置建议显示需要人工判断。
- 前置校验、执行动作、观察期和回读保持未执行状态。

它说明：

- 页面跳转携带了事件对象，落点是对应记录。
- 未执行的阶段保持未点亮，页面没有补画一条虚构处置链。

#### 点击“全链路拓扑剧场”

注意：

- 外部来源与 R450 所在内网节点分别标记。
- 剧场时间线停在安全拒绝位置。
- 页面显示影响对象和拒绝原因。

它说明：

- 安全来源、受影响节点和处理阶段处于同一条可追踪链路。
- 系统对高误伤风险动作采取人工升级策略。

### 场景 1B：隔离服务故障，自动恢复并双窗口验证

回到“态势”首页，然后在终端执行：

```bash
sudo ./scripts/inject_incident.sh service-down
```

脚本会创建 `demo-collector.service`，启动后使用 `SIGKILL` 杀死主进程。systemd 会把它记录为真实的 `failed` 单元。

#### 0 至约 45 秒：看态势首页

注意：

- 实时提示出现 `demo-collector.service`。
- 状态从“刚发现”进入“处置中”。
- 两次检测之间存在二次确认阶段。

它说明：

- 单次采样不会直接触发写操作。
- 哨兵在独立巡检中检测 systemd 当前状态。

#### 出现提示后：点击记录

页面进入“多轮故障回放”，对应服务记录自动选中。

注意处理轨迹依次变化：

```text
巡检发现
  → 二次确认
  → 前置校验
  → 执行动作
  → 快速回退窗口
  → 稳定性窗口
  → 回读验证
```

它说明：

- 前置检查确认目标已经处于 failed 状态。
- 动作目录只允许对该隔离服务执行 `restart_unit`。
- 动作回执和观察结果来自真实命令与探针。

#### 进入“全链路拓扑剧场”

建议在“处置中”阶段进入剧场，并一直停留到恢复完成。

重点观察：

1. 影响范围只包含 `demo-collector.service`。
2. 执行动作后，快速观察约 60 秒。
3. 快速观察通过后，稳定性观察约 180 秒。
4. 目标探针和保护探针采样数持续增加。
5. 最终状态变为“已自动处置”。

它说明：

- `systemctl restart` 的退出码只是动作回执。
- 快速窗口捕获立即恶化。
- 稳定性窗口确认恢复可以维持。
- 保护探针用于发现新故障和旁路损伤。
- 连续健康和最终回读共同决定 `resolved`。

当前默认时长约为：

- 两轮检测及宽限：约 40 至 50 秒。
- 快速回退窗口：约 60 秒。
- 稳定性窗口：约 180 秒。
- 单次完整链：通常约 4 至 5 分钟。

#### 等待 Demo 1 记忆落库

服务显示恢复后，在终端执行：

```bash
sudo ./scripts/demo_memory_rag.sh status
```

应当看到：

```text
failed_units 记忆：可用于探针排序
```

这说明经过外生验证的处置链已经生成或恢复以下记录：

- `sem-sentinel.failed_units`
- `proc-sentinel.failed_units`
- `skill:failed_services`

这一步是 Demo 2 的前置条件。

## Demo 2：记忆系统与知识检索共同参与调查

这套演示复用 Demo 1 刚刚验证过的服务故障经验。脚本暂时暂停自动写操作，让失败单元保持可观察；检测、知识检索和只读探针继续工作。

### 第一幕：布置第二次相似故障

浏览器回到“态势”，终端执行：

```bash
sudo ./scripts/demo_memory_rag.sh arm
```

脚本依次完成：

1. 检查 `failed_units` 程序性记忆是否处于可检索状态。
2. 检查全局写操作当前没有被其他操作员暂停。
3. 以 `memory-rag-demo` 身份暂停自动写操作。
4. 再次杀死 `demo-collector.service`。
5. 保持哨兵检测和调查接口可用。

注意终端输出：

- 明确显示暂停 actor。
- 明确显示服务处于 failed。
- 明确给出收尾命令。

它说明：

- 调查期间的故障状态保持稳定，避免页面还没打开服务就已恢复。
- 暂停开关独立于 Agent 进程，并带操作员和原因审计。

### 第二幕：从态势首页进入对应记录

等待 Page1 出现 `demo-collector.service`，点击该行。

页面切换到“多轮故障回放”，对应态势记录自动选中。

注意：

- 当前链显示检测结果。
- 写操作暂停会使处置停在安全门。
- 记录仍然能进入调查页面。

### 第三幕：进入诊断处置

在态势记录右上点击“看处置链路”。

页面进入“诊断处置”，焦点对象应保持为：

```text
demo-collector.service
```

“查这一个故障”会自动开始只读调查。无需点击演示注入按钮。

### 第四幕：看“本次调查上下文回执”

回执左侧是历史记忆。

预期看到：

- 命中 `proc-sentinel.failed_units`。
- 命中 `sem-sentinel.failed_units`。
- 优先探针是 `systemctl --failed --no-legend`。
- 原始通用探针约 10 条。
- 程序性记忆把失败服务检查调整到第一位。
- 当前输出中确实包含 `demo-collector.service`。
- 现场证据确认后跳过其余无关通用探针。
- 顶部显示“影响记录已持久化”。

它说明：

- 历史案例只负责调查排序。
- 当前 systemd 输出独立确认故障仍然存在。
- 提前停止来自确定性根因映射和当前证据。
- `/memory/{id}/influence` 已经能反查这次调查。

点击“展开探针顺序变化”，可以看到：

1. 默认探针顺序。
2. 记忆调整后的计划顺序。
3. 本轮实际执行顺序。

这三行是“记忆改变了什么”的直接证据。

### 第五幕：看知识检索

回执右侧是知识检索结果。

预期看到：

- `Inspect failed systemd units`
- `Restart and read back a systemd service`
- `Bound repeated service starts`
- 每条记录的 BM25 分数、匹配词、来源和定位符

展开任意知识记录。

它说明：

- RAG 提供命令语义、重启约束和验证方法。
- 检索源是本机安装的 systemd 操作文档和项目处置契约。
- 低相关查询会弃答，页面不会随机填充文档。

页面固定显示边界：

```text
知识片段解释命令和操作约束；当前探针确认现场状态；动作策略负责授权。
```

### 第六幕：可选收尾，点击“分析”

点击调查区的“分析”。

这一步会让推理服务在证据不足时自动补跑一轮只读命令，再生成带引用的结论。R450 当前实测约需 1 至 2 分钟。现场时间有限时，可以在上下文回执和 influence 已出现后直接进入下一幕。

分析服务会收到三组分离的上下文：

1. 历史故障档案和网络特征。
2. 检索到的运维知识片段。
3. 本次会话刚执行的真实命令输出。

注意：

- 诊断结论引用 `ev-*` 当前证据编号。
- 知识片段解释命令含义和动作约束。
- 可执行步骤仍经过只读命令白名单。
- 会修改状态的步骤保持锁定。

它说明：

- 记忆检索和知识检索都进入了同一次调查。
- 现场证据仍然承担根因确认。
- 文档和历史记录没有获得动作授权能力。

### 第七幕：反查 influence，记忆演示的闭环证据

回执顶部应显示：

```text
影响记录已持久化 · 2
```

两条引用通常对应程序性记忆和语义记忆。它们共同指向同一个 `memory_shortcut` 调查轨迹。

需要查看原始接口时，在终端执行：

```bash
for id in proc-sentinel.failed_units sem-sentinel.failed_units; do
  echo "=== $id ==="
  curl -fsS "http://127.0.0.1:8026/api/rca/memory/$id/influence" | python3 -m json.tool
done
```

重点字段：

- `kind: probe_shortcut`
- `subject: demo-collector.service`
- `preferred_probes`
- `candidate_probe_count`
- `saved_probe_count`
- `original_probe_order`
- `planned_probe_order`
- `executed_probe_order`
- `source_trace_id`

### 第八幕：收尾

演示完成后执行：

```bash
sudo ./scripts/demo_memory_rag.sh cleanup
sudo ./scripts/demo_memory_rag.sh status
```

应当看到：

- 演示服务显示 `inactive` 或未安装，unit 文件已经删除。
- 自动写操作暂停为 false。
- 本脚本 marker 不存在。
- 记忆记录和 influence 审计仍然保留。

## Playwright 视觉验收点

排练时可以让视觉审查脚本按下面的状态抓图和断言：

1. 态势首页存在实时提示行。
2. 点击提示后，顶栏切换为“多轮故障回放”。
3. 对应对象处于选中状态。
4. “全链路拓扑剧场”可以打开。
5. 服务故障期间进度和采样数发生变化。
6. “看处置链路”进入诊断处置后仍保持 `demo-collector.service` 焦点。
7. `investigation-context-receipt` 存在。
8. influence 状态最终变为 confirmed。
9. 浏览器控制台无新增异常。
10. 关键接口均返回 2xx。

## 现场失败时的最短检查

### 态势页没有提示

```bash
curl -fsS http://127.0.0.1:8026/api/rca/sentinel/timeline?limit=20 | python3 -m json.tool
systemctl is-active netops-ops-console-backend
sudo ./scripts/inject_incident.sh status
```

### 服务没有自动恢复

```bash
curl -fsS http://127.0.0.1:8026/api/rca/remediation/safety | python3 -m json.tool
systemctl status demo-collector.service --no-pager
```

重点查看 EmergencyStop、动作预算、冷却时间和故障域锁。

### Demo 2 提示记忆未就绪

Demo 1 的服务恢复链尚未完成落库。等待状态变为 resolved，然后执行：

```bash
sudo ./scripts/demo_memory_rag.sh status
```

### 回执显示检索但没有 influence

```bash
journalctl -u netops-ops-console-backend -n 80 --no-pager
curl -fsS http://127.0.0.1:8026/api/rca/memory/proc-sentinel.failed_units/influence | python3 -m json.tool
```

查看调查轨迹持久化错误和 `source_trace_id`。

### 排练中断

```bash
sudo ./scripts/demo_memory_rag.sh cleanup
```

该命令先删除演示服务，再恢复由本脚本设置的暂停状态。
