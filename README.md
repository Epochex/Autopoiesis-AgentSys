# Autopoiesis-AgentSys

面向内网的态势感知、事件调查和有界自动处置系统。系统持续读取网关安全事件、全量流量事实、处置时间线与二层身份发现，把一次事件整理成可核验的故障档案，把跨时间风险聚合成长期记录，再从多个已验证案例中提炼可复用的网络特征。

记忆系统在这里承担三个具体职责：

1. 为当前调查安排更有依据的探针顺序。
2. 提示某个动作在相似场景中的有效性、失败记录和复发风险。
3. 保存“哪条记忆影响了哪次调查”的归因记录，支持回放和消融评测。

动作能否执行由当前证据、动作策略、安全门、预算、锁和回读结果共同决定。记忆提供调查先验，不提供写操作授权。

## 先看三个真实 use case

仓库提供两套演示。第一套包含安全事件与服务故障两个场景，第二套展示记忆和知识检索如何参与一次新调查。触发动作只在 R450 终端执行，前端没有演示按钮。

准备：

~~~bash
ssh <user>@<R450 的 Tailscale IP>
cd /data/Autopoiesis-AgentSys
sudo ./scripts/demo_memory_rag.sh cleanup
sudo ./scripts/inject_incident.sh status
curl -fsS http://127.0.0.1:8026/api/healthz | python3 -m json.tool
~~~

浏览器打开：

~~~text
http://<R450 的 Tailscale IP>:2026
~~~

完整逐镜头操作说明见 [scripts/DEMO.md](./scripts/DEMO.md)。

### Use case A：安全事件被发现，安全门保留防火墙配置

~~~bash
sudo ./scripts/inject_incident.sh bruteforce
~~~

脚本向本机认证日志写入 12 条来自 RFC 5737 演示地址 203.0.113.77 的失败登录记录。哨兵完成检测与二次确认后，把临时封禁列为候选动作，并逐项检查来源归属、活动管理会话、管理地址豁免、封禁 TTL、提交后回读和超时回滚。

本场景缺少完整的来源归属与管理地址豁免校验，安全门拒绝防火墙写入。前端从“内网实时”提示进入态势记录，再进入全链路剧场，展示：

- 已观测事实：来源地址、失败次数、时间窗口和证据位置。
- 已完成判断：事件达到暴力登录检测阈值。
- 候选动作：临时防火墙封禁。
- 未满足条件：来源归属与管理地址检查。
- 决策结果：防火墙配置保持当前版本，事件交接安全运营。
- 记忆结果：生成 IncidentDossier 和 safety_gated:no_safe_action 记录；本轮不会增加“封禁成功”经验。

这个场景验证检测能力、决策解释和安全边界。页面给出保留配置的工程理由、当前排查结果与后续责任。

### Use case B：隔离服务真实崩溃，系统执行恢复并回读

~~~bash
sudo ./scripts/inject_incident.sh service-down
~~~

脚本创建唯一的 demo-collector-<UTC 时间>.service，并用 SIGKILL 终止主进程。systemd 将单元记录为 failed，哨兵从独立巡检中发现故障。

前端链路依次显示：

~~~text
巡检发现
  → 二次确认
  → 前置校验
  → restart_unit
  → 60 秒快速回退窗口
  → 180 秒稳定性窗口
  → 最终回读
  → resolved 或 reverted
~~~

当前动作目录只允许重启本轮隔离服务。系统在执行前记录目标状态并占用故障域锁，执行后持续采集目标探针和保护探针。任一关键保护指标恶化会停止后续动作；具备逆操作的动作进入回退，restart_unit 从 failed 基线启动且没有建设性逆操作，因此直接转人工。连续健康采样和最终状态回读共同决定成功。完整过程通常需要 4 至 5 分钟。

本场景验证低影响动作的自动授权、真实执行、双窗口观察、旁路损伤检查和结果落库。

### Use case C：第二次相似故障中，记忆和知识检索参与调查

先完整执行一次 Use case B，等待已验证经验落库，然后运行：

~~~bash
sudo ./scripts/demo_memory_rag.sh arm
~~~

脚本以带审计身份的操作员暂停开关保留一个新的 failed 单元，检测与只读调查继续工作。首页出现对应服务后，点击该记录，再点击“看处置链路”。诊断处置页会自动对焦同一服务并展示“本次调查上下文回执”：

- 命中的历史记录及其记忆编号。
- 记忆参与前后的探针顺序。
- 候选探针数、实际执行数和提前停止原因。
- 厂商或系统文档的知识检索结果与 BM25 分数。
- influence 记录，即本次调查实际引用了哪些记忆。
- 当前命令输出，作为本轮结论的现场证据。

当前已验证样例中，检索命中 failed_units 的语义记录和处置程序，10 个候选探针缩减为 1 个实际探针，返回 4 篇 systemd 相关知识片段，并持久化 2 条 influence 记录。该数字描述这次演示样例，长期收益由下文的配对评测持续补证。

收尾：

~~~bash
sudo ./scripts/demo_memory_rag.sh cleanup
~~~

## 数据如何流过系统

~~~text
FortiGate 安全事件 ─┐
ClickHouse 流量事实 ├─→ 证据规范化与关联 ─→ IncidentDossier
哨兵处置时间线 ─────┤                       ├→ RiskPattern
ARP / 身份发现 ─────┘                       └→ NetworkFeature
                                                   │
                         当前调查 ← 混合召回 ←─────┤
                             │                     │
                             ├→ 只读探针与知识检索 │
                             ├→ 动作策略与安全门   │
                             ├→ 执行、观察、回读   │
                             └→ 结果与 influence ──┘
~~~

### 四个真实输入

| 输入 | 当前用途 | 进入长期对象前的处理 |
|---|---|---|
| netops.security_events | 管理登录失败、拒绝与安全事件 | 按来源、目标、事件族和时间窗口聚合 |
| netops.facts | 连接、端口、动作与全历史流量事实 | 过滤可归因事实，保留查询范围与来源 |
| 哨兵追加式时间线 | 检测、确认、动作、回退、观察与最终结果 | 按稳定事件标识还原完整处置链 |
| 环境发现 | ARP、身份归属、地址冲突与覆盖盲区 | 只接收带证据的已确认发现 |

operational_memory.refresh() 周期性读取这些来源，生成不可变快照并写入 PostgreSQL。原始日志仍保存在事实层，RiskPattern 保存聚合结果和证据引用，避免逐条复制数万条同类事件。

### 2026-08-24 现场审计快照

下面的数字用于说明当前部署确实有数据流过，随着采集和整理继续运行会变化：

| 项目 | 审计值 |
|---|---:|
| ClickHouse netops.facts | 51,408,690 行 |
| 采集服务 | active，审计时持续写入 |
| IncidentDossier | 427 |
| RiskPattern | 2,048 |
| NetworkFeature | 13,300 |
| 执行记忆当前记录 | 235 |
| 哨兵事件记录 | 206 |
| Python 测试 | 1,227 passed，11 skipped |
| 前端单元测试 | 5 passed |

健康接口同时暴露持久化状态、整理次数、索引代际和后台任务错误。部署核验需要同时查看健康接口、最新事实时间和具体业务对象，单独的进程存活无法说明数据链路完整。

## 记忆系统：三类业务对象、执行经验与知识库

系统把长期信息分成四个职责明确的部分：

| 层次 | 保存什么 | 参与当前决策的方式 |
|---|---|---|
| 事实层 | 原始安全事件、流量事实、命令输出、回读样本 | 提供当前事件的可复核证据 |
| 业务记忆 | IncidentDossier、RiskPattern、NetworkFeature | 提供历史案例、风险趋势和调查先验 |
| 执行记忆 | 经验证的情节、语义结论、处置程序和资产画像 | 排序探针、提示失败经验、触发复发升级 |
| RAG 知识库 | 厂商文档、系统文档和操作约束 | 解释命令、参数与排查步骤 |

这种拆分防止四类常见污染：原始日志冒充结论，单次成功冒充规律，文档建议冒充现场事实，历史经验冒充当前动作授权。

### IncidentDossier：一次事件的完整档案

IncidentDossier 以稳定事件标识组织症状、证据引用、根因假设、处置尝试、动作回执、观察窗口和最终结果。

档案状态：

~~~text
open → investigating → mitigating → observing → resolved
   └──────────────────────────────────────────→ escalated
   └──────────────────────────────────────────→ closed_false_positive
~~~

根因假设单独维护可信度：

~~~text
hypothesis → supported → confirmed
           └→ refuted
~~~

探测器生成的名称只能进入 hypothesis。supported 需要现场证据，confirmed 需要独立因果验证或操作员确认，并记录确认人和证据。新反证可以把既有结论转为 refuted。根因可信度与动作授权保持两条独立状态机，因此一个低影响、可回退动作可以在根因尚未 confirmed 时缓解影响。

### RiskPattern：跨时间聚合的长期风险

RiskPattern 按事件族、来源、目标、作用域和时间窗口聚合重复攻击、持续暴露与结构性风险。它保存：

- 首次与最近观测时间。
- 证据数量、独立来源数与受影响目标数。
- increasing、stable、decreasing 或 insufficient_data 趋势。
- active、mitigated 或 recurrent 状态。
- 真实事件、回放事件和演练事件的来源标记。
- 指向事实层的证据引用和查询范围。

默认保留窗口为 90 天，单条风险最多保留 20,000 个事件标识，当前存储最多维护 2,048 条聚合风险。达到容量边界时执行有序压缩，原始证据继续留在事实层。

### NetworkFeature：多案例支持的调查特征

NetworkFeature 从已验证档案和长期风险中提取可复用关系，例如“某类症状优先检查哪个信号”“某个动作的恢复时长分布”“某类故障的复发概率”。

晋升规则：

| 条件 | 当前默认值 |
|---|---:|
| 独立支持案例 | 至少 3 个 |
| 晋升置信度 | 至少 0.70 |
| 有效支持权重 | 至少 1.5 |
| 保留置信度 | 至少 0.55 |
| 衰减半衰期 | 90 天 |
| 撤销条件 | 至少 2 个反例，且反例比例达到 40% |

状态按 candidate → promoted → revoked 演进。只有 promoted 且作用域兼容的特征能进入探针排序。每次支持、反例、晋升、撤销和衰减都写入审计事件。

### 执行记忆：记录处置成败，约束复用方式

执行记忆保留四类记录：一次事件的情节、从多次事件归纳的语义、经过验证的处置程序、资产画像。召回采用分段 BM25、精确资产命中、精确业务对象命中和有界关系展开；启用向量索引后加入稠密候选，再按来源质量、时效和结构关系重排。

写入遵循以下规则：

- 只有完成回读和观察窗口的成功动作能增加正向处置经验。
- 无效动作与安全门拒绝单独留档，不增加成功率或根因置信度。
- 演示暂停造成的控制保持会被识别，避免制造“动作无效”样本。
- 冲突事实通过版本和 supersede 关系处理，检索优先当前有效版本。
- quarantine 停止召回并保留审计；redaction、tombstone 和 purge 分别处理脱敏、逻辑删除和物理清除。
- 在线容量预算当前为 64 条活跃记录，低效记录进入隔离区，事件账本继续保留其生命周期。

## 一条记忆如何真正影响调查

当前调查链路给出可核验的输入、状态变化与输出：

~~~text
事件对象
  → 按资产、故障族和证据词检索记忆
  → promoted 特征与经验证程序参与探针排序
  → RAG 检索厂商或系统文档
  → 执行排序后的只读探针
  → 当前输出支持、反驳或终止假设
  → 动作策略计算授权
  → 保存 memory_id、decision_id、变化字段与 influence
~~~

前端“本次调查上下文回执”同时展示历史记忆、知识文档和现场证据。三者在页面上分栏呈现：

- 历史记忆回答“过去哪些案例与这次相似”。
- 知识检索回答“设备或系统文档建议如何检查”。
- 现场证据回答“当前对象此刻处于什么状态”。

influence 接口记录记忆造成的具体变化，例如某探针从第 8 位升到第 1 位、9 个低价值探针被提前停止、复发次数达到阈值后转人工。只被召回但没有改变调查的记录不会计为有效影响。

## 有界自动处置

### 两条状态机解决两个问题

| 状态机 | 核心问题 | 关键输入 |
|---|---|---|
| 根因可信度 | 哪个解释可以进入长期知识 | 现场证据、反证、独立案例、人工确认 |
| 动作执行 | 当前动作是否安全且有效 | 当前信号、前置校验、影响范围、检查点、预算、锁、回读 |

动作状态：

~~~text
proposed → prechecked → committed → observing → passed
                                      └────────→ reverted
                    └──────────────────────────→ escalated
~~~

### 动作等级

| 等级 | 例子 | 自动执行条件 |
|---|---|---|
| L0 | 摘除异常目标、停止送流量、限流 | 已验证信号，影响范围明确 |
| L1 | 重启无状态服务、刷新租约、切换单个非上联接口 | 前置校验、单资产、前置状态快照；存在逆操作时必须注册回退合同 |
| L2 | ACL、路由、VLAN、交换机接口配置 | L1 全部条件，加设备级 confirmed commit 和健康探针 |
| L3 | 核心路由策略、凭据、固件、跨故障域批量变更 | 人工批准，加完整检查点与回退合同 |

当前在线动作目录注册了 restart_unit 和受限的 bounce_interface。网络设备 L2 配置框架已经实现策略、检查点、confirmed commit 接口和回退合同；真实交换机或防火墙的写适配器、管理凭据、冗余组与故障域尚未注册，因此线上 L2 配置写入会被安全门拒绝。

### 防止连续动作放大故障

系统使用预注册 RecoveryGraph 约束后续动作。A 失败后进入 B 需要同时满足：

1. A 已成功回退并完成回读。
2. 新采集证据支持 B。
3. A → B 是预注册边。
4. B 的影响范围不超过 A。
5. 故障、资产和故障域预算仍有余额。
6. 当前管理面、告警和保护探针保持可读。

当前默认预算：

| 约束 | 值 |
|---|---:|
| 单事件自动动作上限 | 2 |
| 单资产窗口内动作上限 | 2 |
| 单故障域窗口内动作上限 | 2 |
| 同一故障域并发 | 1 |
| 冷却期 | 600 秒 |
| 预算窗口 | 3,600 秒 |
| 退避 | 60 至 3,600 秒，带抖动 |

DeviceCheckpoint 保存配置摘要、完整备份位置、版本和恢复命令。DomainLock 保证同一冗余组只有一个在途写操作。EmergencyStop 独立持久化，状态不可读时所有写操作按 fail-closed 处理。回退失败、指标缺失、管理链路中断或保护指标恶化会立即停止后续自动动作并转人工。

## 整张内网的覆盖方式

覆盖范围由传感器和证据合同定义。系统目前组合以下视角：

- 网关会话与安全日志提供 L3/L4 通信、动作和管理面攻击。
- ARP 与邻居表提供 L2 身份和地址归属。
- DHCP 与身份账本提供租约、设备身份和地址漂移。
- 主机探针提供服务、接口、路由和本机健康。
- 哨兵时间线提供动作、观察、回退和最终结果。

environment.findings 记录已经证实的异常，environment.coverage 逐故障类列出当前盲区、缺失传感器和补采方式。没有 ARP、交换机 MAC 表或主机探针的网段会显示具体盲区，系统不会把“没有观测”解释为“没有问题”。

攻击面页面把资产、地址冲突、同广播域、会话冲突和共同目的地关系区分为硬证据与推断关系。每条发现都附带只读验证步骤、判定条件和整改入口。

![攻击面与证据关系](./docs/assets/ui/pentest_surface_graph.png)

![环境发现与传感器覆盖](./docs/assets/ui/pentest_environment.png)

环境感知的设计与数据合同见 [docs/ENVIRONMENT_PERCEPTION.md](./docs/ENVIRONMENT_PERCEPTION.md)。

## 前端如何对应后端对象

| 页面区域 | 用户要回答的问题 | 主要数据来源 |
|---|---|---|
| 内网实时 | 现在发生了什么 | 实时事实、哨兵检测、资产图 |
| 态势记录 | 系统已经确认什么，准备做什么 | IncidentDossier、RiskPattern、安全门 |
| 全链路拓扑剧场 | 当前事件走到哪一步，影响哪些对象 | 事件时间线、动作状态机、拓扑 |
| 诊断处置 | 哪些证据支持结论，记忆和文档如何参与 | 只读探针、执行记忆、RAG、influence |
| 多轮故障回放 | 长期记忆如何晋升、冲突、衰减和隔离 | 独立回放存储、记忆事件账本 |

记忆页面分成三段：

1. **真实事件处置记录**：跟随当前选中事件，显示本轮证据、档案、动作与记忆回执。
2. **离线记忆算法回放**：在独立临时目录运行 6 个留出案例 × 4 轮，展示晋升、强化、冲突、衰减和隔离。
3. **本机在线记忆演化**：读取 PostgreSQL 事件账本，默认展示概览，按需展开单条版本和归因。

离线回放用于快速展示数月尺度的算法变化，在线事件用于证明同一套写入、检索和 influence 链路正在接收真实事件。

## 评测：分别测正确性、贡献度和工程尺度

每个数字都绑定数据集、评测脚本和适用范围。准确率、检索召回、调查成本、索引延迟和线上处置结果分开报告。

### 1. 代码与真实链路检查

| 检查 | 当前结果 | 覆盖内容 |
|---|---:|---|
| Python 测试 | 1,227 passed，11 skipped | 业务对象、状态机、记忆、调查、安全门、处置与接口 |
| 前端测试 | 5 passed | 关键交互与数据适配 |
| 前端构建 | passed | TypeScript 与 Vite 生产构建 |
| 真实安全事件 | 写操作被安全门拒绝 | 决策理由、交接和失败经验落库 |
| 真实服务故障 | 自动恢复并完成双窗口回读 | 动作授权、执行、观察、结果记忆 |
| 记忆 + RAG 调查 | influence 非空 | 记忆命中、探针重排、知识检索和归因 |

测试数是 2026-08-24 主分支现场审计值。后续以 CI 输出为准。

### 2. 记忆贡献配对消融

memory_ablation 使用 5 个案例、每例 3 次重复，共 15 个配对实例。主比较为 M 与 A2：

- M：真实相关记忆。
- A2：相同数量、近似 token 体积的无关提示。
- A0：不执行探针，只在预算结束时评分。
- A1：使用相同推理器和工具，关闭记忆。
- A3：静态 runbook。

当前配对结果：

| 指标 | M | A2 | 差异 |
|---|---:|---:|---:|
| 可执行案例成功数 | 10/10 | 10/10 | 0 |
| McNemar mid-p |  |  | 1.0 |
| 上下文 token 差异 |  |  | 每例不超过 2.56% |

当前小规模稳定网络中的结论是“成功率收益尚未检出”。两个臂都完成同一结论，后续主指标转向达到同一结论所需探针数、调查时长、回退次数和人工升级率。按当前效应假设，40 个样本约有 38% 检验功效，60 个样本约有 65%，因此线上长期数据仍需继续积累。

memory_contribution 还会直接读取 ClickHouse 当前事实，构造 12 个真实设备问题，测量达到完整扫描同一结论所需的探针数：

| 现场事实评测 | 达到结论 | 平均探针数 | 中位数 |
|---|---:|---:|---:|
| M，相关画像记忆 | 12/12 | 8.333 | 9.0 |
| A1，固定原始探针顺序，无画像 | 12/12 | 8.500 | 9.0 |
| A2，等量无关提示 | 12/12 | 8.583 | 9.0 |
| A0，不执行探针 | 0/12 | 无 | 无 |

M 相对 A2 平均少 0.25 条探针，95% bootstrap CI 为 0.00 至 0.75，配对随机化 p=1.0。执行探针的三个臂都达到完整扫描结论，A0 空跑为 0/12；相关画像相对等量无关提示的额外收益仍低于预设显著性门槛。

运行：

~~~bash
python3 -m core.eval.memory_ablation
python3 -m core.eval.memory_contribution
~~~

### 3. R230 六案例组件消融

examples/benchmarks.py 使用 6 个真实 R230 FortiGate 留出案例和确定性规则推理器，逐项关闭或放开组件：

| 配置 | 根因分类准确率 |
|---|---:|
| 轻量路径，启用技能筛选 | 6/6 |
| 完整上下文 | 6/6 |
| 关闭记忆 | 6/6 |
| 放开全部工具，关闭技能筛选 | 1/6 |

同一实验的 4 轮冷启动与热启动探针数均为 32，准确率均为 1.0，记忆记录从 0 增至 19。这个留出集支持“技能筛选是当前决定性组件”，同时给出两个零增益结果：记忆没有提高这 6 例的准确率，4 轮整理没有减少探针数。6 个案例用于组件定位，统计结论继续依赖更大的配对评测。

运行：

~~~bash
python3 examples/benchmarks.py
~~~

### 4. LongMemEval-500：检索能力

当前 TieredMemoryStore 在 LongMemEval-500 上重新运行，recall@5 为 0.970。旧版词袋检索为 0.906。下表中的 Mem0、Reflexion 和 BM25 数字来自仓库保存的同数据集、同 recall@5 口径结果；它们用于定位检索能力，未覆盖本项目的业务对象状态机、动作回读或 influence。保存结果见 [results_core.json](./eval_mem_compare/results_core.json) 与 [results_mem0.json](./eval_mem_compare/results_mem0.json)。

| 方法 | LongMemEval-500 recall@5 | 结果来源 |
|---|---:|---|
| 旧版词袋检索 | 0.906 | 仓库历史结果 |
| Mem0，infer=False | 0.916 | 同口径保存结果 |
| Reflexion | 0.918 | 同口径保存结果 |
| BM25 基线 | 0.970 | 同口径保存结果 |
| 当前 TieredMemoryStore | 0.970 | 2026-08-24 本地重跑 |

这个结果支持“分段 BM25 已达到该数据集上的词法基线”。它没有测量处置成功率；处置贡献由配对消融和真实链路回读测量。

### 5. FortiOS 文档检索：保留有效增益，也保留失败阶段

数据集包含 FortiOS 7.4 Administration Guide 的 1,145 个章节、9,014 个切片，以及 6 个冻结标签的真实 R230 事件。样本规模较小，表格保留每一阶段的真实结果：

| 检索阶段 | Recall@1 | Recall@5 | Recall@10 | nDCG@10 |
|---|---:|---:|---:|---:|
| BM25 原文 | 0.000 | 0.250 | 0.333 | 0.179 |
| 上下文标题 | 0.000 | 0.250 | 0.333 | 0.182 |
| BM25 + 稠密候选 | 0.083 | 0.167 | 0.417 | 0.267 |
| 交叉编码器重排 | 0.000 | 0.250 | 0.417 | 0.192 |

混合候选把 Recall@10 从 0.333 提高到 0.417。确定性标题增强接近零增益，当前交叉编码器降低了 nDCG@10，因此它们没有被写成默认质量提升结论。完整口径见 [docs/BENCHMARKS.md](./docs/BENCHMARKS.md)。

### 6. HNSW 百万级召回与延迟

该实验使用 1,000,000 条确定性合成高斯向量、128 维、Flat 精确近邻作为真值，测量单请求 Recall@10、P95、QPS、构建时间和索引体积。

| 配置 | Recall@10 | P95 |
|---|---:|---:|
| HNSW ef=32 | 0.443 | 0.97 ms |
| HNSW ef=1024 | 0.846 | 21.4 ms |
| Flat 精确检索 | 1.000 | 36.4 ms |

HNSW 冷构建 909.7 秒，索引体积 784 MB。该实验说明索引配置的召回与延迟前沿，业务检索质量由前两项检索评测给出。

![HNSW 百万级召回与延迟前沿](./docs/assets/hnsw_frontier.png)

原始结果见 [benchmark_results/vector_index_1m.json](./benchmark_results/vector_index_1m.json)，方法见 [docs/HNSW_SCALE_BENCHMARK.md](./docs/HNSW_SCALE_BENCHMARK.md)。

### 7. 分段 BM25 的持续变更成本

十万文档、20% 变更实验比较旧版查询时全量重建和当前热增量段实现：

| 实现 | 查询 P95 | Top-10 一致性 |
|---|---:|---:|
| 查询时全量重建 | 929.8 ms | 1.000 |
| 热增量段 + 封存段 | 12.0 ms | 1.000 |

77.75 倍来自消除查询路径上的全量重建，适用于旧实现到当前实现的迁移收益。常驻索引的公平比较结果约为 12.06 倍，见 [benchmark_results/index_lifecycle_fair_100k.json](./benchmark_results/index_lifecycle_fair_100k.json)。

![分段 BM25 在持续变更下的查询延迟](./docs/assets/bm25_incremental.png)

原始迁移实验见 [benchmark_results/index_lifecycle_100k.json](./benchmark_results/index_lifecycle_100k.json)。

### 8. 向量索引版本更新、删除与压缩

十万条 128 维向量经过 10,000 次更新和 10,000 次删除后，版本表立即过滤旧版本与墓碑，后台压缩生成新的基础代际：

| 指标 | 压缩前 | 压缩后 |
|---|---:|---:|
| 物理向量 | 110,000 | 90,000 |
| 活跃向量 | 90,000 | 90,000 |
| 过期物理向量 | 20,000 | 0 |
| 查询 P95 | 1.3421 ms | 0.9751 ms |
| Recall@10 | 0.898 | 0.912 |

压缩回收 20,000 条物理向量，快照重载 0.7162 秒，重启前后查询结果一致。该实验测量版本过滤、压缩和快照恢复，数据见 [benchmark_results/vector_lifecycle_100k.json](./benchmark_results/vector_lifecycle_100k.json)。

### 9. ITBench 的位置

ITBench SRE、CISO 和 FinOps 数字保留为公开研究参照。当前仓库没有在 ITBench 专用集群上完成本地跑分，因此 README 不列本地对比排名。项目本身的可复验结论来自本节列出的真实事件、配对消融、LongMemEval、FortiOS 检索和索引实验。

## 可复现命令

~~~bash
# 全量 Python 测试
python3 -m pytest -q tests_py/

# 前端测试与生产构建
cd frontend
npm test -- --run
npm run build

# 记忆检索与贡献评测
cd /data/Autopoiesis-AgentSys
python3 -m core.eval.memory_ablation
python3 -m core.eval.memory_contribution
python3 -m core.eval.longmemeval tmp/longmemeval_s.json

# 索引实验
python3 -m core.eval.vector_index_benchmark
python3 -m core.eval.index_lifecycle_benchmark
~~~

部分评测需要本地数据集或可选依赖。脚本会在输出中标明实际数据源、降级路径和缺失项。

## 目录与职责

~~~text
core/memory/         执行记忆、分段 BM25、向量候选、关系索引、持久化
core/evolve/         写入路由、冲突消解、衰减、隔离、反思与整理
core/context/        有预算的证据上下文编译
core/orchestrator/   意图路由、任务编排、技能调度与升级
core/skills/         技能注册表和工具合同
core/verifier/       引用、前置条件、后置条件与证据核验
core/remediate/      观察窗口、保护探针、回退与恢复图
core/eval/           记忆贡献、检索、RAG 和索引评测
domains/network_rca/ 内网事件、三类业务记忆、设备画像与处置规则
domains/active_recon/只读侦察、攻击面与整改 playbook
frontend/            React/Vite 前端与 FastAPI 网关
scripts/             受控事件、记忆调查和演示操作手册
~~~

关键实现：

- [IncidentDossier](./domains/network_rca/incident_dossier.py)
- [RiskPattern](./domains/network_rca/risk_pattern.py)
- [NetworkFeature](./domains/network_rca/network_feature.py)
- [业务记忆汇聚](./frontend/gateway/app/operational_memory.py)
- [有界处置入口](./frontend/gateway/app/remediation.py)
- [记忆与 RAG 演示](./scripts/demo_memory_rag.sh)

## 持久化与可观测

PostgreSQL 在同一事务中提交当前记录和追加式事件。乐观版本检查拒绝并发覆盖。索引消费端按单调偏移投影到 BM25、资产索引和可选向量索引，所有投影成功后推进检查点。

每次调查与处置保存：

- run_id 和稳定事件标识。
- 检索候选、分数分解和最终上下文。
- 实际工具调用、退出码和输出摘要。
- 假设状态变化及证据引用。
- 动作策略、前置条件、预算与锁。
- 动作回执、观察样本、回退和最终结果。
- 记忆写入、版本变化和 influence。

健康接口、事件详情和记忆事件流共同定位“代码已部署、数据已进入、对象已形成、记忆已使用、动作已验证”五个阶段。实现说明见 [docs/EXECUTION_OBSERVABILITY.md](./docs/EXECUTION_OBSERVABILITY.md)。

常用核验接口：

| 接口 | 核验对象 |
|---|---|
| GET /api/healthz | 服务、持久化、整理计数和后台错误 |
| GET /api/rca/operational-memory | 三类业务记忆与四个输入源状态 |
| GET /api/rca/memory/events | 执行记忆的追加式生命周期事件 |
| GET /api/rca/memory/{memory_id}/influence | 单条记忆影响过的调查决定 |
| GET /api/rca/event-memory-receipt | 当前事件的证据、档案、经验与引用回执 |
| GET /api/rca/remediation/actions | 已注册动作及其等级和条件 |
| GET /api/rca/remediation/safety | 预算、锁、暂停状态与观察参数 |

## CI / CD

主分支流水线执行：

1. Python 3.11 全量测试和数据清单校验。
2. Node 20 前端测试、TypeScript 检查和 Vite 构建。
3. 手动或定时的确定性评测冒烟。
4. push 到 main 后，自托管 R450 runner 快进代码、构建前端、刷新网关依赖、重启服务并检查 /api/healthz。
5. 版本标签触发控制台镜像构建并推送 GHCR。

部署成功还需要核对最新事实时间、业务记忆刷新结果和具体演示链路。配置与 runner 说明见 [docs/CI_SETUP.md](./docs/CI_SETUP.md)。

## 当前能力边界

| 能力 | 当前状态 |
|---|---|
| FortiGate 安全事件与全量流量采集 | 在线持续写入 |
| IncidentDossier、RiskPattern、NetworkFeature | 在线生成并持久化 |
| systemd 隔离服务自动恢复 | 已接入真实执行、观察与回读 |
| 记忆重排探针、RAG 与 influence | 已在真实调查演示中验证 |
| 防火墙攻击事件自动封禁 | 当前场景由安全门保留配置并转人工 |
| 交换机与防火墙 L2 配置写入 | 策略与接口已实现，真实设备适配器和凭据未注册 |
| 整网故障覆盖 | 按传感器覆盖，盲区通过 environment.coverage 明示 |
| 长期记忆收益 | 已具备在线归因与配对评测，当前小样本成功率增益未检出 |

这张表是报告、答辩和面试中推荐使用的项目边界。每一项都能继续追问到输入、状态机、执行条件、输出和验证证据。

## 参考

记忆与评测设计参考 CoALA、Mem0、A-MEM、Generative Agents、LongMemEval 和 StreamBench。自动处置设计采用预算、冷却、故障域并发限制、双观察窗口、confirmed commit 与自动回退等工程实践。索引实现参考 FreshDiskANN、SPFresh 与 Quake。

详细实验口径见 [docs/BENCHMARKS.md](./docs/BENCHMARKS.md)，动态索引研究见 [docs/INDEX_LIFECYCLE_RESEARCH.md](./docs/INDEX_LIFECYCLE_RESEARCH.md)。
