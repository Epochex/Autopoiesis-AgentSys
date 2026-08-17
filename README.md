# Autopoiesis-AgentSys

**一套内网态势感知的多智能体系统，跑在一个可迁移的自演化 Agent 核心（AgentSys）之上。** 系统对真实 FortiGate/R230 内网日志做实时态势处理、根因分析、攻击面侦察与环境盲区点名；下面这层 AgentSys 把「连续运行数周的智能体会踩的坑」做成一套可测量、可回放、可演化的内核——记忆变旧或被污染、上下文无节制膨胀、技能过多干扰模型、有用经验沉淀不成稳定策略——并以清晰的域接口暴露出来，让内网态势感知只是它落地的第一个域。全部数字出自 [`core/`](./core) + [`domains/`](./domains) 的确定性 Python 内核。

<p align="left">
<b>百万级向量索引</b>&nbsp;·&nbsp;<b>8900 万+ 实时网络事件接入</b>&nbsp;·&nbsp;<b>十万条持续变更索引</b>&nbsp;·&nbsp;<b>573 项确定性测试</b>&nbsp;·&nbsp;<b>真实生产日志留出集</b>
</p>

系统分两条主线，界面按这两条线展开：**在线态势线**——事件流进来，多智能体做关联、根因、侦察、检索，产出可核验的处置建议；**后台演化线**——核验通过的运行轨迹回流成记忆，经写入路由、冲突消解、效用驱逐沉淀为稳定策略，并投影进可增量维护的索引层。

```text
在线态势:  事件流 → 关联/簇窗口 → 记忆召回(BM25/资产/关系, 可选 HNSW) → 上下文编译 → 技能调度 → 多智能体推理 → 验证 → 处置预案 + trace
后台演化:  核验通过的 trace → 记忆整合/反思 → 冲突消解/效用驱逐 → PostgreSQL 事务提交 → 索引事件投影 → 后台压缩
```

设计主线一句话：**在线路径保持小，后台路径负责学。**

---

## 1 · 实时态势感知：全链路事件流处理

系统的第一现场是内网态势。真实 FortiGate/R230 syslog 经 Redpanda 跨节点事件流接入，累计接入 **8900 万+ 网络事件与 93 万+ 告警**；事件时间水位线、迟到窗口、去重与原子检查点保证乱序处理与重启恢复。态势台把一条告警从原始事件走到处置预案的六段管线全部摊开——**关联器 → 告警流 → 簇窗口 → AIOps 推理 → 建议流 → 处置预案**——每段都标注真实耗时与该段用的模型（规则推理器或外部 provider）。

![实时态势 · 全链路事件流](./docs/assets/ui/live_situation_chain.png)

界面右侧是被点选告警的诊断详情：诊断时间线（首个告警→簇末告警→AIOps 推理→建议产出→假设评审→预案生成）、各阶段巡逻记录、带支持/反证计数的假设集，以及只读的处置预案草案（每一步都标「只读核查」还是「需人工审批」）。整套「全链路拓扑剧场」可全屏展开，把该告警走过的自愈路径在设备/记忆拓扑上高亮。

## 2 · 多智能体编排：路由、调度、升级、验证

在线路径是一个门控的多智能体系统，不是单模型直答：

- **级联意图路由**：规则快路径处理高频确定请求，语义检索召回候选技能，复合与歧义请求升级 Agent，未命中触发技能库自扩展（回放回归门通过后才入库）。
- **技能注意力调度**：相关性做硬门，学到的成功率与误用率在相关集内排序。这是承重组件——六例真实留出集上，开启技能调度根因准确率 **100%**，关闭后落到 **16.7%**。
- **自适应升级**：单 Agent 按证据歧义与影响面门控升级为 planner-executor-critic。
- **验证**：写动作执行前检查前置条件与人工审批凭证，执行后检查后置条件、不变量与真实状态回读，失败立即停止并执行可回读补偿；诊断侧拒绝无引用、虚构引用与矛盾证据。独立的 LLM-as-judge 评审接口与主推理隔离，只做语义支持度打分。

## 3 · 自演化记忆生命周期（核心机制）

这层是 AgentSys 的核心论点：**面向连续运行数周的智能体，把记忆做成一个可测量、可回放、可演化的生命周期**，而不是一个只增不治的向量库。它是全系统的重点机制，但只是系统的一部分——上面两条态势/编排线才是它服务的对象。

- **三层记忆**（情景/语义/程序）+ 写入路由（ADD/UPDATE/NOOP）+ 可展开关联链 + 重要度门控反思。事件带观测时间、来源轨迹和类型化关系，`similar_to` 只参与召回。
- **在线混合召回**：分段 BM25、精确资产命中与有界两跳关系展开产生候选，`AUTOPOIESIS_ENABLE_VECTOR_MEMORY=1` 后加入 HNSW 语义候选并用结构先验重排。每条候选的词法分、向量分、资产命中、图跳数和最终分都进执行轨迹。
- **写入侧生命周期**：冲突消解 `supersede`、容量预算下的效用驱逐（保护先验）。

**记忆观测舱**把内核的每一次生命周期事件回放出来——三层记忆网格逐条写入、加固、抽象为洞见（185 次真实生命周期事件），右侧逐字段可审计单条记忆的置信/重要度/强度、本步写入路由与召回归因。全部读自真实内核运行 `core/evolve/observatory.py`，无合成。

![记忆观测舱 · 三层记忆生命周期](./docs/assets/ui/memory_observatory.png)

**写入路由 `route()`** 是生命周期的入口判决：每条候选按与既有记忆的相似度落在 ADD/UPDATE/NOOP 三区。真实 R230 数据集上 6 次调用全部落在 ADD 区（相似度 0.00–0.40，离 0.62 的 UPDATE 闸门还差 0.22），标尺如实标出「本数据集路由结果 — ADD ×6 · UPDATE ×0 · NOOP ×0」，不粉饰。

![写入路由 · route() 相似度标尺](./docs/assets/ui/write_route_ruler.png)

**上下文包**是喂给下游推理器的最终记忆，跟随回放展示每次召回如何拼装、如何变、为何进包；点开「完整上下文」可看每条记忆的排名升降、得分分解与召回后被丢弃项的 DIFF。

![上下文包 · 递交给推理器的记忆](./docs/assets/ui/context_packet.png)

### 记忆检索：追平词法上限，超越已发表方案

检索核心从词袋匹配升级为分段 BM25 后，LongMemEval-500 的 recall@5 从 0.906 提到 **0.970**，追平该任务的 BM25 词法天花板，并高于 Mem0（infer=False，0.916）、Reflexion（0.918）与 Claude Code 式原子/索引记忆（0.946）。评测逐位复现各方案自报口径，全程 LLM-free。

![Memory retrieval vs. baselines](./docs/assets/memory_recall.png)

记忆系统的真正差异化在写入侧的生命周期治理。事实更新场景下，时序互近邻 `supersede` 在旧记忆改写同实体根因时将其退役，最新答案的 recall@1 提升 **21.8 个百分点**，陈旧答案冒到首位的比例下降 **63%**。

![Fact-update lifecycle](./docs/assets/supersede_gain.png)

### 混合检索实录：真实案例可回放

检索页不是抽象管线图，而是可回放的真实案例实录：顶部切换真实 RCA 案例（`eno1 无载波` / `showroom→office 不可达`，均为 phase-1 轨迹里 5× 真实运行），▶ 播放/点阶段跳步；正文里每篇命中文档带真实标题、真实正文、命中词高亮与全程轨迹徽章（命中#→融合#→上下文），末步「组装上下文」是真实喂给下游的段落加 token 预算填充。诚实降级：本机向量/重排离线时如实标注、图跳未扩展时写明「命中记忆之间无关系边可跳」。

![混合检索 · 真实案例实录](./docs/assets/ui/retrieval_record.png)

| 能力 | 场景 | 结果 |
|---|---|---|
| 混合知识检索 | 9014 条厂商文档切片 recall@10 | BM25 主链路 + 稠密补充，0.33 → **0.42** |
| 检索路由 | 稠密路失败模式归因 | 86% 为时序歧义（对实体、错事件），据此固定按数据类型选路 |
| 上下文编译 | 2048 token 预算、八段结构化上下文 | 空分区预算回收、必需证据全保留、根因准确率不受损 |
| 技能注意力调度 | 六类真实留出事件根因准确率 | 开启 **100%**，关闭 16.7%（承重组件） |
| 效用驱逐 | 容量预算 B=10 | 优于 Ebbinghaus 衰减、LRU 与随机 |

## 4 · 索引层：规模与持续变更下的召回–延迟前沿

### 向量索引：百万级规模上的召回–延迟前沿

FAISS 索引规模压测在确定性合成高斯向量上以 Flat 精确结果为真值，实测十万与百万规模的构建时间、索引体积、P95、吞吐与 Recall@10。百万条 128 维向量上，HNSW 通过 `efSearch` 扫出一条完整的召回–延迟前沿：从 `ef=32` 的 0.443@0.97ms 到 `ef=1024` 的 **0.846@21.4ms**，全程单请求 QPS 高于 Flat 精确检索（Flat P95 36.4ms、QPS 27.9）；冷构建 909.7s、索引 784MB。

![HNSW recall-latency frontier](./docs/assets/hnsw_frontier.png)

### 索引生命周期：持续变更下压掉两个数量级

BM25 从查询时全量重建改为热增量倒排 + 不可变封存段 + 删除标记 + 后台压缩，查询按活跃集合的全局统计统一评分。十万条文档、20% 变更量下，查询 P95 从 929.8ms 降到 **12.0ms（77.75 倍）**，压缩回收 25% 物理条目且 Top-10 完全一致。

![BM25 incremental index](./docs/assets/bm25_incremental.png)

向量侧同理：HNSW 承担不可变基础代际，新版本进入精确增量层，删除由版本表立即过滤，后台锁外重建并原子切换。十万条向量经一万次更新与一万次删除后压缩回收两万条旧向量，P95 从 1.34ms 降到 0.98ms，重启结果一致。

## 5 · 攻击面侦察与环境盲区点名

态势感知的另一半是「事故被记录之前」的主动认知。系统在授权内网上做只读侦察与环境扫描，产出可读的攻击面而不是一堆分数。

**攻击面图**把 52 个真实资产按已挖掘关系连成图（96 条关系），地址/会话冲突、同广播域、同目的地等边分「硬证据/推断」两态；10 个暂无已挖掘关系的资产被单独点名——「不是没有关系，是这批证据里没找到」。

![攻击面 · 资产关系图](./docs/assets/ui/pentest_surface_graph.png)

**地址空间占用热力图**按 /24 铺开每个地址的身份归属（已绑定身份/有流量无绑定/地址争用/无观测），把「一个地址被静态设备与服务器同时占用」这类地址争用用红格直接标出。

![地址空间占用 · 三网段热力图](./docs/assets/ui/pentest_port_heatmap.png)

每条结论点开是一套**可亲自运行的验证/修复 playbook**：目标、授权前提、编号步骤、判定、整改、证据来源。诚实门控——只读步骤（`ip neigh` / `nmap` / `openssl`）标「安全」可复制直接跑；入侵/利用步骤标红斜纹「需审批·勿直接跑」且 `# GATED · payload/wordlist withheld`，与「入侵动作停在闸门从未执行」一致；只在 RFC5737 文档网段/授权资产上参数化。

![判定 · 每条结论带可运行 playbook](./docs/assets/ui/pentest_findings_playbook.png)

**环境感知**在事故被记录之前扫描原始网关语料，指出地址重复、作用域内无租约绑定、租约反复重建、单主机多地址、会话元组冲突、地址池压力、管理面凭据攻击。报告分两半：`findings[]` 是现有数据源能证明的，`coverage[]` 按故障类点名现有数据源无法证明的部分并写明补哪个传感器能覆盖。

![环境感知 · 授权范围与传感器覆盖](./docs/assets/ui/pentest_environment.png)

这一半来自一次真实事故：192.168.1.23 被一台静态配置设备与服务器同时占用数周，唯一身份来源是网关 DHCP 服务器而占用方从未发过 DHCP 包，L2 身份源（ARP / neighbour 表）补上这一类，判定跨采集序列做归属漂移。判定同时使用实时源与全历史：每个源标注是否仍在写入，每条判定出报告前对仍在写入的源复核一次。设计见 [docs/ENVIRONMENT_PERCEPTION.md](./docs/ENVIRONMENT_PERCEPTION.md)。

## 6 · 基准与可复现

评测为 LLM-free、确定性、可复现。基准场景把系统四条能力线映射到公开/自建基准，界面复用同一套态势 UI（换数据源不换 UI）。

![能力 → 基准 → 真实指标 映射](./docs/assets/ui/bench_capability_map.png)

- **内网根因分析 → 网络 RCA 自愈回放（真跑）**：真实 R230 FortiGate 留出集（6 类事件 × 4 pass，规则推理器）上取证 32 次，根因准确率与引用核验均为 100%，关闭技能调度后落到 16.7%；6 真实用例注入 Redpanda 隔离 topic 后端到端检测/校验/自愈 100%、记忆自演化 +19。
- **长期记忆/检索 → LongMemEval-500（真跑）**：recall@5 = 0.906，BM25 词法上限 0.97 领先（如实不粉饰）。
- **自我渗透/安全 → ITBench·CISO（公开定义）**、**内网 RCA → ITBench·SRE（公开参照）**：ITBench 为公开基准（IBM，ICML 2025，arXiv:2502.05352），SRE 11.4% / CISO 25.2% / FinOps 25.8% 为公开 SOTA 基线，本地专用集群跑分标注「待跑」，未伪造本地分。

![LongMemEval-500 · 记忆检索](./docs/assets/ui/bench_longmemeval.png)

```bash
python3 examples/benchmarks.py        # §1–§3，真实 R230 集
python3 -m pytest tests_py/ -q        # 全量测试，573 passed / 8 skipped
```

## 7 · 可迁移核心：AgentSys 模块

内网态势感知是第一个落地域，但域逻辑与内核解耦——AgentSys 是可迁移的那一层，任何长周期智能体域都能复用：

- `core/memory/` — 三层记忆、BM25 检索核心、hybrid_kb 混合检索器、拓扑图记忆
- `core/evolve/` — 写入路由、A-MEM、反思、冲突消解 supersede、效用驱逐、自演化流、observatory
- `core/context/` — 结构化预算上下文压缩
- `core/orchestrator/` — 级联意图路由、自适应编排、技能调度
- `core/skills/` — 技能注册表、技能诱导、契约
- `core/verifier/` — 契约验证、引用核验

域接入只需实现自己的证据源与技能契约：`domains/network_rca`（内网 RCA，首个落地）、`domains/active_recon`（只读侦察/加固报告）、`domains/enterprise_ops`（企业运维/定价工作流，合成 fixture）都挂在同一套内核上。

## 前端与可观测

[`frontend/`](./frontend) 是 React/Vite 战术态势界面与 FastAPI 网关。`POST /api/rca/diagnose` 使用服务级长生命周期运行时，核验通过后整合记忆并触发索引维护；`/api/healthz` 暴露持久化、事件投影、索引代际、压缩线程与失败状态。后端另存逐节点追加式轨迹，覆盖召回、演变分析、技能与工具、上下文、推理、核验、记忆提交、事件持久化与后台索引维护，`run_id` 定位单次运行、`session_id` 聚合同一事故的多次运行，查询接口返回失败、部分完成、未完成节点、瓶颈与跨运行退化信号。实现见 [`docs/EXECUTION_OBSERVABILITY.md`](./docs/EXECUTION_OBSERVABILITY.md)。

`frontend/script/vreview.mjs` 用 Playwright 驱动真实浏览器做可测量前端验证（实际裁切、axe 对比度、横向滚动、console 错误、像素 diff）；本 README 的界面截图即用同一套 Playwright 驱动从真实运行中裁出。

## 事实持久化

PostgreSQL 当前状态表与只追加事件流在同一事务提交，乐观版本拒绝并发覆盖；消费端按单调偏移把事件投影到 BM25、资产索引与向量索引，全部成功后推进检查点。

## 数据

- 真实：网络设备日志、厂商技术文档、IODA v2 断网事件池、LongMemEval-500。
- 真实告警与日志含内外网 IP，走 gitignore 本地留存；仓库内带脱敏 seed 与合成 fixture，克隆后基准回退到 seed 并标注所用数据集。

## 目录

```text
core/memory/        三层记忆、BM25 检索核心、hybrid_kb 混合检索器、拓扑图记忆
core/evolve/        写入路由、A-MEM、反思、冲突消解 supersede、效用驱逐、自演化流、observatory
core/context/       结构化预算上下文压缩
core/orchestrator/  级联意图路由、自适应编排、技能调度
core/skills/        技能注册表、技能诱导、契约
core/verifier/      契约验证、引用核验
core/eval/          确定性基准、混合检索评测、独立模型证据评审与配对消融
core/llm/           OpenAI 兼容 provider（外部 API 与本地 GPU 后端）
domains/network_rca  首个落地域：内网 RCA
domains/active_recon 只读侦察 / 加固报告
domains/enterprise_ops 企业运维 / 定价工作流（合成 fixture）
frontend/           React/Vite 战术态势界面 + 记忆 observatory + FastAPI 网关
```

## 推理后端

在线路径使用 OpenAI 兼容 provider，支持外部 API 与本地 GPU 部署两种后端；确定性基准走内置规则推理器；语义支持评测使用隔离的模型评审接口。核心评测与命令行路径读取 `AUTOPOIESIS_LLM_*` 配置。

## CI / CD

`.github/workflows/` 下五条流水线：

- **CI** — `python-ci`（3.11 装 `.[dev]`、跑全量 `tests_py`、mock-labeled Phase 1.5 报告、校验真实数据集 manifest）、`frontend-ci`（Node 20、tsc 类型检查 + vite 构建为 merge gate、eslint 为 advisory、vitest）、`benchmarks`（手动/周定时的确定性基准冒烟）。
- **CD** — `release`（打 `v*` tag 时用 `frontend/Dockerfile` 构建控制台镜像并推到 GHCR，仅用内置 token）、`deploy`（push 到 main 时在 r450 上的自托管 runner 直发：ff 到 main → `npm run build` → 刷新 gateway 依赖 → `systemctl restart netops-ops-console-backend` → `/api/healthz` 健康检查；r450 同时是开发机，`deploy.sh` 对脏工作树 fail-safe 绝不覆盖未提交改动）。

见 [docs/CI_SETUP.md](./docs/CI_SETUP.md)。

## roadmap

- 候选改进须通过验证器与回放门才生效，Agent 不能自由改写生产行为。
- GRPO 组相对策略优化在 [`core/evolve/`](./core/evolve) 有确定性规则版实现，在线路径使用规则推理器与 OpenAI 兼容 provider，GPU 侧梯度训练在 roadmap 中。

## 研究参考

CoALA（arXiv:2309.02427）· Mem0（2504.19413）· A-MEM（2502.12110）· Generative Agents（2304.03442）· StreamBench（2406.08747）· LongMemEval（2410.10813）· ITBench（2502.05352）· FreshDiskANN · SPFresh · Quake。记忆研究引用见 [docs/BENCHMARKS.md](./docs/BENCHMARKS.md)，动态索引研究见 [docs/INDEX_LIFECYCLE_RESEARCH.md](./docs/INDEX_LIFECYCLE_RESEARCH.md)。
