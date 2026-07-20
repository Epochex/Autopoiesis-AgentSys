# FAIR IODA Hybrid Diagnosis

## Setup

The diagnostic used all 832 IODA events and 8,542 evidence documents. Queries and documents were the fair text-only representations in `ioda_retrieval.py` and `dense_retrieval.py`; the event time window was excluded. Dense retrieval used the cached `BAAI/bge-small-en-v1.5` embeddings and exact flat cosine search. All recall values are macro recall@10.

Top-10 RRF used route depth 10 and the repository default constant `c=60`. The weighted score was

`1 / (60 + rank_bm25) + w / (60 + rank_dense)`.

The repository's existing `rrf-fair` is a three-route fusion of BM25, `structured_no_time`, and dense-flat. Because the requested diagnosis concerns BM25+dense, the tables distinguish that existing result from the two-route fusion.

## Baseline reproduction

| Method | Macro recall@10 |
|---|---:|
| BM25 | 0.264442 |
| dense-flat | 0.173857 |
| existing `rrf-fair` (BM25 + no-time structured + dense) | 0.253627 |
| equal-weight RRF (BM25 + dense only) | 0.245452 |

The first three values reproduce the known rounded results: 0.264, 0.174, and 0.254. Equal-weight two-route fusion loses 0.018990 recall relative to BM25.

## Dense-weight intervention

| Dense weight `w` | Macro recall@10 |
|---:|---:|
| 1.00 | 0.245452 |
| 0.75 | 0.266345 |
| 0.50 | 0.266345 |
| 0.25 | 0.266345 |
| 0.10 | 0.266345 |
| 0.00 | 0.264442 |

The stated monotonic hypothesis does **not** hold: recall jumps by 0.020893 when dense is reduced from 1.00 to 0.75, remains 0.266345 through `w=0.10`, then falls by 0.001903 when dense is removed. This intervention identifies equal voting by the weaker dense route as the drag, not dense participation itself. At depth 10, a sub-unit dense contribution mainly boosts BM25/dense consensus documents and fills queries where BM25 returns fewer than 10 candidates. At `w=1.0`, dense-only high ranks can displace BM25 documents, reducing recall.

## Rank of the first relevant document

For each query, the answer rank is the rank of its first relevant document in the complete method ranking. Dense and RRF rank all documents. BM25 ranks only positive-score lexical candidates; its 14 unranked answers were assigned the explicit sentinel rank 8,543 (`N+1`) for the all-query mean.

| Method | Median rank | Mean rank, all 832 | Mean when ranked | Unranked |
|---|---:|---:|---:|---:|
| BM25 | 4 | 162.239183 | 18.803178 | 14 |
| dense-flat | 6 | 23.079327 | 23.079327 | 0 |
| RRF (BM25 + dense) | 4 | 21.030048 | 21.030048 | 0 |

Dense ranks the first relevant document worse than BM25 on 271/832 queries (0.325721). Dense's lower all-query mean is caused by covering the 14 queries absent from BM25's positive-score ranking; among ranked BM25 queries, BM25's mean rank is better (18.803178 versus 23.079327), and its median is better (4 versus 6).

## Demotions and gains

Using the requested binary definition—whether a query has any relevant document in the top 10—the expected negative net does **not** occur:

| Transition | Queries |
|---|---:|
| BM25 hit → hybrid miss | 44 |
| BM25 miss → hybrid hit | 48 |
| Net hybrid binary wins | +4 |

Binary hit counts hide partial recall for events with several relevant documents. BM25 has higher per-query recall on 198 queries, hybrid has higher recall on 104, and 530 tie. Summed per-query recall advantages are 31.258282 for BM25 and 15.458738 for hybrid; their difference, divided by 832, is the observed 0.018990 macro-recall gap. The hybrid therefore gains four more any-hit queries but loses substantially more relevant-document coverage.

## Dense top-1 failure mode

Dense top-1 is wrong on 628/832 queries. Of those errors:

| Relationship to query entity | Count | Fraction of wrong top-1s |
|---|---:|---:|
| Same entity type, different entity | 71 | 0.113057 |
| Same entity type, same entity | 538 | 0.856688 |
| Different entity type | 19 | 0.030255 |

Five deterministic examples from manifest order follow. `Labeled event` is report-only ground truth and was never included in retrieval text.

| Query event | Query entity | Wrong dense top-1 | Labeled event | Same type, different entity? |
|---|---|---|---|---|
| `radar:225` | country `GM` | country `GM`, Cloudflare recovery | `radar:385` | No |
| `radar:224` | country `KZ` | country `KZ`, Cloudflare onset | `radar:217` | No |
| `radar:223` | country `BF` | country `BF`, Cloudflare onset | `radar:741` | No |
| `radar:222` | country `TO` | country `GN`, IODA BGP recovery | `radar:872` | Yes |
| `radar:221` | country `YE` | country `YE`, Cloudflare onset | `radar:655` | No |

The proposed identifier-blurring mechanism is not the dominant dense failure: only 11.3057% of wrong top-1s preserve the type while substituting a different entity. In 85.6688%, dense retrieves the exact query entity but selects evidence assigned to another event. With the time key withheld and many events sharing an entity, the dense representation cannot identify the correct event instance from the remaining short, repetitive source/signal/phase text.

## Mechanistic conclusion

The controlled weight sweep proves that the equal-weight dense vote causes the two-route hybrid deficit: reducing its weight from 1.00 to 0.75 changes no data or retriever and raises recall@10 from 0.245452 to 0.266345. Dense is a weak top-10 route on this task (0.173857), so equal RRF promotes dense-only candidates strongly enough to remove useful BM25 evidence. A small dense vote is beneficial as a consensus/fallback signal, which is why the strict monotonic-to-BM25 hypothesis fails.

The concrete dense error analysis revises the proposed root cause. The main problem is wrong-event selection under repeated exact entities and no time key, not embeddings replacing one identifier with a semantically similar identifier. Equal-rank fusion converts that event-level ambiguity into BM25 demotions.
