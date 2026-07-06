# Résumé bullets — selfevo-orchiter (honest, code-backed)

Every number here is reproduced by `python3 examples/benchmarks.py` and
`python3 -m pytest tests_py/ -q` (40 tests). Full method + citations in
[docs/BENCHMARKS.md](./BENCHMARKS.md). Use these bullets verbatim; do not round up.

---

## English (impact-first)

**Self-Evolving Long-Horizon Agent Kernel** — business-decoupled agent runtime;
first domain: internal-network root-cause analysis on real Dahua FortiGate syslog.

- Built a **self-evolution loop** (online read-only diagnosis → offline trace
  consolidation) that, on a recurring real-incident stream, **cut read-only probes and
  tool cost 75% (32→8) at unchanged 100% root-cause accuracy and 100% citation
  verification** — measured cold-vs-warm, StreamBench-style; recurrences are resolved
  from provenance-linked episodic memory (0 fresh probes), never at the cost of accuracy.
- **Ablation-identified the load-bearing component**: removing attention-based skill
  scheduling collapses root-cause accuracy **100%→16.7%** on the real held-out set,
  while evidence compression and tiered memory are Δ0 on accuracy — reframed honestly as
  **efficiency/robustness levers** (fewer tokens/probes, graceful degradation), not
  accuracy lifts. Engine-independent (holds under a rule reasoner).
- Engineered a **managed 3-tier memory** (episodic/semantic/procedural, CoALA) with
  **Mem0-style ADD/UPDATE/NOOP write routing**, **A-MEM associative links**,
  **Generative-Agents importance-gated reflection**, and **Ebbinghaus decay/forgetting**
  — 19 curated, de-duplicated memories with 14 cross-links; all four mechanisms covered
  by property tests.
- Hardened the online path: **evidence-aware context compression to a token budget**,
  **hard read-only skill gating**, a **citation verifier** (every cited fact must have
  been observed), and a **replayable trace ledger** enabling per-component replay ablation.
- Conforms to **LongMemEval** (ICLR'25, the external long-term-memory benchmark) via an
  **LLM-free recall@k harness** — reproducible with one command, no API key.
- 40 automated tests; one-command benchmark reproducer; live FastAPI console visualizing
  the real cold-vs-warm evolution curve.

## 中文（可直接粘贴）

**自演化长周期 Agent 内核** —— 业务解耦运行时；首落地场景：真实 Dahua FortiGate syslog 的内网根因分析。

- 实现**自演化闭环**（在线只读诊断 → 离线轨迹固化）：在真实复现事件流上，**只读探针 /
  工具成本下降 75%（32→8），根因准确率与引用核验保持 100% 不变**（cold-vs-warm，
  StreamBench 式度量）；复现事件由溯源绑定的情景记忆直接召回（0 次新取证），效率提升从不牺牲准确率。
- **消融定位承重组件**：在真实留出集上移除注意力式技能调度，根因准确率**从 100% 坍塌到 16.7%**；
  而证据压缩与三层记忆对**最终准确率 Δ0** —— 诚实定位为**效率/鲁棒性杠杆**（更少 token/探针、
  压缩后不掉点），而非准确率来源；结论引擎无关（规则推理器下成立）。
- 构建**受管理的三层记忆**（情景/语义/程序，CoALA）：**Mem0 式 ADD/UPDATE/NOOP 写入路由**、
  **A-MEM 关联链**、**Generative-Agents 重要度门控反思**、**Ebbinghaus 衰减遗忘**——
  19 条去重记忆 + 14 条关联；四种机制均有属性测试覆盖。
- 加固在线路径：**证据感知的预算内上下文压缩**、**只读技能硬门控**、**引用核验器**
  （每条被引用事实必须被观测到）、**可回放轨迹账本**（支撑逐组件回放消融）。
- 对齐外部权威记忆基准 **LongMemEval**（ICLR'25）：提供**无需 LLM 的 recall@k 评测脚手架**，
  一条命令可复现、无需密钥。
- 40 项自动化测试；一键基准复现脚本；FastAPI 控制台实时可视化真实 cold-vs-warm 演化曲线。

---

## What is real vs. roadmap (do not overclaim)

| claim | status |
|---|---|
| −75% probes @ 100% accuracy (cold-vs-warm) | ✅ real, reproducible |
| ablation 100%→16.7% (skill scheduling load-bearing) | ✅ real (held-out set) |
| Mem0 router / A-MEM links / reflection / Ebbinghaus decay | ✅ coded + tested |
| citation verifier, read-only gating, replay ablation | ✅ coded + tested |
| LongMemEval conformance harness | ✅ runs; real numbers need the public dataset |
| "three-tier memory *raises accuracy*" | ❌ do NOT say — it's Δ0 on accuracy here |
| GRPO policy optimization | ⚠️ roadmap — group-reward export only, not trained |
| WorkArena / cross-domain transfer numbers | ⚠️ roadmap — not yet measured |

## Citations

CoALA (Sumers+ 2023, 2309.02427) · Mem0 (Chhikara+ 2025, 2504.19413) ·
A-MEM (Xu+ 2025, 2502.12110) · Generative Agents (Park+ 2023, 2304.03442) ·
Voyager (Wang+ 2023, 2305.16291) · StreamBench (Wu+ 2024, 2406.08747) ·
LongMemEval (Wu+ ICLR'25, 2410.10813) · GRPO/DeepSeekMath (Shao+ 2024, 2402.03300) ·
Ebbinghaus 1885.
