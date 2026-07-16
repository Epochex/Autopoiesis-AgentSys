# Résumé bullets — Autopoiesis-AgentSys (honest, code-backed)

Every number here is reproduced by `python3 examples/benchmarks.py` and
`python3 -m pytest tests_py/ -q` (125 tests). Full method + citations in
[docs/BENCHMARKS.md](./BENCHMARKS.md). Use these bullets verbatim; do not round up and do
not drop the caveats — the honesty is the point.

---

## English (impact-first)

**Self-Evolving Long-Horizon Agent Kernel** — business-decoupled agent runtime;
first domain: internal-network root-cause analysis on real Dahua FortiGate syslog.

- Built a **self-evolution loop** (online read-only diagnosis → offline trace
  consolidation) that, on a **recurring** real-incident stream, **cut read-only probes and
  tool cost 75% (32→8) with root-cause accuracy and citation verification held flat** —
  measured cold-vs-warm (StreamBench-style) on a 6-case real FortiGate held-out set with a
  rule reasoner. Recurrences are resolved from provenance-linked episodic memory (0 fresh
  probes); the recalled evidence is **re-run through the reasoner and citation verifier**,
  so caching can never buy efficiency at the cost of correctness. (The 75% scales with how
  often incidents recur — 3 of 4 passes served from memory — not a fixed efficiency law.)
- **Ablation-identified the load-bearing component**: on the real held-out set, removing
  attention-based skill scheduling lets a dominant high-volume signal swamp the minority
  cases and root-cause accuracy drops **100%→16.7%**, while evidence compression and tiered
  memory are **Δ0 on accuracy** — reframed honestly as **efficiency/robustness levers**,
  not accuracy lifts. The gating mechanism is **independently reproduced on synthetic
  stats**; the exact magnitude is a small-N (6-case) + first-match-reasoner property, and
  the headline 100% is a **deterministic pipeline correctly classifying 6 curated real-log
  incident types** — evidence of correct end-to-end wiring and permission-gated evidence
  routing, **not** a claim of learned accuracy or generalization. Engine-independent (holds
  under a rule reasoner; an LLM path exists but is not the measured baseline).
- Engineered a **managed 3-tier memory** (episodic/semantic/procedural, CoALA) with
  **Mem0-style ADD/UPDATE/NOOP write routing**, **A-MEM associative links**,
  **Generative-Agents importance-gated reflection**, and **Ebbinghaus decay/forgetting**
  — 19 curated, de-duplicated memories with 14 cross-links; all four mechanisms covered
  by property tests.
- Hardened the online path: **evidence-aware context compression to a token budget**,
  a **3-layer hard read-only skill gate**, a **citation verifier** (every cited fact must
  have been observed), a **contract verifier** (pre/post/invariant + grounded read-back),
  and a **replayable 22-kind trace ledger** enabling per-component replay ablation.
- Added breadth on the *same* kernel: **trace-driven skill induction** with a replay-gated
  promotion, an **opt-in read-only escalation** path (planner/executor/critic + intent
  router), and two more domains — **self-pentest** (real recon pipeline, approval-gated
  intrusive probes, mock documentation-net target) and **contract-checked anomaly
  detection** (synthetic simulation).
- Conforms to **LongMemEval** (ICLR'25, the external long-term-memory benchmark) via an
  **LLM-free recall@k harness** — reproducible with one command, no API key.
- 125 automated tests; one-command benchmark reproducer; live FastAPI console visualizing
  the real cold-vs-warm evolution curve, ablation, and per-case diagnosis traces.

## 中文（可直接粘贴）

**自演化长周期 Agent 内核** —— 业务解耦运行时；首落地场景：真实 Dahua FortiGate syslog 的内网根因分析。

- 实现**自演化闭环**（在线只读诊断 → 离线轨迹固化）：在**复现**事件流上，**只读探针 /
  工具成本下降 75%（32→8），根因准确率与引用核验保持不变**（cold-vs-warm，StreamBench 式，
  6 案例真实 FortiGate 留出集，rule 推理器）；复现事件由溯源绑定的情景记忆召回（0 次新取证），
  召回的证据**仍重跑推理器 + 引用核验器**，因此缓存不可能以牺牲正确性换取效率。（75% 随复现频率变化——4 遍里 3 遍命中缓存——并非固定效率定律。）
- **消融定位承重组件**：在真实留出集上移除注意力式技能调度，主导的高频信号淹没少数派案例，
  根因准确率**从 100% 跌到 16.7%**；而证据压缩与三层记忆对**最终准确率 Δ0** —— 诚实定位为
  **效率/鲁棒性杠杆**而非准确率来源。门控机制已在**合成数据上独立复现**；但该幅度是小 N（6 案例）+
  首匹配推理器特性，且这里的 100% 是**确定性流水线在 6 个精选真实日志故障型上端到端分类正确**
  ——证明的是接线正确与权限门控式证据路由，**不是**学习到的准确率或泛化；结论引擎无关（规则推理器下成立，另有 LLM 路径但非被测基线）。
- 构建**受管理的三层记忆**（情景/语义/程序，CoALA）：**Mem0 式 ADD/UPDATE/NOOP 写入路由**、
  **A-MEM 关联链**、**Generative-Agents 重要度门控反思**、**Ebbinghaus 衰减遗忘**——
  19 条去重记忆 + 14 条关联；四种机制均有属性测试覆盖。
- 加固在线路径：**证据感知的预算内上下文压缩**、**三层只读技能硬门控**、**引用核验器**、
  **契约核验器**（pre/post/invariant + 落地回读）、**可回放 22 类轨迹账本**（支撑逐组件回放消融）。
- 同一内核上做广度：**轨迹驱动的技能归纳**（replay 晋升门）、**可选只读升级**路径（planner/executor/critic +
  intent router）、以及两个新域——**自渗透**（真侦察管线、侵入探针 approval 门控、mock 文档网段靶）与
  **契约式异常检测**（合成仿真）。
- 对齐外部权威记忆基准 **LongMemEval**（ICLR'25）：提供**无需 LLM 的 recall@k 评测脚手架**，
  一条命令可复现、无需密钥。
- 125 项自动化测试；一键基准复现脚本；FastAPI 控制台实时可视化真实 cold-vs-warm 演化曲线、消融与逐案诊断轨迹。

---

## What is real vs. roadmap (do not overclaim)

| claim | status |
|---|---|
| −75% probes with accuracy held flat (cold-vs-warm, recurring stream) | ✅ real, reproducible |
| ablation 100%→16.7% (skill scheduling load-bearing) | ✅ real — but N=6, rule reasoner; the 100% is pipeline self-consistency on curated cases, not generalization |
| Mem0 router / A-MEM links / reflection | ✅ coded + tested + wired into the loop |
| citation verifier, contract verifier, read-only gating, replay ablation | ✅ coded + tested |
| skill induction + replay-gated promotion | ✅ coded + tested |
| LongMemEval conformance harness | ✅ runs; real numbers need the public dataset |
| "three-tier memory *raises accuracy*" | ❌ do NOT say — it's Δ0 on accuracy here |
| Ebbinghaus decay/forgetting | ⚠️ implemented + unit-tested, **not wired into the online loop** (demo forgets 0) |
| multi-agent planner/executor/critic + adaptive escalation | ⚠️ implemented + tested, **opt-in & read-only, not on the shipped single-agent console** |
| GRPO policy optimization | ⚠️ roadmap — group-relative advantage + a confidence-update rule are implemented & unit-tested, but **not wired into the online loop**; no LLM/GPU training |
| active_recon self-pentest | ⚠️ real recon pipeline + approval-gated probes — **mock (RFC-5737 documentation-net) target**, not a real engagement |
| enterprise_ops anomaly detection | ⚠️ real contract logic — **synthetic simulation**, not real ERP data |
| WorkArena / cross-domain transfer numbers | ⚠️ roadmap — not yet measured |

## Citations

CoALA (Sumers+ 2023, 2309.02427) · Mem0 (Chhikara+ 2025, 2504.19413) ·
A-MEM (Xu+ 2025, 2502.12110) · Generative Agents (Park+ 2023, 2304.03442) ·
Voyager (Wang+ 2023, 2305.16291) · StreamBench (Wu+ 2024, 2406.08747) ·
LongMemEval (Wu+ ICLR'25, 2410.10813) · GRPO/DeepSeekMath (Shao+ 2024, 2402.03300) ·
Ebbinghaus 1885.
