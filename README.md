# Autopoiesis-AgentSys

长周期 Agent 的自演化内核。项目分两部分:被基准测量的 [`core/`](./core) + [`domains/`](./domains) Python 内核,以及把它可视化的 [`frontend/`](./frontend) 前端;首个落地场景是基于真实 FortiGate/R230 内网日志的根因分析(RCA)。所有可复现的数字都出自 Python 内核。

这个仓库不解决通用编排。它针对的是 Agent 连续运行数周后才暴露的失效模式:记忆变旧或被污染、上下文无节制增长、技能太多干扰模型、有用的经验始终沉淀不成稳定策略。

设计取向是一句话:**在线路径保持小,后台路径负责学。**

```text
在线:  任务 → 记忆检索 → 上下文编译 → 技能注意力调度 → 单 Agent 执行 → 验证器 → trace 账本
后台:  trace 账本 → 记忆整合/反思 → 冲突消解与效用驱逐 → 技能打分与剪枝 → 回放晋升门
```

## 核心取向:记忆是生命周期,不是一次检索

RAG 是检索原语,不是系统边界。这个仓库把记忆当作一个生命周期来做:原始 trace 保留,干净的经验被整合成情景/语义/程序三层记忆,矛盾或过期的记忆被退役,上下文在预算下被编译,只有一小份任务相关的技能被暴露给模型。

这一点有实测支撑,也有实测打脸的地方,下面如实写。

## 记忆系统

- **三层记忆**(情景/语义/程序)+ Mem0 式写入路由(ADD/UPDATE/NOOP)+ A-MEM 关联链 + 重要度门控反思(Generative-Agents 式)。
- **检索核心是 Okapi BM25 全文 + 有界结构重排**(层级先验、A-MEM 中心度、重要度、recency),结构项只在近似平分时改变排序,不覆盖明显的词法赢家。
- **写入侧生命周期**:冲突消解 `supersede`(新记忆改写同实体根因时退役旧的)、效用驱逐(容量预算下按效用淘汰,保护先验不动)。
- **索引生命周期**:BM25 使用热增量倒排、不可变封存段、删除标记和后台压缩，查询按活跃集合的全局统计统一评分；十万条、20% 变更量下相对每次查询重建索引的 P95 快 **78.49 倍**，压缩回收 **25%** 物理条目且 Top-10 完全一致。
- **事实持久化**:可选 PostgreSQL 当前状态表与只追加事件流在同一事务提交，使用乐观版本拒绝并发覆盖，并用单调事件偏移驱动索引校验和重建；本地 BM25/HNSW 是可丢弃的派生索引，不承担业务事实源。设置 `AUTOPOIESIS_MEMORY_DSN` 后，进程重启会恢复完整记忆并重建检索索引。

在 LongMemEval-500 的同一 LLM-free recall@k 指标下(该 harness 逐位复现仓库自报的数字):

- 检索核心从早期的词袋匹配改成 BM25 后,recall@5 从 **0.906 提到 0.970**,追平 BM25 词法天花板,并高于 Mem0(infer=False,0.916)、Reflexion(0.918)、以及 Claude Code 式原子/索引记忆(0.946)。
- **诚实结论:记忆系统的价值不在原始检索**——纯 BM25 就是这个任务的天花板,加稠密向量或结构信号都无提升。它的差异化在写入侧:`supersede` 在事实更新场景把最新答案检索 **+20.5pt@k=1**、陈旧答案冒头 **−72%**;效用驱逐在容量预算下优于 LRU / Ebbinghaus 衰减 / 随机。

## 检索 / RAG

- **生产用纯 BM25**(稀疏),在标识符/日志密集的网络数据上就是上限。
- **可插拔混合检索器** `core/memory/hybrid_kb.py`:BM25 + dense(bge / faiss-HNSW)+ RRF 融合 + cross-encoder 精排。**按数据类型路由**:自然语言文档用它,日志/事件不用。
  - 真 FortiOS 7.4 管理手册语料(9014 chunk、非循环内容标签)上,混合把 recall@10 从 **0.33 提到 0.42**。
  - IODA 事件检索上,等权 RRF 混合反而**低于**纯 BM25(0.245 vs 0.264)。根因诊断见 [`core/eval/HYBRID_DIAGNOSIS.md`](./core/eval/HYBRID_DIAGNOSIS.md):把 dense 路降权(w≈0.1–0.5)后混合微超 BM25(0.266);dense 的主要失败模式是时序歧义(对实体、错事件,占 86%),不是标识符模糊(11%)。
- **FAISS 索引规模压测**:同一批确定性向量上以 Flat 精确结果作真值,实测 10 万与 100 万规模的构建时间、索引体积、P95、吞吐和 Recall@10。百万条 128 维向量上,HNSW 在 `efSearch=1024` 时 Recall@10 为 **0.846**、P95 **21.37 ms**,Flat P95 **36.42 ms**;代价是冷构建 **909.70 s**、索引 **784.13 MB**。完整参数曲线与复现命令见 [`docs/HNSW_SCALE_BENCHMARK.md`](./docs/HNSW_SCALE_BENCHMARK.md)。
- **动态向量索引**:HNSW 只承担不可变基础代际，新版本进入精确增量层，删除由版本表立即过滤，后台锁外重建并原子切换。十万条向量经历一万次更新和一万次删除后，压缩回收两万条旧向量，P95 从 **1.34 ms** 降到 **0.98 ms**，重启结果一致。研究选型与边界见 [`docs/INDEX_LIFECYCLE_RESEARCH.md`](./docs/INDEX_LIFECYCLE_RESEARCH.md)。

## 上下文压缩

`core/context/compiler.py`:结构化八段(Current State / Task Spec / Assets & Entities / Workflow / Errors & Corrections / Evidence / Learnings / Key Results),每段独立预算,溢出只在段内裁、不饿死其它段,**必需证据永不被裁**,输出带压缩比和完整 provenance。全程确定性、无 LLM 调用。

消融显示压缩对根因准确率是 Δ0——它的价值是每 token 保留的信息量与鲁棒性,不是提准。这一点在 [docs/BENCHMARKS.md](./docs/BENCHMARKS.md) 里如实标注。

## 编排与验证

- 级联意图路由:规则快路径吃高频确定请求,语义检索召回候选技能,复合/歧义请求升级 Agent,未命中触发技能库自扩展(带回放回归门,不破坏既有技能才入库)。
- 技能注意力调度:相关性做硬门,学到的成功/误用率只在相关集内排序。消融证明它是承重组件——关掉后 6 例真实留出集准确率 100%→16.7%。
- 验证:契约验证(前置/后置/不变量 + 终端回读 + 步进评判)+ 引用核验(引用的证据必须真被观测到)。
- 自适应升级(单 Agent → planner-executor-critic,按证据歧义与影响面门控)已实现,但默认不接入出货的单 Agent 路径。

## 评测与可复现

评测是 LLM-free、确定性、可复现的,这也是仓库可信度的来源。观测层做能力门控:内核证明不了自己产出的值(如未接线的衰减、检索分数)一律置灰,不假装在跑。

真实 R230 FortiGate 留出集(6 例 × 4 pass,规则推理器)上的头条数字:

- 复现事件可命中溯源记忆,但历史 `evidence_snapshot` 只作假设来源;每次诊断仍执行当前只读取证,当前证据通过 verifier 后才标记记忆确认。真实留出流修正后为 **32→32 次取证(Δ0%)**,根因准确率与引用核验均保持 **100%**;原 −75% 数字已作废,因为它来自跳过当前取证。
- 消融:关掉技能调度,准确率 **100%→16.7%**。

注意口径:N=6 + 确定性规则推理器,这里的 100% 是六类真实事件被正确分类(接线正确 + 权限门控证据路由),不是学到的泛化。复现:

```bash
python3 examples/benchmarks.py        # §1–§3,真实 R230 集
python3 -m pytest tests_py/ -q        # 全量测试
```

## 前端与可观测

[`frontend/`](./frontend) 是 React/Vite 的战术态势界面(态势 / 长轨迹 / 渗透 三页)+ 能力门控的记忆 observatory + FastAPI 网关(`frontend/gateway/`)。它把 agent 正在诊断的真实 R230 网络可视化——证据面、拓扑、记忆生命周期、只读自渗透——每条线、每个计数都读自真实保留集抓包,不是示意图。默认服务在 `http://<host>:2026`。

observatory 与内核共用同一条诚实原则:内核证明不了自己产出的值一律置灰,不在界面上假装在跑。`frontend/script/vreview.mjs` 用 Playwright 驱动真实浏览器做可测量的前端验证(实际裁切、axe 对比度、横向滚动、console 错误、像素 diff),而不是靠眯眼看截图。

## 数据

- 真实:FortiGate R230 syslog、FortiOS 7.4 管理手册语料、IODA v2 断网事件池、LongMemEval-500。
- 真实告警/日志因含内外网 IP 走 gitignore、本地留存;仓库内带脱敏 seed 与合成 fixture,克隆后基准会自动回退到 seed 并明确说明用了哪个集。

## 目录

```text
core/memory/        三层记忆、BM25 检索核心、hybrid_kb 混合检索器、拓扑图记忆
core/evolve/        写入路由、A-MEM、反思、冲突消解 supersede、效用驱逐、自演化流、observatory
core/context/       结构化预算上下文压缩
core/orchestrator/  级联意图路由、自适应编排、技能调度
core/skills/        技能注册表、技能诱导、契约
core/verifier/      契约验证、引用核验
core/eval/          LLM-free 评测、检索基准、混合检索根因诊断
core/llm/           OpenAI 兼容 provider(DeepSeek API / 本地 GPU 隧道)
domains/network_rca 首个落地域:内网 RCA
domains/active_recon 只读侦察 / 加固报告
domains/enterprise_ops 企业运维/定价工作流(合成 fixture)
frontend/           React/Vite 战术态势界面 + 记忆 observatory + FastAPI 网关
```

## 推理后端

生产推理走 DeepSeek API(`core/llm/provider.py`,`DS_V4_*` 环境变量),LLM-free 评测走内置规则推理器。鹏城 GPU 隧道配置见 [docs/PENGCHENG_PROVIDER.md](./docs/PENGCHENG_PROVIDER.md)。所有配置读 `AUTOPOIESIS_*` 变量,旧 `SELFEVO_*` 作为兼容回退保留。

## 边界与 roadmap

- 系统不允许 Agent 自由改写生产行为。候选改进须过验证器与回放门才生效。
- GRPO 组相对策略优化在 `core/evolve/` 里有确定性规则版实现,但不是当前的性能驱动项——在线路径用规则推理器 / DeepSeek API,GPU 侧梯度训练仍属 roadmap。

## 研究参考

CoALA(arXiv:2309.02427)· Mem0(2504.19413)· A-MEM(2502.12110)· Generative Agents(2304.03442)· StreamBench(2406.08747)· LongMemEval(2410.10813)· FreshDiskANN· SPFresh· Quake。记忆研究引用见 [docs/BENCHMARKS.md](./docs/BENCHMARKS.md)，动态索引研究见 [docs/INDEX_LIFECYCLE_RESEARCH.md](./docs/INDEX_LIFECYCLE_RESEARCH.md)。
