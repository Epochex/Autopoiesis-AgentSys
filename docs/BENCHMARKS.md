# Benchmarks & Reproducibility

The single design rule of this project is **honesty**: every number below is produced
by the Python code in `core/` + `domains/`, on the real Dahua FortiGate → R230 syslog
held-out set, and is reproducible with the commands given. Nothing here is a mock, a
projection, or a hand-tuned figure. Where a result is illustrative or synthetic it is
labelled as such in the same sentence.

> Scope note. The **headline evidence is the real R230 held-out stream** (below).
> **LongMemEval is the external conformance anchor** — a harness you run yourself on
> the public dataset. (An earlier TypeScript `src/` prototype and its `npm` workflow have
> been **removed**; the measured system is Python-only.)

---

## 1. Self-evolution — the agent gets cheaper on a recurring stream, for free

One agent processes a stream of real incidents over several passes (StreamBench-style
online protocol). The first encounter is investigated normally; every recurrence is
matched against provenance-linked episodic memory, then re-probed against current
state. Historical snapshots are never admitted as current evidence.

| metric | cold (no learning) | warm (self-evolving) | Δ |
|---|---|---|---|
| read-only probes / tool calls | 32 | **32** | **0.0%** |
| tool cost | 32.0 | **32.0** | **0.0%** |
| root-cause accuracy | 1.00 | **1.00** | 0 |
| citation-verify pass rate | 1.00 | **1.00** | 0 |
| learned in-process memories | 0 | **19** | +19 |

*Real R230 FortiGate held-out set, 6 cases × 4 passes, rule reasoner, engine-independent.*

Passes 2–4 confirm all six recalled incidents using fresh current-run evidence. The
earlier `−75%` result came from replaying each historical `evidence_snapshot` and skipping
current probes; it has been removed because that verifies an old incident, not current state.
On this held-out corpus the cold router already selects the minimal required checks, so safe
memory confirmation does not reduce tool cost. Efficiency requires a separate measured case
where procedural memory safely removes unnecessary cold-path checks.

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
| Autopoiesis full path | on | on | on | **100%** |
| − evidence compression | on | off | on | 100% |
| − tiered memory | off | on | on | 100% |
| **− skill scheduling** | on | on | **off** | **16.7%** |

*6-case real held-out, rule reasoner.* Removing **skill scheduling** collapses accuracy
100% → 16.7% (1/6) — it is the load-bearing lever. Compression and memory are **Δ0 on
accuracy** on this set: their honest value is *efficiency and robustness* (fewer
tokens/probes, graceful degradation), **not** an accuracy lift. The resume must say so.

> **Read the 100% honestly.** With N=6 and a deterministic rule reasoner, the 100% is the
> pipeline correctly classifying six curated real-log incident types — evidence of correct
> wiring and permission-gated evidence routing, **not** learned accuracy or generalization.
> The collapse *mechanism* (a dominant signal swamping minority cases without gating) is
> real and independently reproduced on synthetic stats; the exact *magnitude* (1/6) is a
> small-N + first-match-reasoner property.

> Caveat (honesty): this collapse is a property of the **real** held-out set. On the mock
> seed cases every configuration scores 100% — skill scheduling only becomes load-bearing
> once the agent must pick the right probe among real distractors. `examples/benchmarks.py`
> prints which dataset produced the table.

Reproduce: `python3 examples/benchmarks.py` (§2 of its output).

---

## 3. Memory layer — managed, not just appended (Phase B)

Between events the warm in-process store is *curated* by four rule-based mechanisms, each
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

## 5. Retrieval baselines — dense embeddings & cross-encoder reranking (eval-only, optional)

These rows live behind the optional `dense` / `rerank` extras (sentence-transformers +
faiss + torch). They are **never imported by the core or the online RCA path**; they exist
only to state honest, head-to-head retrieval numbers against real embedding and reranking
models. Run them in a dedicated venv:

```bash
python3 -m venv .venv-dense && . .venv-dense/bin/activate
pip install -e '.[dense,rerank]'
python3 -m core.eval.dense_retrieval ioda      # §5.1 dense vs sparse
python3 -m core.eval.reranker all              # §5.2 first-stage vs +reranker
```

### 5.1 Dense vs sparse on the real IODA v2 pool — the FAIR comparison

> **Correction of a label artifact.** An earlier framing headlined a huge
> "structured ≫ dense" gap. That gap is **circular and is not reported as a retrieval
> result**: the IODA relevance labels (`candidate_event_id`) were *defined* by a per-event
> **entity + time-window** pull, and the `structured` retriever scores documents by that
> **same entity+time key** — it reconstructs the label-defining key (and the time window is
> deliberately withheld from every other method). It is an **upper bound on what the join
> key can recover, not a fair retrieval baseline.**

The **fair, text-only comparison** (every method sees only operator-observable
text/entities; **no time window**), macro-averaged **recall@10 / nDCG@10** over 832 events
on the 8542-doc pool (`BAAI/bge-small-en-v1.5`):

| method (fair, text-only) | recall@10 | nDCG@10 |
|---|--:|--:|
| dense-binary (sign-bit) | 0.071 | 0.076 |
| dense-hnsw | 0.128 | 0.141 |
| **dense-flat** (exact cosine) | **0.174** | 0.189 |
| structured_no_time (typed entity, no time) | 0.216 | 0.221 |
| naive (bag-of-words) | 0.219 | 0.242 |
| rrf-fair (bm25 + structured_no_time + dense) | 0.254 | 0.266 |
| **bm25** (Okapi) | **0.264** | 0.289 |

**Honest finding: dense does NOT beat BM25 here — it is the *worst* of the text retrievers.**
The evidence documents are terse identifier strings (entity id + source + signal type),
so lexical overlap (BM25) carries more signal than a general-purpose sentence embedding,
and sign-bit binary quantization (32× smaller vectors, verified) degrades it further. This
is a real property of identifier-like corpora, reported as measured.

For completeness the **label-key upper bound (diagnostic, not fair)** is
`structured` recall@10 **0.752** / nDCG@10 **0.847**, `rrf` 0.615, `rrf+dense` 0.479 — these
use the time window that *defines* the labels and must never be quoted as a dense/BM25 win.
`core/eval/dense_retrieval.py` prints the fair table first and the upper bound under an
explicit "NOT a fair baseline" banner.

### 5.2 Cross-encoder reranking — where it helps, and where it does not

A two-stage eval: a BM25 first stage fetches a top-k pool, then a cross-encoder
(`cross-encoder/ms-marco-MiniLM-L-6-v2`, ~80 MB CPU) rescores every (query, document) pair
and re-orders it (`core/eval/reranker.py`). Measured first-stage vs +reranker on three
settings — **including the two where reranking does not help, reported honestly:**

| setting | k | first-stage recall / nDCG | +reranker recall / nDCG | Δ recall |
|---|--:|--:|--:|--:|
| (a) IODA fair (832 q, 8542 docs) | @10 | 0.264 / 0.289 | 0.240 / 0.261 | **−9.3%** |
| (b) FortiGate skill routing (6 q, 9 skills) | @3 | 0.833 / 0.833 | 0.833 / 0.833 | **±0.0%** |
| (c) **BEIR SciFact** (300 q, 5183 docs, real qrels) | @10 | 0.779 / 0.663 | **0.793 / 0.679** | **+1.8%** |
| (c) **BEIR SciFact** — top rank | @1 | 0.529 / 0.547 | **0.549 / 0.573** | **+3.8%** (nDCG@1 **+4.9%**) |

- **(a) IODA — reranking *hurts* (−9.3% recall@10).** The evidence docs carry almost no
  free-text signal, so the language model's relevance judgment is noise that displaces
  BM25's lexical hits. Predicted up front, confirmed as measured.
- **(b) FortiGate — no change.** A 9-skill catalog with a first-stage ceiling
  (pool recall@9 = 0.75): reranking can only reorder what BM25 already surfaced, and the
  top-3 set is unchanged. Honest null result.
- **(c) BEIR SciFact — reranking *helps* (+1.8% recall@10, and +4.9% nDCG@1).** This is the
  **externally-valid, non-circular number**: public benchmark, human relevance judgments,
  no dependency on this repo's data. The lift is real but modest and concentrated at the
  very top of the ranking (nDCG@1 +4.9%) — exactly what a reranker is for.

**Takeaway (the honest one):** a cross-encoder reranker is worth its cost only when the
documents carry genuine natural-language semantics *and* the first stage leaves headroom
(SciFact); on terse identifier corpora it can actively hurt (IODA), and on a tiny catalog
bounded by first-stage recall it does nothing (FortiGate). We report all three.

Reproduce (downloads BEIR SciFact from the public UKP mirror with the stdlib; if it is
unreachable the driver skips (c) and reports (a)(b) only):
```bash
python3 -m core.eval.reranker all
```

### 5.3 FortiOS operations-KB RAG — the first REAL ops-doc corpus (eval-only, optional)

This is the honest, application-grounded version of the resume's "运维知识库 RAG (runbook /
工单 / 设备手册)" line. Unlike §5.1 (terse IODA identifier strings) and §5.2's 9-skill
catalog, the corpus here is a **real vendor operations manual**.

- **Corpus (real, not synthesized).** The public **FortiOS 7.4.0 Administration Guide** from
  `docs.fortinet.com` — **all 1,145 sections** downloaded as HTML and converted to text with
  the stdlib parser (`<div id="mc-main-content">` → headings/paragraphs/list-items/table
  cells). **Structure-aware chunking** (split at h1/h2 boundaries, ~1.1 k-char windows) yields
  **9,014 chunks**. Nothing is fabricated; the build is reproducible
  (`python -m core.eval.fortios_corpus build`).
- **Contextual Retrieval, deterministic (no LLM).** Each chunk is prefixed with a document
  context header built purely from its **section-title hierarchy** taken from the guide's own
  table-of-contents tree, e.g. `FortiOS 7.4 Administration Guide > User & Authentication >
  LDAP servers > Configuring an LDAP server`. This is the zero-cost, reproducible variant of
  Anthropic's Contextual Retrieval — we lift the publisher's ground-truth breadcrumb instead
  of asking an LLM to write per-chunk context.
- **Labels — NON-CIRCULAR, and this time it matters.** Queries are the **six real R230
  FortiGate incidents** (held-out set). A manual section is labelled *relevant* iff its prose
  **explains the mechanism of that incident's root cause**, judged by reading the section —
  never by any retriever score. The map + a written per-section rationale are frozen in
  `core/eval/fortios_labels.json`. Every retriever scores on query↔chunk **text** (BM25 term
  overlap / bge cosine / cross-encoder pair score); the labels use the section↔root-cause
  **meaning**. There is **no shared key** (no entity+time join, no title match) a retriever
  could reconstruct — exactly the circularity that invalidated the earlier IODA +334%. (Some
  relevant sections, e.g. *Configuring the maximum log in attempts and lockout period* and the
  *DHCP monitor*, barely share surface terms with the incident wording, which is why BM25
  alone misses them.)

Four-stage pipeline, each stage adding **one** component; metrics are **section-level**
(passage→document collapse), macro-averaged over the **N = 6** incidents:

| stage | recall@1 | recall@5 | recall@10 | nDCG@10 |
|---|--:|--:|--:|--:|
| BM25 (raw chunks) | 0.000 | 0.250 | 0.333 | 0.179 |
| + Contextual-Retrieval header | 0.000 | 0.250 | 0.333 | 0.182 |
| **+ dense/hybrid** (BM25-CR ⊕ bge, RRF) | **0.083** | 0.167 | **0.417** | **0.267** |
| + cross-encoder rerank | 0.000 | 0.250 | 0.417 | 0.192 |

**Honest verdict — where the pipeline helps, and where it does NOT:**

- **Hybrid BM25+dense is the only lever that helps.** It lifts **recall@10 0.333 → 0.417
  (+25% relative)** and nDCG@10 0.179 → 0.267, and is the only stage to land a relevant
  section at rank 1. Dense embeddings recover semantically-worded incidents that lexical
  overlap misses (e.g. the DHCP-allocation and security-posture cases), though the fusion also
  reorders mid-ranks (recall@5 dips) — a real, mixed effect, not a clean win.
- **Deterministic Contextual Retrieval gives ~zero lift here** (recall@10 unchanged; nDCG@10
  +0.003). The ToC-hierarchy header adds correct parent terms but does not change which
  sections surface for these six queries. Honest null result — reported, not hidden. (It
  remains cheap insurance and is expected to matter more on deeper, more ambiguous chunks.)
- **Cross-encoder reranking does NOT help recall@10 (flat 0.417) and *hurts* ranking quality**
  (nDCG@10 0.267 → 0.192): it demotes the good top-1 that hybrid found. On this corpus the
  reranker is not worth its cost.
- **Statistical power is low: N = 6.** Each incident is ≈0.17 of recall@10, so the +25% is
  "roughly one more incident's section reaching the top-10." Treat the magnitudes as
  **directional**, not precise. The value of this eval is that it is *real and non-circular*,
  not that it is high-powered — §5.2's BEIR SciFact (300 queries, human qrels) remains the
  higher-power external anchor for the rerank stage.

Reproduce (needs the `dense`+`rerank` venv; the corpus HTML/embeddings cache under
`.dense_cache/`, gitignored):
```bash
python3 -m core.eval.fortios_corpus build   # (re)build the corpus from docs.fortinet.com
python3 -m core.eval.fortios_corpus eval    # four-stage recall@k / nDCG@k table
```

---

## 6. Flat versus HNSW at 100k and 1m vectors

`core/eval/vector_index_benchmark.py` isolates index-engine behaviour from embedding-model
quality. It generates deterministic normalized 128-dimensional vectors, takes exact
`IndexFlatIP` top-10 as the oracle, and measures the production HNSW configuration
(`M=32`, `efConstruction=200`) across six `efSearch` values.

| vectors | Flat build | HNSW build | Flat index | HNSW index | HNSW Recall@10 / P95 |
|---:|---:|---:|---:|---:|---:|
| 100,000 | 0.03 s | 30.99 s | 51.20 MB | 78.42 MB | 0.899 / 0.66 ms (`ef=128`) |
| 1,000,000 | 0.31 s | 909.70 s | 512.00 MB | 784.13 MB | 0.846 / 21.37 ms (`ef=1024`) |

The million-vector result exposes the real trade-off: the previous default `efSearch=128`
has P95 2.33 ms but only 0.532 Recall@10; raising it to 1024 reaches 0.846 Recall@10 while
remaining faster than Flat's 36.42 ms P95. The 784 MB serialized HNSW index reloads in
0.57 s, so a serving system should build offline, persist, and atomically swap generations
instead of rebuilding in a request path.

These are synthetic-vector index measurements, not natural-language relevance or production
traffic numbers. Full six-point curves, hardware, memory, methodology, commands, and JSON
artifacts are in [HNSW_SCALE_BENCHMARK.md](./HNSW_SCALE_BENCHMARK.md).

---

## 7. Dynamic index churn and compaction

The lifecycle benchmark begins with 100,000 records, updates 10,000 ids, deletes another
10,000 ids, then measures query behaviour before and after physical compaction.

| path | before compaction | after compaction | reclaimed | restart |
|---|---:|---:|---:|---:|
| segmented BM25 | 120,000 physical / 90,000 live | 90,000 / 90,000 | 25.0% | exact Top-10 preserved |
| HNSW base + Flat delta | 110,000 physical / 90,000 live | 90,000 / 90,000 | 20,000 vectors | result rows preserved |

The previous memory-store path rebuilt BM25 inside every request. On the churned 90,000-live
corpus its P95 was 908.26 ms across five samples; the persistent segmented index measured
11.57 ms P95 across 100 queries, a 78.49x speedup. Compaction took 0.59 s and every pre/post/
restart Top-10 matched a freshly constructed monolithic BM25.

For 128-dimensional vectors, compaction reduced P95 from 1.34 ms to 0.98 ms and raised batch
throughput from 751.30 to 1252.19 QPS. Recall@10 against exact Flat was 0.898 before and 0.912
after the HNSW rebuild; build topology can change approximate results, so generation release
must gate on a fixed recall target rather than byte reclamation alone. Full methodology,
primary references, and limitations are in
[INDEX_LIFECYCLE_RESEARCH.md](./INDEX_LIFECYCLE_RESEARCH.md).

---

## Full test suite

```bash
python3 -m pytest tests_py/ -q      # default: 316 passed, 13 skipped
# with vector-bench extra: 330 passed, 7 skipped
# opt-in 100k/1m performance regression: 2 passed
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
