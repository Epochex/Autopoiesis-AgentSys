"""Benchmark-scenario report for the frontend `/api/rca/benchmark` page.

``build_benchmark_report`` serves two things, each honestly labelled:

* **LongMemEval-500** — a REAL local run of the memory/retrieval layer.  The numbers
  are read verbatim from ``eval_mem_compare/results_core.json`` +
  ``eval_mem_compare/results_mem0.json`` (the same files ``eval_mem_compare/report.py``
  merges) and only reshaped — never recomputed.  This is an LLM-free recall@k metric,
  and on it the BM25 lexical floor tops the table; the note says so plainly rather
  than spinning the tiered store as the winner.

* **ITBench** (IBM, ICML 2025) — PUBLISHED reference baselines only.  No local run has
  been done (needs a k3s/k8s cluster), so it is marked ``live: false`` and every
  number is a public SOTA figure, not an invented "our score".

Nothing is fabricated: LongMemEval cells come from the json, ITBench cells are the
published leaderboard/paper figures hardcoded as clearly-labelled reference.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# repo root: .../core/eval/benchmark_report.py -> parents[2]
_ROOT = Path(__file__).resolve().parents[2]
_RESULTS_CORE = _ROOT / "eval_mem_compare" / "results_core.json"
_RESULTS_MEM0 = _ROOT / "eval_mem_compare" / "results_mem0.json"

_K_GRID = [1, 3, 5, 10]
_ABILITIES = [
    "knowledge-update",
    "multi-session",
    "single-session-assistant",
    "single-session-preference",
    "single-session-user",
    "temporal-reasoning",
]

# Row order + stable ids/labels for each system name as it appears in the json.
# (Mirrors the ORDER in eval_mem_compare/report.py: system-under-test first.)
_SYSTEMS = [
    ("tiered (this repo)", "tiered", "Tiered (this repo)", True),
    ("Mem0 (mem0ai, infer=False)", "mem0", "Mem0 (mem0ai, infer=False)", False),
    ("Reflexion (reflective retrieval)", "reflexion", "Reflexion (reflective retrieval)", False),
    ("flat vector (same embedder)", "flat_vector", "Flat vector (same embedder)", False),
    ("BM25 (lexical floor)", "bm25", "BM25 (lexical floor)", False),
]


def _load_results() -> dict[str, Any]:
    """Read + merge the two REAL result files, exactly like eval_mem_compare/report.py."""
    merged: dict[str, Any] = {}
    for fp in (_RESULTS_CORE, _RESULTS_MEM0):
        if fp.exists():
            merged.update(json.loads(fp.read_text()))
    return merged


def _round(value: float | None) -> float | None:
    return round(float(value), 4) if value is not None else None


def _longmemeval_systems(results: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, sid, label, is_self in _SYSTEMS:
        entry = results.get(name)
        if not entry:
            continue
        recall = {
            str(k): _round(entry.get(str(k), {}).get("recall_at_k")) for k in _K_GRID
        }
        cell5 = entry.get("5", {})
        by_type5 = cell5.get("by_type", {})
        out.append(
            {
                "id": sid,
                "label": label,
                "self": is_self,
                "recall": recall,
                "ans_hit5": _round(cell5.get("answer_string_hit")),
                "by_ability5": {
                    ability: _round(by_type5.get(ability)) for ability in _ABILITIES
                },
            }
        )
    return out


def build_benchmark_report(lang: str = "zh") -> dict[str, Any]:
    """Build the benchmark-scenario payload for the frontend (contract is locked).

    ``lang`` is accepted for API symmetry with the other builders; the payload is
    bilingual by contract, so the value only selects which text a monolingual caller
    would read — both are always returned.
    """
    _ = lang  # bilingual payload; parameter kept for API symmetry
    results = _load_results()
    systems = _longmemeval_systems(results)

    return {
        "ok": True,
        "longmemeval": {
            "dataset": "LongMemEval-500",
            "n": 500,
            "live": True,
            "metric": "recall@k · LLM-free",
            "ks": list(_K_GRID),
            "abilities": list(_ABILITIES),
            "systems": systems,
            "note": {
                "zh": (
                    "这是无 LLM 的 recall@k 检索指标：在这一项上 BM25 词法基线（recall@5 0.97）"
                    "领先分层记忆库（recall@5 0.906），如实呈现，不粉饰为分层库获胜。"
                    "该指标只衡量“能否检索到答案所在会话”，不衡量生成质量；分层库的价值在"
                    "多会话/时序等能力维度与可演化记忆机制，而非这条纯词法友好的召回曲线。"
                ),
                "en": (
                    "This is an LLM-free recall@k retrieval metric: on it the BM25 lexical "
                    "floor (recall@5 0.97) leads the tiered memory store (recall@5 0.906). "
                    "Stated plainly — the tiered store does not top this table. The metric "
                    "only measures whether the answer-bearing session is retrieved, not "
                    "generation quality; the tiered store's value shows in multi-session / "
                    "temporal abilities and its evolving-memory mechanics, not this purely "
                    "lexical-friendly recall curve."
                ),
            },
        },
        "itbench": {
            "name": "ITBench",
            "live": False,
            "status": {
                "zh": "公开基线 · 本地 k3s 待跑",
                "en": "published baseline · local k3s run pending",
            },
            "paper_url": "https://arxiv.org/abs/2502.05352",
            "repo_url": "https://github.com/itbench-hub/ITBench",
            "leaderboard_url": "https://artificialanalysis.ai/evaluations/itbench-aa",
            "total_scenarios": 102,
            "groups": [
                {
                    "id": "sre",
                    "label": {"zh": "SRE · 事件/根因", "en": "SRE · incident/RCA"},
                    "maps_to": {"zh": "内网 RCA(page1/2)", "en": "network RCA (page1/2)"},
                    "sota_pct": 11.4,
                },
                {
                    "id": "ciso",
                    "label": {"zh": "CISO · 安全合规运营", "en": "CISO · security & compliance"},
                    "maps_to": {"zh": "自我渗透/安全(page3)", "en": "self-pentest/security (page3)"},
                    "sota_pct": 25.2,
                },
                {
                    "id": "finops",
                    "label": {"zh": "FinOps · 成本运营", "en": "FinOps"},
                    "maps_to": {"zh": "(加成)", "en": "(bonus)"},
                    "sota_pct": 25.8,
                },
            ],
            "note": {
                "zh": (
                    "ITBench 为公开基准（IBM，ICML 2025，arXiv:2502.05352），102 个真实场景；"
                    "公开 SOTA 智能体的解决率很低——SRE 11.4%、CISO 25.2%、FinOps 25.8%"
                    "（不含异常检测），说明这类任务仍极难、提升空间大。本项目尚未在本地跑，"
                    "需 k8s/k3s 集群，故标注为公开基线、非本地成绩。"
                ),
                "en": (
                    "ITBench is a published benchmark (IBM, ICML 2025, arXiv:2502.05352) of "
                    "102 real-world scenarios. Published SOTA agents solve very little — "
                    "SRE 11.4%, CISO 25.2%, FinOps 25.8% (excl. anomaly detection) — so the "
                    "task stays hard with plenty of headroom. No local run has been done "
                    "yet; it needs a k8s/k3s cluster, hence labelled a published baseline, "
                    "not a local score."
                ),
            },
        },
        "coverage": [
            {
                "cap": {"zh": "内网根因分析", "en": "network RCA"},
                "benchmark": "ITBench · SRE",
                "covered": True,
            },
            {
                "cap": {"zh": "自我渗透/安全", "en": "self-pentest/security"},
                "benchmark": "ITBench · CISO",
                "covered": True,
            },
            {
                "cap": {"zh": "长期记忆/检索", "en": "long-term memory/retrieval"},
                "benchmark": "LongMemEval-500",
                "covered": True,
            },
            {
                "cap": {"zh": "自演化/技能归纳", "en": "self-evolution/skill induction"},
                "benchmark": {"zh": "无公开基准 · 原创贡献", "en": "no public benchmark · novel"},
                "covered": False,
            },
        ],
    }
