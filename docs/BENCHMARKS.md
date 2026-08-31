# Benchmarks and evaluation boundaries

Every result in this document is a local measurement with a named analysis unit.  Test
counts, fixed-case pass rates, retrieval recall, index latency and controlled concurrency
measure different objects.  None of them is a proxy for an integrated investigation
service.

The release-level evaluation target is a temporal investigation case.  A qualifying trace
starts from a live event cluster, persists evidence and hypotheses across steps, waits for
delayed evidence, revises a contradicted hypothesis, restores from a checkpoint after a
service restart, and closes on independently labelled evidence.  Paired traces execute
``fixed_script``, ``no_memory`` and ``full_system`` against the same current fault and tool
budget.  `core/eval/temporal_case_evaluation.py` scores exported traces;
`core/eval/business_value_acceptance.py` scores persisted live cases and sessions.  The
committed 2026-08-31 acceptance run contains six controlled host cases and one repeated-fault
pair.

| evaluation | measured object | valid conclusion | excluded conclusion |
|---|---|---|---|
| temporal case trace | persisted multi-step case | continuity, revision, restart recovery, closing quality and paired memory cost | model generalization until real labelled traces are supplied |
| memory-effectiveness harness | five fixed source cases and deterministic variants | mechanism execution, observed cost and failure boundaries in that harness | production benefit and action safety |
| LongMemEval | answer-bearing session retrieval | recall at k for saved-record lookup | diagnosis or remediation success |
| IODA, SciFact, FortiOS retrieval | ranked document candidates | retrieval-stage relevance | whole-system accuracy |
| multi-agent concurrency microbenchmark | four controlled I/O handlers | scheduling overlap and latency | diagnosis quality, model quality or business completion |
| vector and BM25 index benchmarks | synthetic vectors or documents | index recall, latency, churn and recovery | natural-language relevance or live workload capacity |
| Python and frontend tests | code contracts | regression status for covered branches | an end-to-end business result |
| ITBench references | published third-party results | external task definitions | a local project score |
| RCAEval full metric replay | 733 valid cases from a 735-case labelled index | root-service candidate rank and recurrence negative transfer | open investigation, model grounding or action recovery |
| ITBench-Lite SRE replay | 35 labelled alert and Kubernetes-event snapshots | alert-scope completeness and event retrieval | live interaction or active remediation |
| AIOpsLab source audit | registered fault and mitigation scenario contracts | available external action-test coverage | this project's action success or readback |
| controlled host acceptance | six loopback-bound systemd fault cases | executable takeover, open investigation, grounded decision, same-fault speed, action readback and recurrence paths | population-wide production rates |

## Public AIOps business-value audit

The executable audit is `core/eval/public_aiops_business.py`.  It pins source revisions in
`core/eval/public_aiops_sources.json`, downloads only the modalities required by each metric,
and writes a case-level JSON report.  It makes zero model calls.

The 2026-08-31 run downloaded all 735 RCAEval metric cases and all 35 ITBench-Lite SRE
ground truths, alerting snapshots and Kubernetes-event tables.  Two RCAEval cases have a
published injection timestamp outside the available before/after split, leaving 733 eligible
cases.  The deterministic robust-shift baseline measured root-service Hit@1 0.8104, Hit@3
0.9768 and Hit@5 0.9959.

The chronological memory comparison used 542 recurrence pairs.  Query and indexed text
contained observed metric names only; the verified root service stayed in result metadata.
Memory can intervene only inside the same system, suite and fault class, when at least four
of the five retrieved prior roots agree and that root remains in the current Top-5.  It
intervened in 3 pairs, reduced candidates in all 3, harmed 0 and abstained in 539.  On the
held-out repetition-4-and-later split it again intervened in 3 of 159 pairs, saved 4
candidates and harmed 0.  Mean saving over all 542 pairs is 0.00738 candidates; the low
coverage is part of the result.

The conclusion gate was fitted on repetitions 1 to 3 and evaluated on repetitions 4 and
later.  A root is published only when three distinct current metric signal types appear in
the Top-5.  The 159-case held-out split published 24 roots, all 24 matched the label, and
abstained on 135.  Removing one signal type from the 24 accepted inputs caused 24 of 24 to
abstain, with zero false confirmations.  This establishes the measured precision and
coverage of this deterministic gate on RCAEval; it does not estimate every future open
failure family.

ITBench alert parsing produced a complete asset, time and fault-domain scope in 28 of 35
scenarios.  Kubernetes events contained at least one root-bearing record in 27 scenarios.
BM25 over namespace-filtered events reached Hit@20 0.3704 and macro Recall@20 0.1743.  Eight
scenarios had no root-bearing Kubernetes event and remain unresolved for that source.  This
result identifies the missing adapter work: metrics, logs, traces and configuration state
must join the event evidence before the open-investigation loop can be evaluated.

The AIOpsLab source audit found 89 registered task ids, including 14 mitigation tasks, and 35
scenario files with both fault injection and recovery methods.  These counts describe an
external test facility.  This project's separate controlled host acceptance executed five
eligible actions: successful restarts reached stable original-system readback, while an
always-failing service ended in ``revert_unverified``, required human escalation and stopped
further changes.

The committed artifact `benchmark_results/public_aiops_business_20260831.json` contains
aggregates, all 733 RCAEval case results, all 542 recurrence pairs, every ITBench scenario and
the latest controlled host snapshot.  The host evidence is also committed independently as
`benchmark_results/business_value_acceptance_20260831.json`, so public replay and local action
evidence can be inspected without conflating their sample levels.

`examples/benchmarks.py` remains a developer diagnostic for fixed fixtures.  Its output must
not appear in a release claim, resume metric or whole-system completion statement.

## 1. LongMemEval: saved-record retrieval

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) (Wu et al., ICLR 2025 :
arXiv:2410.10813) is the authoritative long-term-memory benchmark: 500 questions over
long, distractor-saturated multi-session histories, spanning five abilities
(information extraction, multi-session reasoning, temporal reasoning, knowledge update,
abstention).

`core/eval/longmemeval.py` maps a LongMemEval item onto our `TieredMemoryStore` and
measures the metric this project is responsible for : **does tiered retrieval surface
the answer-bearing session out of all the distractors** (recall@k) : with **no LLM in
the loop**, so it is reproducible anywhere.

```bash
# real numbers : download longmemeval_s.json first (link above):
python3 -m core.eval.longmemeval /path/to/longmemeval_s.json 5

# synthetic smoke test (SHIPS in-repo; NOT a LongMemEval result):
python3 -m core.eval.longmemeval tests_py/fixtures/longmemeval_synthetic.json 3
```

The in-repo run reports `recall@3 = 0.75` on the **synthetic** fixture and prints a
`NOT a LongMemEval result` banner. Real-dataset numbers are intentionally left for the
reader to generate : we do not ship a LongMemEval score we did not run.

---

## 5. Retrieval baselines : dense embeddings & cross-encoder reranking (eval-only, optional)

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

### 5.1 Dense vs sparse on the real IODA v2 pool : the FAIR comparison

> **Correction of a label artifact.** An earlier framing headlined a huge
> "structured ≫ dense" gap. That gap is **circular and is not reported as a retrieval
> result**: the IODA relevance labels (`candidate_event_id`) were *defined* by a per-event
> **entity + time-window** pull, and the `structured` retriever scores documents by that
> **same entity+time key** : it reconstructs the label-defining key (and the time window is
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

**Honest finding: dense does NOT beat BM25 here : it is the *worst* of the text retrievers.**
The evidence documents are terse identifier strings (entity id + source + signal type),
so lexical overlap (BM25) carries more signal than a general-purpose sentence embedding,
and sign-bit binary quantization (32× smaller vectors, verified) degrades it further. This
is a real property of identifier-like corpora, reported as measured.

For completeness the **label-key upper bound (diagnostic, not fair)** is
`structured` recall@10 **0.752** / nDCG@10 **0.847**, `rrf` 0.615, `rrf+dense` 0.479 : these
use the time window that *defines* the labels and must never be quoted as a dense/BM25 win.
`core/eval/dense_retrieval.py` prints the fair table first and the upper bound under an
explicit "NOT a fair baseline" banner.

### 5.2 Cross-encoder reranking : where it helps, and where it does not

A two-stage eval: a BM25 first stage fetches a top-k pool, then a cross-encoder
(`cross-encoder/ms-marco-MiniLM-L-6-v2`, ~80 MB CPU) rescores every (query, document) pair
and re-orders it (`core/eval/reranker.py`). Measured first-stage vs +reranker on three
settings : **including the two where reranking does not help, reported honestly:**

| setting | k | first-stage recall / nDCG | +reranker recall / nDCG | Δ recall |
|---|--:|--:|--:|--:|
| (a) IODA fair (832 q, 8542 docs) | @10 | 0.264 / 0.289 | 0.240 / 0.261 | **−9.3%** |
| (b) FortiGate skill routing (6 q, 9 skills) | @3 | 0.833 / 0.833 | 0.833 / 0.833 | **±0.0%** |
| (c) **BEIR SciFact** (300 q, 5183 docs, real qrels) | @10 | 0.779 / 0.663 | **0.793 / 0.679** | **+1.8%** |
| (c) **BEIR SciFact** : top rank | @1 | 0.529 / 0.547 | **0.549 / 0.573** | **+3.8%** (nDCG@1 **+4.9%**) |

- **(a) IODA : reranking *hurts* (−9.3% recall@10).** The evidence docs carry almost no
  free-text signal, so the language model's relevance judgment is noise that displaces
  BM25's lexical hits. Predicted up front, confirmed as measured.
- **(b) FortiGate : no change.** A 9-skill catalog with a first-stage ceiling
  (pool recall@9 = 0.75): reranking can only reorder what BM25 already surfaced, and the
  top-3 set is unchanged. Honest null result.
- **(c) BEIR SciFact : reranking *helps* (+1.8% recall@10, and +4.9% nDCG@1).** This is the
  **externally-valid, non-circular number**: public benchmark, human relevance judgments,
  no dependency on this repo's data. The lift is real but modest and concentrated at the
  very top of the ranking (nDCG@1 +4.9%) : exactly what a reranker is for.

**Takeaway (the honest one):** a cross-encoder reranker is worth its cost only when the
documents carry genuine natural-language semantics *and* the first stage leaves headroom
(SciFact); on terse identifier corpora it can actively hurt (IODA), and on a tiny catalog
bounded by first-stage recall it does nothing (FortiGate). We report all three.

Reproduce (downloads BEIR SciFact from the public UKP mirror with the stdlib; if it is
unreachable the driver skips (c) and reports (a)(b) only):
```bash
python3 -m core.eval.reranker all
```

### 5.3 FortiOS operations-KB RAG : the first REAL ops-doc corpus (eval-only, optional)

This is the honest, application-grounded version of the resume's "运维知识库 RAG (runbook /
工单 / 设备手册)" line. Unlike §5.1 (terse IODA identifier strings) and §5.2's 9-skill
catalog, the corpus here is a **real vendor operations manual**.

- **Corpus (real, not synthesized).** The public **FortiOS 7.4.0 Administration Guide** from
  `docs.fortinet.com` : **all 1,145 sections** downloaded as HTML and converted to text with
  the stdlib parser (`<div id="mc-main-content">` → headings/paragraphs/list-items/table
  cells). **Structure-aware chunking** (split at h1/h2 boundaries, ~1.1 k-char windows) yields
  **9,014 chunks**. Nothing is fabricated; the build is reproducible
  (`python -m core.eval.fortios_corpus build`).
- **Contextual Retrieval, deterministic (no LLM).** Each chunk is prefixed with a document
  context header built purely from its **section-title hierarchy** taken from the guide's own
  table-of-contents tree, e.g. `FortiOS 7.4 Administration Guide > User & Authentication >
  LDAP servers > Configuring an LDAP server`. This is the zero-cost, reproducible variant of
  Anthropic's Contextual Retrieval : we lift the publisher's ground-truth breadcrumb instead
  of asking an LLM to write per-chunk context.
- **Labels : NON-CIRCULAR, and this time it matters.** Queries are the **six real R230
  FortiGate incidents** (held-out set). A manual section is labelled *relevant* iff its prose
  **explains the mechanism of that incident's root cause**, judged by reading the section :
  never by any retriever score. The map + a written per-section rationale are frozen in
  `core/eval/fortios_labels.json`. Every retriever scores on query↔chunk **text** (BM25 term
  overlap / bge cosine / cross-encoder pair score); the labels use the section↔root-cause
  **meaning**. There is **no shared key** (no entity+time join, no title match) a retriever
  could reconstruct : exactly the circularity that invalidated the earlier IODA +334%. (Some
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

**Honest verdict : where the pipeline helps, and where it does NOT:**

- **Hybrid BM25+dense is the only lever that helps.** It lifts **recall@10 0.333 → 0.417
  (+25% relative)** and nDCG@10 0.179 → 0.267, and is the only stage to land a relevant
  section at rank 1. Dense embeddings recover semantically-worded incidents that lexical
  overlap misses (e.g. the DHCP-allocation and security-posture cases), though the fusion also
  reorders mid-ranks (recall@5 dips) : a real, mixed effect, not a clean win.
- **Deterministic Contextual Retrieval gives ~zero lift here** (recall@10 unchanged; nDCG@10
  +0.003). The ToC-hierarchy header adds correct parent terms but does not change which
  sections surface for these six queries. Honest null result : reported, not hidden. (It
  remains cheap insurance and is expected to matter more on deeper, more ambiguous chunks.)
- **Cross-encoder reranking does NOT help recall@10 (flat 0.417) and *hurts* ranking quality**
  (nDCG@10 0.267 → 0.192): it demotes the good top-1 that hybrid found. On this corpus the
  reranker is not worth its cost.
- **Statistical power is low: N = 6.** Each incident is ≈0.17 of recall@10, so the +25% is
  "roughly one more incident's section reaching the top-10." Treat the magnitudes as
  **directional**, not precise. The value of this eval is that it is *real and non-circular*,
  not that it is high-powered : §5.2's BEIR SciFact (300 queries, human qrels) remains the
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
quality. It generates deterministic normalized 128-dimensional Gaussian vectors, takes exact
`IndexFlatIP` top-10 as the oracle, and measures the benchmark HNSW configuration
(`M=32`, `efConstruction=200`) across six `efSearch` values.

| vectors | Flat build | HNSW build | Flat index | HNSW index | HNSW Recall@10 / P95 |
|---:|---:|---:|---:|---:|---:|
| 100,000 | 0.03 s | 30.99 s | 51.20 MB | 78.42 MB | 0.899 / 0.66 ms (`ef=128`) |
| 1,000,000 | 0.31 s | 909.70 s | 512.00 MB | 784.13 MB | 0.846 / 21.37 ms (`ef=1024`) |

The million-vector result exposes the real trade-off: the previous default `efSearch=128`
has P95 2.33 ms but only 0.532 Recall@10; raising it to 1024 reaches 0.846 Recall@10 while
remaining faster than Flat's 36.42 ms P95. The committed 1M artifact is a first-build run
(`cache_hit=false`, `load_seconds=0.0`) and therefore contains no measured 1M reload time.
A serving system should still build offline, persist, and atomically swap generations instead
of rebuilding in a request path; the separate 100k lifecycle artifact measured a 0.7162 s load.

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
python3 -m pytest tests_py/ -q      # 2026-08-31: 1327 passed, 11 skipped, 1 dependency deprecation warning
# persistence coverage: 2 opt-in PostgreSQL 17 container integrations + 12 logic unit tests
# opt-in 100k/1m performance regression: 2 passed
```

## References

- CoALA : Sumers et al., 2023 (arXiv:2309.02427) : tiered memory taxonomy
- Mem0 : Chhikara et al., 2025 (arXiv:2504.19413) : memory write routing
- A-MEM : Xu et al., 2025 (arXiv:2502.12110) : agentic associative memory
- Generative Agents : Park et al., 2023 (arXiv:2304.03442) : reflection
- Voyager : Wang et al., 2023 (arXiv:2305.16291) : procedural skill library
- Agent Workflow Memory : Wang et al., 2024 (arXiv:2409.07429) : reusable workflows
- StreamBench : Wu et al., 2024 (arXiv:2406.08747) : online continual improvement
- LongMemEval : Wu et al., ICLR 2025 (arXiv:2410.10813) : long-term memory eval
- GRPO / DeepSeekMath : Shao et al., 2024 (arXiv:2402.03300) : group-relative policy opt (roadmap)
- Ebbinghaus, 1885 : *Über das Gedächtnis* : the forgetting curve
