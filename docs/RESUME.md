# Résumé bullets: Autopoiesis-AgentSys

Every number here is reproduced by `python3 examples/benchmarks.py` and
`python3 -m pytest tests_py/ -q` (368 passed, 8 skipped; 14 PostgreSQL 17
integration tests and 2 scale regressions are opt-in). Full method + citations in
[docs/BENCHMARKS.md](./BENCHMARKS.md). Use these bullets verbatim; do not round up and do
not drop the measurement scope.

---

## English (impact-first)

**Self-Evolving Long-Horizon Agent Kernel**, a business-decoupled agent runtime;
first domain: internal-network root-cause analysis over operational evidence.

- Built a **freshness-gated self-evolution loop** spanning online diagnosis and offline trace
  consolidation: episodic recall proposes a root-cause candidate, current-run probes collect
  evidence, and memory confirmation requires both citation verification and agreement with
  the remembered root. The six-case held-out stream completed 32 current-state probes with
  100% root-cause accuracy and citation verification.
- **Ablation-identified the load-bearing component**: on the real held-out set, removing
  attention-based skill scheduling lets a dominant high-volume signal swamp the minority
  cases and root-cause accuracy falls from **100% to 16.7%**. The experiment uses six
  curated real-log incident types and a deterministic rule reasoner; the same relevance
  gate is independently reproduced on synthetic statistics.
- Engineered a **managed 3-tier memory** with ADD/UPDATE/NOOP write routing,
  incremental segmented BM25, an online HNSW-base/Flat-delta semantic route,
  bounded two-hop relation expansion, typed event histories, explicit reuse credit,
  utility eviction, and atomic index-generation compaction.
- Hardened the online path: **CJK-aware context compilation into both rule and LLM reasoning**,
  a **3-layer hard read-only skill gate**, a **citation verifier** (every cited fact must
  satisfy its root-cause evidence contract), a **contract verifier** (pre/post/invariant + grounded read-back),
  and a **replayable typed trace ledger** enabling per-component replay ablation.
- Added breadth on the *same* kernel: **trace-driven skill-candidate capture** with a replay
  gate, an **opt-in read-only escalation** path (planner/executor/critic + intent
  router), and two more domains — **self-pentest** (real recon pipeline, approval-gated
  intrusive probes, mock documentation-net target) and **contract-checked anomaly
  detection** (synthetic simulation).
- Conforms to **LongMemEval** (ICLR'25, the external long-term-memory benchmark) via an
  **LLM-free recall@k harness**, reproducible with one command and no API key.
- 368 passing automated tests, 14 opt-in PostgreSQL 17 integration tests, plus 2 opt-in scale regressions; one-command benchmark reproducer; live FastAPI console visualizing
  the real cold-vs-warm evolution curve, ablation, and per-case diagnosis traces.

## 中文（可直接粘贴）

**自演化长周期智能体内核**，业务解耦运行时；首落地场景为真实网络日志的内网根因分析。

- 实现**鲜证据门控的自演化闭环**，贯通在线诊断与离线轨迹固化。情景记忆生成根因候选，
  当前任务重新取证，引用核验与历史根因一致性共同决定记忆确认；6 案例留出流完成 32 次
  当前状态探测，根因准确率与引用核验率均为 100%。
- **消融定位承重组件**：在真实留出集上移除注意力式技能调度，主导的高频信号淹没少数派案例，
  根因准确率**从 100% 降至 16.7%**。实验使用 6 类精选真实日志故障和确定性规则推理器，
  同一相关性门控机制已在合成统计数据上独立复现。
- 构建**受管理的三层记忆**（情景、语义、程序），包含 ADD/UPDATE/NOOP 写入路由、
  增量分段 BM25、在线 HNSW 基础层与 Flat 增量层、两跳关系展开、类型化事件历史、
  显式复用归因、效用驱逐及索引代际原子压缩。
- 加固在线路径：**中文感知且同时接入规则与大模型推理的结构化上下文**、**三层只读技能硬门控**、**根因证据契约核验器**、
  **契约核验器**（前置、后置、不变量与落地回读）、**可回放类型化轨迹账本**（支撑逐组件回放消融）。
- 同一内核上做广度：**轨迹驱动的技能归纳**（回放晋升门）、**可选只读升级**路径（规划、执行、评判与意图路由），
  以及两个新域：**自渗透**（真实侦察管线、侵入探针审批门控、文档网段模拟靶）与
  **契约式异常检测**（合成仿真）。
- 对齐外部权威记忆基准 **LongMemEval**（ICLR'25）：提供**无需 LLM 的 recall@k 评测脚手架**，
  一条命令可复现、无需密钥。
- 368 项自动化测试、14 项可选 PostgreSQL 17 集成测试与 2 项十万、百万级性能回归；一键基准复现脚本；FastAPI 控制台实时可视化真实冷热态演化曲线、消融与逐案诊断轨迹。

## Citations

CoALA (Sumers+ 2023, 2309.02427) · Mem0 (Chhikara+ 2025, 2504.19413) ·
A-MEM (Xu+ 2025, 2502.12110) · Generative Agents (Park+ 2023, 2304.03442) ·
Voyager (Wang+ 2023, 2305.16291) · StreamBench (Wu+ 2024, 2406.08747) ·
LongMemEval (Wu+ ICLR'25, 2410.10813) · GRPO/DeepSeekMath (Shao+ 2024, 2402.03300) ·
Ebbinghaus 1885.
