"""Measure whether device portraits reduce investigation probes, without an LLM.

The experimental unit is one real device event plus the historical facts used to
build its portrait.  All four arms see the same event and deterministic rule
reasoner.  They differ only in probe order: M uses the portrait, A1 keeps the
fixed order, A2 receives the same number of irrelevant hints, and A0 runs no
probe.  The outcome is the prefix length needed to reproduce the conclusion of
the complete sweep.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import statistics
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from domains.network_rca.device_profile import Anomaly, ProfileStore
from domains.network_rca.fortigate_stream import FortiEvent
from frontend.gateway.app.investigate import TRIAGE_PROBES, order_triage_by_profile
from frontend.gateway.ingest.facts_ingest import (
    CH_DB,
    CH_PASS,
    CH_TABLE,
    CH_URL,
    CH_USER,
    parse_line,
)


TARGET_DEVICES = ("192.168.16.56", "192.168.16.28", "192.168.16.73")
MAX_DEVICES = 12
ROWS_PER_DEVICE = 2_000
ASSUMED_PAIRED_SD_PROBES = 2.0

# These are evidence routes, not portrait rules.  The full sweep establishes a
# reference conclusion mechanically, then each prefix is checked against it.
_CONCLUSION_PROBES: dict[str, frozenset[str]] = {
    "first_deny": frozenset({
        "journalctl -p err -n 40 --no-pager --since -24h",
    }),
    "new_peer": frozenset({"ip neigh show", "ss -tulpn"}),
    "new_interface": frozenset({"ip -br link show"}),
    "session_spike": frozenset({"ss -tulpn"}),
    "peer_outlier": frozenset({"ss -tulpn", "ip route show"}),
    "volume_outlier": frozenset({"ss -tulpn", "ip -br link show"}),
}

# A2 has one false anomaly hint per real hint.  Its probes are selected from
# checks that cannot establish any portrait conclusion in this harness.
_SHAM_PROBES = (
    "systemctl --failed --no-legend",
    "df -h",
    "free -m",
    "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz",
    "dmesg -T --level err,crit,alert -x",
)


@dataclass(frozen=True, slots=True)
class Fact:
    at: datetime
    device: str
    dst_ip: str | None
    dst_port: int | None
    proto: int | None
    action: str | None
    event_type: str
    subtype: str
    src_intf: str | None
    dst_intf: str | None
    sent_bytes: int
    rcvd_bytes: int

    def event(self, *, device: str | None = None) -> FortiEvent:
        source = device or self.device
        return FortiEvent(
            at=self.at,
            logid="clickhouse-fact",
            type=self.event_type,
            subtype=self.subtype,
            level="notice",
            action=self.action,
            src_ip=source,
            dst_ip=self.dst_ip,
            src_port=None,
            dst_port=self.dst_port,
            proto=self.proto,
            src_intf=self.src_intf,
            dst_intf=self.dst_intf,
            user=None,
            logdesc=None,
            msg=None,
            status=None,
            sent_bytes=self.sent_bytes,
            rcvd_bytes=self.rcvd_bytes,
            raw={},
        )


@dataclass(frozen=True, slots=True)
class DeviceQuestion:
    case_id: str
    subject: str
    baseline_device: str
    question: str
    candidate_at: str
    anomaly_types: tuple[str, ...]
    anomaly_explanations: tuple[str, ...]
    anomaly_numbers: tuple[dict[str, int | float], ...]

    @property
    def conclusion(self) -> tuple[str, ...]:
        return self.anomaly_types or ("no_profile_anomaly",)


@dataclass(frozen=True, slots=True)
class ArmResult:
    case_id: str
    subject: str
    arm: str
    hint_count: int
    hints: tuple[str, ...]
    probe_order: tuple[str, ...]
    reached_same_conclusion: bool
    probes_required: int | None
    conclusion: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ContributionReport:
    power_statement: str
    design: dict[str, Any]
    arms: dict[str, dict[str, Any]]
    primary_comparison: dict[str, Any]
    conclusion: str
    cases: tuple[DeviceQuestion, ...]
    raw_results: tuple[ArmResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "power_statement": self.power_statement,
            "design": self.design,
            "arms": self.arms,
            "primary_comparison": self.primary_comparison,
            "conclusion": self.conclusion,
            "cases": [asdict(case) for case in self.cases],
            "raw_results": [asdict(row) for row in self.raw_results],
        }


def _integer(value: Any) -> int | None:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return None
    return number or None


def _timestamp(value: Any) -> datetime:
    raw = str(value).replace(" ", "T")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fact_from_row(row: dict[str, Any]) -> Fact:
    return Fact(
        at=_timestamp(row["event_ts"]),
        device=str(row.get("device_key") or row.get("srcip") or ""),
        dst_ip=str(row.get("dstip") or "") or None,
        dst_port=_integer(row.get("dstport")),
        proto=_integer(row.get("proto")),
        action=str(row.get("action") or "") or None,
        event_type=str(row.get("type") or "traffic"),
        subtype=str(row.get("subtype") or "forward"),
        src_intf=str(row.get("srcintf") or "") or None,
        dst_intf=str(row.get("dstintf") or "") or None,
        sent_bytes=int(row.get("sentbyte") or 0),
        rcvd_bytes=int(row.get("rcvdbyte") or 0),
    )


def _ch_rows(sql: str) -> list[dict[str, Any]]:
    url = f"{CH_URL}/?query={urllib.parse.quote(sql + ' FORMAT JSONEachRow')}"
    request = urllib.request.Request(
        url,
        headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASS},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return [
            json.loads(line)
            for line in response.read().decode("utf-8").splitlines()
            if line.strip()
        ]


def load_real_facts() -> tuple[list[Fact], str]:
    """Load a bounded real cohort from ClickHouse, with a real-log offline copy.

    The repository fallback is used only when the fact store is unreachable.  It
    keeps the command runnable in isolated CI and is identified verbatim in the
    report, so a fallback run cannot be mistaken for a ClickHouse run.
    """
    quoted_targets = ",".join(f"'{item}'" for item in TARGET_DEVICES)
    sql = (
        "WITH recent AS (SELECT max(event_ts) AS latest FROM "
        f"{CH_DB}.{CH_TABLE}) "
        "SELECT event_ts, device_key, srcip, dstip, dstport, proto, action, service, "
        "type, subtype, srcintf, dstintf, sentbyte, rcvdbyte "
        f"FROM {CH_DB}.{CH_TABLE} WHERE event_ts >= "
        "(SELECT latest - INTERVAL 8 DAY FROM recent) AND ("
        f"device_key IN ({quoted_targets}) OR device_key IN ("
        f"SELECT device_key FROM {CH_DB}.{CH_TABLE} WHERE event_ts >= "
        "(SELECT latest - INTERVAL 8 DAY FROM recent) "
        "AND startsWith(device_key, '192.168.') GROUP BY device_key "
        f"ORDER BY count() DESC LIMIT {MAX_DEVICES})) "
        f"ORDER BY device_key, event_ts DESC LIMIT {ROWS_PER_DEVICE} BY device_key"
    )
    try:
        rows = _ch_rows(sql)
        facts = [_fact_from_row(row) for row in rows]
        if facts:
            return facts, f"ClickHouse {CH_DB}.{CH_TABLE} ({CH_URL})"
        failure = "query returned no rows"
    except Exception as error:  # noqa: BLE001 - isolated CI uses the committed real sample
        failure = f"{type(error).__name__}: {error}"

    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "domains/network_rca/fixtures/real/syslog").glob("*.log"))
    facts: list[Fact] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="ignore") as source:
            for line in source:
                row = parse_line(line)
                if row is None:
                    continue
                facts.append(_fact_from_row(dict(zip(
                    (
                        "event_ts", "device_key", "srcip", "dstip", "dstport",
                        "proto", "action", "service", "app", "type", "subtype",
                        "srcintf", "dstintf", "dstcountry", "srcname", "sentbyte",
                        "rcvdbyte",
                    ),
                    row,
                ))))
    return facts, f"repository real FortiGate snapshot; ClickHouse unavailable ({failure})"


def _anomalies_against(
    subject: str,
    baseline: Sequence[Fact],
    candidates: Sequence[Fact],
    cohort: Sequence[Fact] = (),
) -> tuple[Fact, list[Anomaly]]:
    """Choose the most informative real candidate against a fixed real baseline."""
    if any(fact.device != subject for fact in baseline):
        raise ValueError("a device baseline may contain only that device's own facts")
    ranked: list[tuple[int, int, Fact, list[Anomaly]]] = []
    priority = {
        "first_deny": 6, "new_peer": 5, "new_interface": 4,
        "session_spike": 3, "peer_outlier": 2, "volume_outlier": 1,
    }
    for index, fact in enumerate(sorted(candidates, key=lambda item: item.at)):
        profile = ProfileStore()
        for baseline_fact in sorted(baseline, key=lambda item: item.at):
            profile.observe(baseline_fact.event())
        grouped: dict[str, list[Fact]] = {}
        for cohort_fact in cohort:
            if cohort_fact.at < fact.at:
                grouped.setdefault(cohort_fact.device, []).append(cohort_fact)
        for device, rows in grouped.items():
            profile.seed_group_summary(
                device,
                sessions=len(rows),
                peer_count=len({row.dst_ip for row in rows if row.dst_ip}),
                accepted=sum(row.action == "accept" for row in rows),
                denied=sum(row.action == "deny" for row in rows),
                known_peers=(fact.dst_ip,) if device == subject and fact.dst_ip in {
                    row.dst_ip for row in rows
                } else (),
            )
        anomalies = profile.anomalies(fact.event())
        score = sum(priority.get(item.type, 0) for item in anomalies)
        ranked.append((score, index, fact, anomalies))
    if not ranked:
        raise ValueError(f"no candidate facts for {subject}")
    _, _, candidate, anomalies = max(ranked, key=lambda item: (item[0], item[1]))
    return candidate, anomalies


def _own_history_case(
    subject: str, facts: Sequence[Fact], cohort: Sequence[Fact]
) -> tuple[Fact, list[Anomaly], str]:
    ordered = sorted(facts, key=lambda item: item.at)
    if len(ordered) == 1:
        candidate, anomalies = _anomalies_against(subject, (), ordered, cohort)
        return candidate, anomalies, subject
    split = max(1, min(len(ordered) - 1, len(ordered) // 2))
    candidate, anomalies = _anomalies_against(
        subject, ordered[:split], ordered[split:], cohort
    )
    return candidate, anomalies, subject


def build_device_questions(facts: Sequence[Fact], *, limit: int = MAX_DEVICES) -> list[DeviceQuestion]:
    grouped: dict[str, list[Fact]] = {}
    for fact in facts:
        if fact.device:
            grouped.setdefault(fact.device, []).append(fact)
    if not grouped:
        return []

    selected = [device for device in TARGET_DEVICES if device in grouped]
    selected.extend(
        device
        for device, _rows in sorted(
            grouped.items(), key=lambda item: (-len(item[1]), item[0])
        )
        if device not in selected and device.startswith("192.168.")
    )
    selected = selected[:limit]

    questions: list[DeviceQuestion] = []
    for index, subject in enumerate(selected):
        rows = grouped[subject]
        candidate, anomalies, baseline_device = _own_history_case(subject, rows, facts)
        questions.append(DeviceQuestion(
            case_id=f"device-{index + 1:02d}-{subject}",
            subject=subject,
            baseline_device=baseline_device,
            question=f"查 {subject} 这台设备怎么了",
            candidate_at=candidate.at.isoformat(),
            anomaly_types=tuple(item.type for item in anomalies),
            anomaly_explanations=tuple(item.explanation for item in anomalies),
            anomaly_numbers=tuple(dict(item.numbers) for item in anomalies),
        ))
    return questions


def _same_conclusion_after(question: DeviceQuestion, order: Sequence[str]) -> int | None:
    wanted = set(question.anomaly_types)
    if wanted:
        confirmed: set[str] = set()
        for count, probe in enumerate(order, start=1):
            confirmed.update(
                anomaly_type
                for anomaly_type in wanted
                if probe in _CONCLUSION_PROBES[anomaly_type]
            )
            if confirmed == wanted:
                return count
        return None

    # A negative portrait conclusion needs coverage of every rule family.  Shared
    # connection evidence may cover both peer novelty and a session spike.
    covered: set[str] = set()
    for count, probe in enumerate(order, start=1):
        covered.update(
            anomaly_type
            for anomaly_type, probes in _CONCLUSION_PROBES.items()
            if probe in probes
        )
        if covered == set(_CONCLUSION_PROBES):
            return count
    return None


def _sham_order(question: DeviceQuestion) -> tuple[list[str], tuple[str, ...]]:
    hint_count = len(question.anomaly_types)
    digest = hashlib.sha256(question.case_id.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:2], "big") % len(_SHAM_PROBES)
    pool = list(_SHAM_PROBES[offset:] + _SHAM_PROBES[:offset])
    prefix = pool[:hint_count]
    return prefix + [probe for probe in TRIAGE_PROBES if probe not in prefix], tuple(
        f"irrelevant_anomaly_{index + 1}" for index in range(hint_count)
    )


def evaluate_question(question: DeviceQuestion) -> list[ArmResult]:
    m_order = order_triage_by_profile(list(TRIAGE_PROBES), list(question.anomaly_types))
    sham_order, sham_hints = _sham_order(question)
    plans = (
        ("M", m_order, question.anomaly_types),
        ("A1", list(TRIAGE_PROBES), ()),
        ("A2", sham_order, sham_hints),
        ("A0", [], ()),
    )
    rows: list[ArmResult] = []
    for arm, order, hints in plans:
        count = _same_conclusion_after(question, order)
        rows.append(ArmResult(
            case_id=question.case_id,
            subject=question.subject,
            arm=arm,
            hint_count=len(hints),
            hints=tuple(hints),
            probe_order=tuple(order),
            reached_same_conclusion=count is not None,
            probes_required=count,
            conclusion=question.conclusion if count is not None else None,
        ))
    return rows


def _mean_ci(values: Sequence[float], *, samples: int = 4_000) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(20_260_823)
    means = sorted(
        statistics.mean(rng.choice(values) for _ in values)
        for _ in range(samples)
    )
    return [
        round(means[int(0.025 * (samples - 1))], 3),
        round(means[int(0.975 * (samples - 1))], 3),
    ]


def paired_randomization_p(values: Sequence[float]) -> float:
    """Two-sided exact sign-flip test for the paired mean difference."""
    nonzero = [abs(value) for value in values if value != 0]
    if not nonzero:
        return 1.0
    observed = abs(sum(values))
    if len(nonzero) <= 20:
        totals = (
            abs(sum(sign * value for sign, value in zip(signs, nonzero)))
            for signs in itertools.product((-1, 1), repeat=len(nonzero))
        )
        extreme = sum(total >= observed - 1e-12 for total in totals)
        return extreme / (2 ** len(nonzero))
    rng = random.Random(20_260_823)
    samples = 100_000
    extreme = sum(
        abs(sum((1 if rng.random() < 0.5 else -1) * value for value in nonzero))
        >= observed - 1e-12
        for _ in range(samples)
    )
    return (extreme + 1) / (samples + 1)


def _arm_metrics(rows: Iterable[ArmResult]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[ArmResult]] = {arm: [] for arm in ("M", "A1", "A2", "A0")}
    for row in rows:
        grouped[row.arm].append(row)
    metrics: dict[str, dict[str, Any]] = {}
    for arm, values in grouped.items():
        counts = [row.probes_required for row in values if row.probes_required is not None]
        metrics[arm] = {
            "n": len(values),
            "reached_same_conclusion_n": len(counts),
            "mean_probes": round(statistics.mean(counts), 3) if counts else None,
            "median_probes": round(statistics.median(counts), 3) if counts else None,
            "min_probes": min(counts) if counts else None,
            "max_probes": max(counts) if counts else None,
        }
    return metrics


def run_contribution(
    facts: Sequence[Fact] | None = None,
    *,
    source: str | None = None,
    limit: int = MAX_DEVICES,
) -> ContributionReport:
    if facts is None:
        loaded, loaded_source = load_real_facts()
        facts = loaded
        source = source or loaded_source
    source = source or "caller-supplied real fact rows"
    cases = build_device_questions(facts, limit=limit)
    if not cases:
        raise RuntimeError("no real device questions could be built")

    raw = [row for case in cases for row in evaluate_question(case)]
    by_key = {(row.case_id, row.arm): row for row in raw}
    paired = [
        (
            by_key[(case.case_id, "M")],
            by_key[(case.case_id, "A2")],
        )
        for case in cases
        if by_key[(case.case_id, "M")].probes_required is not None
        and by_key[(case.case_id, "A2")].probes_required is not None
    ]
    savings = [
        float(a2.probes_required - memory.probes_required)  # type: ignore[operator]
        for memory, a2 in paired
    ]
    paired_n = len(savings)
    mde = (
        (1.959963984540054 + 0.8416212335729143)
        * ASSUMED_PAIRED_SD_PROBES
        / math.sqrt(paired_n)
        if paired_n
        else None
    )
    p_value = paired_randomization_p(savings)
    mean_saved = statistics.mean(savings) if savings else 0.0
    significant = bool(savings) and mean_saved > 0 and p_value < 0.05
    power_statement = (
        f"样本量与功效：n={paired_n} 个真实设备问题；双侧配对检验 α=0.05，"
        f"按预设配对差标准差 {ASSUMED_PAIRED_SD_PROBES:.1f} 条探针，80% 功效可检测"
        f"至少 {mde:.2f} 条的平均差。"
        if mde is not None
        else "样本量与功效：n=0；没有可用于 M vs A2 的配对样本，无法估计功效。"
    )
    if significant:
        conclusion = (
            f"画像排序带来可测增益：M 相对 A2 平均少 {mean_saved:.2f} 条探针，"
            f"双侧配对随机化检验 p={p_value:.4f}。"
        )
    else:
        conclusion = (
            "画像在这个场景没带来可测增益："
            f"M 相对 A2 平均少 {mean_saved:.2f} 条探针，p={p_value:.4f}；"
            "当前差值或样本量没有越过预先声明的显著性门槛。"
        )

    primary = {
        "comparison": "M_vs_A2",
        "paired_n": paired_n,
        "mean_probes_saved_by_M": round(mean_saved, 3),
        "median_probes_saved_by_M": round(statistics.median(savings), 3) if savings else None,
        "bootstrap_95_ci_mean_saved": _mean_ci(savings),
        "paired_randomization_two_sided_p": round(p_value, 6),
        "significant_at_0.05": significant,
        "mde_80pct_power_probes": round(mde, 3) if mde is not None else None,
        "paired_differences_A2_minus_M": savings,
    }
    return ContributionReport(
        power_statement=power_statement,
        design={
            "data_source": source,
            "sample_n": len(cases),
            "llm_calls": 0,
            "reasoner": "deterministic portrait-evidence rules",
            "outcome": "probes needed to reproduce the complete-sweep conclusion",
            "primary_comparison": "M vs A2",
            "arms": {
                "M": "portrait-ordered probes",
                "A1": "fixed original order without portrait",
                "A2": "same anomaly-hint count, irrelevant probe targets",
                "A0": "no probes",
            },
        },
        arms=_arm_metrics(raw),
        primary_comparison=primary,
        conclusion=conclusion,
        cases=tuple(cases),
        raw_results=tuple(raw),
    )


def render_report(report: ContributionReport) -> str:
    lines = [
        report.power_statement,
        f"数据源：{report.design['data_source']}",
        "指标：达到完整探针扫描同一结论所需的探针数；主对比 M vs A2。",
        "",
        "四臂结果：",
    ]
    for arm in ("M", "A1", "A2", "A0"):
        metric = report.arms[arm]
        lines.append(
            f"  {arm}: reached={metric['reached_same_conclusion_n']}/{metric['n']}, "
            f"mean={metric['mean_probes']}, median={metric['median_probes']}"
        )
    primary = report.primary_comparison
    lines.extend((
        "",
        (
            "M vs A2：平均少 "
            f"{primary['mean_probes_saved_by_M']:.3f} 条，"
            f"95% bootstrap CI={primary['bootstrap_95_ci_mean_saved']}，"
            f"paired-randomization p={primary['paired_randomization_two_sided_p']:.6f}。"
        ),
        report.conclusion,
        "A0 空跑达到同一结论的样本数："
        f"{report.arms['A0']['reached_same_conclusion_n']}。",
        "",
        "逐设备画像提示：",
    ))
    for case in report.cases:
        hints = "; ".join(case.anomaly_explanations) or "未见画像偏离"
        counts = {
            row.arm: row.probes_required
            for row in report.raw_results
            if row.case_id == case.case_id
        }
        lines.append(
            f"  {case.subject} (baseline={case.baseline_device}, {case.candidate_at}, "
            f"M/A1/A2/A0={counts['M']}/{counts['A1']}/{counts['A2']}/{counts['A0']}): "
            f"{hints}"
        )
    return "\n".join(lines)


def main() -> None:
    print(render_report(run_contribution()))


if __name__ == "__main__":
    main()


__all__ = [
    "ArmResult",
    "ContributionReport",
    "DeviceQuestion",
    "Fact",
    "build_device_questions",
    "evaluate_question",
    "load_real_facts",
    "paired_randomization_p",
    "render_report",
    "run_contribution",
]
