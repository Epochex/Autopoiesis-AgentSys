# Benchmarks & Reproducibility

The single design rule of this project is **honesty**: every number below is produced
by the Python code in `core/` + `domains/`, on the real Dahua FortiGate → R230 syslog
held-out set, and is reproducible with the commands given. Nothing here is a mock, a
projection, or a hand-tuned figure. Where a result is illustrative or synthetic it is
labelled as such in the same sentence.

> Scope note. The **headline evidence is the real R230 held-out stream** (below).
> **LongMemEval is the external conformance anchor** — a harness you run yourself on
> the public dataset. The legacy TypeScript `src/` tree and its `npm` commands are an
> earlier prototype and are *not* the system measured here.

---

## 1. Self-evolution — the agent gets cheaper on a recurring stream, for free

One agent processes a stream of real incidents over several passes (StreamBench-style
online protocol). The first encounter is investigated normally; every recurrence is
resolved from provenance-linked episodic memory (0 fresh probes). The verifier still
re-checks every citation, so efficiency is never bought with correctness.

| metric | cold (no learning) | warm (self-evolving) | Δ |
|---|---|---|---|
| read-only probes / tool calls | 32 | **8** | **−75.0%** |
| tool cost | 32.0 | **8.0** | **−75.0%** |
| root-cause accuracy | 1.00 | **1.00** | 0 |
| citation-verify pass rate | 1.00 | **1.00** | 0 |
| persistent memories | 0 | **19** | +19 |

*Real R230 FortiGate held-out set, 6 cases × 4 passes, rule reasoner, engine-independent.*

The `−75%` is real *transfer over time*: passes 2–4 recall all 6 incidents from memory
and probe zero times, at unchanged accuracy. This is the "越用越省" claim, measured.

Reproduce (one command — prefers the real held-out, falls back to seed cases and says so):
```bash
python3 examples/benchmarks.py          # prints §1, §2 and §3 on the real R230 set
# also served live at:  GET /api/rca/evolution?passes=4
```

---

## 2. Ablation — which component is load-bearing

Turn each component off and re-measure root-cause accuracy on the held-out set:

| configuration | memory | compress | skill-ctl | root-cause acc |
|---|:--:|:--:|:--:|---|
| SELFEVO full path | on | on | on | **100%** |
| − evidence compression | on | off | on | 100% |
| − tiered memory | off | on | on | 100% |
| **− skill scheduling** | on | on | **off** | **16.7%** |

*6-case real held-out, rule reasoner.* Removing **skill scheduling** collapses accuracy
100% → 16.7% (1/6) — it is the load-bearing lever. Compression and memory are **Δ0 on
accuracy** on this set: their honest value is *efficiency and robustness* (fewer
tokens/probes, graceful degradation), **not** an accuracy lift. The resume must say so.

> Caveat (honesty): this collapse is a property of the **real** held-out set. On the mock
> seed cases every configuration scores 100% — skill scheduling only becomes load-bearing
> once the agent must pick the right probe among real distractors. `examples/benchmarks.py`
> prints which dataset produced the table.

Reproduce: `python3 examples/benchmarks.py` (§2 of its output).

---

## 3. Memory layer — managed, not just appended (Phase B)

Between events the persistent core is *curated* by four rule-based mechanisms, each
drawn from the recent agent-memory literature and each derived from real run signals:

| mechanism | paper | what it does | live number |
|---|---|---|---|
| write router (ADD/UPDATE/NOOP) | Mem0 (Chhikara et al., 2025) | reinforce a variant instead of duplicating | 19 active, deduped |
| associative links | A-MEM (Xu et al., 2025) | connect same-family memories for transfer | 14 links |
| reflection | Generative Agents (Park et al., 2023) | abstract a salient family into a higher-level insight | 1 insight |
| decay + forgetting | Ebbinghaus (1885) | unused memories fade; reused ones stay warm | 0 forgotten (all recur) |

19 memories = 6 episodic / 7 semantic / 6 procedural. `forgotten=0` is honest: on a
fully-recurring stream every memory is reused each pass, so nothing goes stale — the
decay mechanism is proven in isolation by `tests_py/test_memory_ops.py`.

Reproduce:
```bash
python3 -m pytest tests_py/test_memory_ops.py -q     # 9 mechanism property tests
```

---

## 4. LongMemEval — external conformance anchor

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) (Wu et al., ICLR 2025 —
arXiv:2410.10813) is the authoritative long-term-memory benchmark: 500 questions over
long, distractor-saturated multi-session histories, spanning five abilities
(information extraction, multi-session reasoning, temporal reasoning, knowledge update,
abstention).

`core/eval/longmemeval.py` maps a LongMemEval item onto our `TieredMemoryStore` and
measures the metric this project is responsible for — **does tiered retrieval surface
the answer-bearing session out of all the distractors** (recall@k) — with **no LLM in
the loop**, so it is reproducible anywhere.

```bash
# real numbers — download longmemeval_s.json first (link above):
python3 -m core.eval.longmemeval /path/to/longmemeval_s.json 5

# synthetic smoke test (SHIPS in-repo; NOT a LongMemEval result):
python3 -m core.eval.longmemeval tests_py/fixtures/longmemeval_synthetic.json 3
```

The in-repo run reports `recall@3 = 0.75` on the **synthetic** fixture and prints a
`NOT a LongMemEval result` banner. Real-dataset numbers are intentionally left for the
reader to generate — we do not ship a LongMemEval score we did not run.

---

## Full test suite

```bash
python3 -m pytest tests_py/ -q      # 40 tests: kernel + self-evolution + memory-ops + LongMemEval
```

## References

- CoALA — Sumers et al., 2023 (arXiv:2309.02427) — tiered memory taxonomy
- Mem0 — Chhikara et al., 2025 (arXiv:2504.19413) — memory write routing
- A-MEM — Xu et al., 2025 (arXiv:2502.12110) — agentic associative memory
- Generative Agents — Park et al., 2023 (arXiv:2304.03442) — reflection
- Voyager — Wang et al., 2023 (arXiv:2305.16291) — procedural skill library
- Agent Workflow Memory — Wang et al., 2024 (arXiv:2409.07429) — reusable workflows
- StreamBench — Wu et al., 2024 (arXiv:2406.08747) — online continual improvement
- LongMemEval — Wu et al., ICLR 2025 (arXiv:2410.10813) — long-term memory eval
- GRPO / DeepSeekMath — Shao et al., 2024 (arXiv:2402.03300) — group-relative policy opt (roadmap)
- Ebbinghaus, 1885 — *Über das Gedächtnis* — the forgetting curve
