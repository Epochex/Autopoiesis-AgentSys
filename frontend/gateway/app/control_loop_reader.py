from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import yaml

from .config import Settings

STATIC_TOPOLOGY_NOTES = [
  {
    'title': 'Deterministic before LLM',
    'detail': (
      'The console keeps LCORE fault annotation, deterministic alerting, and '
      'AIOps reasoning as separate planes instead of letting the model create alerts.'
    ),
  },
  {
    'title': 'Topology is evidence',
    'detail': (
      'The current reasoning path carries a topology-aware subgraph that separates '
      'root-candidate, symptom, and noise nodes before stage requests are assembled.'
    ),
  },
  {
    'title': 'LLM calls are gated',
    'detail': (
      'Low-value transient slices stay on the bounded template path, while '
      'high-value fault-localization slices remain eligible for external LLM review.'
    ),
  },
  {
    'title': 'Remediation stays bounded',
    'detail': (
      'The output is still operator guidance only: execution, approval, and rollback '
      'remain visible boundaries rather than hidden write-back behavior.'
    ),
  },
]

CONTROL_SPECS = [
  (
    'rule-deny-threshold',
    'RULE_DENY_THRESHOLD',
    'core/deployments/40-core-correlator.yaml',
    'Legacy traffic-burst rule retained for compatibility; LCORE control loop is driven by annotated_fault_v1.',
  ),
  (
    'rule-cooldown',
    'RULE_ALERT_COOLDOWN_SEC',
    'core/deployments/40-core-correlator.yaml',
    'Prevents repeated alert emission from dominating the current event story.',
  ),
  (
    'cluster-window',
    'AIOPS_CLUSTER_WINDOW_SEC',
    'core/deployments/80-core-aiops-agent.yaml',
    'The live cluster-scope path is coded and waiting for a natural same-key hit window.',
  ),
  (
    'cluster-min',
    'AIOPS_CLUSTER_MIN_ALERTS',
    'core/deployments/80-core-aiops-agent.yaml',
    'This is the watch threshold the UI surfaces as pre-trigger progress.',
  ),
  (
    'cluster-cooldown',
    'AIOPS_CLUSTER_COOLDOWN_SEC',
    'core/deployments/80-core-aiops-agent.yaml',
    'Prevents repeated cluster-scope suggestions from re-firing too aggressively.',
  ),
  (
    'forwarder-local',
    'FORWARDER_FILTER_DROP_LOCAL_DENY',
    'edge/edge_forwarder/deployments/30-edge-forwarder.yaml',
    'Edge forwarding remains lossless for canonical LCORE fact replay.',
  ),
  (
    'forwarder-broadcast',
    'FORWARDER_FILTER_DROP_BROADCAST_MDNS_NBNS',
    'edge/edge_forwarder/deployments/30-edge-forwarder.yaml',
    'Legacy office-traffic suppression switch; no longer defines the active LCORE scenario.',
  ),
]


def load_control_loop_snapshot(settings: Settings) -> dict[str, Any]:
  alerts_dir = settings.artifact_root / 'alerts'
  suggestions_dir = settings.artifact_root / 'aiops'
  observability_dir = settings.artifact_root / 'observability'

  alerts = _load_recent_jsonl(alerts_dir, 'alerts', max_files=8)
  suggestions = _load_recent_jsonl(suggestions_dir, 'suggestions', max_files=8)
  live_report = _load_latest_live_report(observability_dir)

  latest_alert = _latest_by_ts(alerts, 'alert_ts')
  latest_suggestion = _latest_by_ts(suggestions, 'suggestion_ts')
  reference_dt = _max_dt(
    _parse_ts(_get_text(latest_alert, 'alert_ts')),
    _parse_ts(_get_text(latest_suggestion, 'suggestion_ts')),
    _parse_ts(_get_text(live_report, 'report_ts')),
    datetime.now(timezone.utc),
  )
  reference_date = reference_dt.date()

  alerts_for_day = _filter_by_date(alerts, 'alert_ts', reference_date)
  suggestions_for_day = _filter_by_date(suggestions, 'suggestion_ts', reference_date)
  alerts_by_id = {
    alert.get('alert_id'): alert
    for alert in alerts
    if isinstance(alert.get('alert_id'), str)
  }

  strategy_controls = _build_strategy_controls(settings.repo_root)
  control_lookup = {item['label']: item['currentValue'] for item in strategy_controls}

  suggestion_records = _build_suggestion_records(
    suggestions_for_day,
    alerts_by_id,
    control_lookup,
    limit=12,
  )
  if not suggestion_records and latest_suggestion:
    suggestion_records = _build_suggestion_records(
      [latest_suggestion],
      alerts_by_id,
      control_lookup,
      limit=1,
    )

  default_suggestion = suggestion_records[0] if suggestion_records else None

  raw_freshness_sec = _get_number(
    live_report,
    'history_assessment',
    'latest_raw_payload_age_sec',
  )
  history_backlog = _get_bool(
    live_report,
    'history_assessment',
    'history_backlog_suspected',
  )

  return {
    'repo': {
      'branch': _resolve_branch(settings),
      'validation': 'live gateway · control-loop artifacts + deployment config',
    },
    'controlLoop': {
      'latestAlertTs': _get_text(latest_alert, 'alert_ts') or 'n/a',
      'latestSuggestionTs': _get_text(latest_suggestion, 'suggestion_ts') or 'n/a',
      'contextNote': (
        'Derived from live JSONL sinks, the latest control-loop audit, and '
        'deployment env configuration.'
      ),
    },
    'defaultSuggestionId': default_suggestion['id'] if default_suggestion else '',
    'overviewMetrics': [
      {
        'id': 'raw-freshness',
        'label': 'Raw Freshness',
        'value': _format_age(raw_freshness_sec),
        'hint': 'Latest control-loop audit reports raw payload recency from the live topic path.',
        'state': _freshness_state(raw_freshness_sec),
      },
      {
        'id': 'alert-latest',
        'label': 'Latest Alert',
        'value': _format_clock(_get_text(latest_alert, 'alert_ts')),
        'hint': 'Latest alert_ts observed from the local alerts sink.',
        'state': 'ok' if latest_alert else 'watch',
      },
      {
        'id': 'suggestion-latest',
        'label': 'Latest Suggestion',
        'value': _format_clock(_get_text(latest_suggestion, 'suggestion_ts')),
        'hint': 'Latest suggestion_ts observed from the local AIOps sink.',
        'state': 'ok' if latest_suggestion else 'watch',
      },
      {
        'id': 'backlog',
        'label': 'History Backlog',
        'value': _format_bool(history_backlog),
        'hint': 'This comes from the latest live control-loop audit rather than a synthetic UI heartbeat.',
        'state': 'neutral' if history_backlog is None else ('watch' if history_backlog else 'ok'),
      },
      {
        'id': 'current-day-volume',
        'label': 'Current Day Volume',
        'value': f"{len(alerts_for_day)} / {len(suggestions_for_day)}",
        'hint': f"alerts / suggestions observed for {reference_date.isoformat()} UTC.",
        'state': 'ok' if suggestions_for_day else 'watch',
      },
      {
        'id': 'closure',
        'label': 'Closed Loop',
        'value': _closure_label(suggestion_records),
        'hint': 'Execution feedback remains visible as the next control boundary.',
        'state': 'watch',
      },
    ],
    'cadence': _build_cadence(alerts_for_day, suggestions_for_day),
    'evidenceCoverage': _build_evidence_coverage(alerts_for_day, live_report),
    'stageNodes': _build_stage_nodes(
      latest_alert=latest_alert,
      latest_suggestion=latest_suggestion,
      alerts_for_day=alerts_for_day,
      suggestions_for_day=suggestions_for_day,
      raw_freshness_sec=raw_freshness_sec,
      history_backlog=history_backlog,
      control_lookup=control_lookup,
    ),
    'stageLinks': [
      {'id': 'l1', 'source': 'lcore-source', 'target': 'ingest', 'state': 'active'},
      {'id': 'l2', 'source': 'ingest', 'target': 'forwarder', 'state': 'active'},
      {'id': 'l3', 'source': 'forwarder', 'target': 'raw-topic', 'state': 'active'},
      {'id': 'l4', 'source': 'raw-topic', 'target': 'correlator', 'state': 'active'},
      {'id': 'l5', 'source': 'correlator', 'target': 'alerts-topic', 'state': 'active'},
      {'id': 'l6', 'source': 'alerts-topic', 'target': 'alerts-sink', 'state': 'steady'},
      {'id': 'l7', 'source': 'alerts-topic', 'target': 'clickhouse', 'state': 'steady'},
      {'id': 'l8', 'source': 'alerts-topic', 'target': 'aiops-agent', 'state': 'active'},
      {'id': 'l9', 'source': 'aiops-agent', 'target': 'suggestions-topic', 'state': 'active'},
      {'id': 'l10', 'source': 'suggestions-topic', 'target': 'remediation', 'state': 'planned'},
    ],
    'timeline': default_suggestion.get('timeline') if default_suggestion else _build_timeline(None),
    'clusterWatch': _build_cluster_watch(alerts, control_lookup),
    'suggestions': suggestion_records,
    'strategyControls': strategy_controls,
    'feed': _build_feed(alerts, suggestions, live_report),
    'topologyNotes': STATIC_TOPOLOGY_NOTES,
  }


def _build_strategy_controls(repo_root: Path) -> list[dict[str, str]]:
  source_map = {
    'core/deployments/40-core-correlator.yaml': repo_root
    / 'core'
    / 'deployments'
    / '40-core-correlator.yaml',
    'core/deployments/80-core-aiops-agent.yaml': repo_root
    / 'core'
    / 'deployments'
    / '80-core-aiops-agent.yaml',
    'edge/edge_forwarder/deployments/30-edge-forwarder.yaml': repo_root
    / 'edge'
    / 'edge_forwarder'
    / 'deployments'
    / '30-edge-forwarder.yaml',
  }
  env_by_file = {
    relative_path: _load_env_map(path)
    for relative_path, path in source_map.items()
  }

  controls: list[dict[str, str]] = []
  for control_id, label, relative_path, detail in CONTROL_SPECS:
    controls.append(
      {
        'id': control_id,
        'label': label,
        'currentValue': env_by_file.get(relative_path, {}).get(label, 'unknown'),
        'source': relative_path,
        'detail': detail,
      },
    )
  return controls


def _build_cadence(
  alerts: list[dict[str, Any]],
  suggestions: list[dict[str, Any]],
) -> dict[str, list[Any]]:
  alert_counts = Counter(_slot_key(item.get('alert_ts')) for item in alerts)
  suggestion_counts = Counter(_slot_key(item.get('suggestion_ts')) for item in suggestions)
  labels = sorted(
    label
    for label in set(alert_counts) | set(suggestion_counts)
    if label != 'n/a'
  )
  labels = labels[-12:] or ['n/a']
  return {
    'labels': labels,
    'alerts': [alert_counts.get(label, 0) for label in labels],
    'suggestions': [suggestion_counts.get(label, 0) for label in labels],
  }


def _build_evidence_coverage(
  alerts: list[dict[str, Any]],
  live_report: dict[str, Any],
) -> dict[str, list[Any]]:
  labels = ['Topology', 'Device', 'Change']
  if alerts:
    values = [
      round(_presence_rate(alerts, 'topology_context') * 100),
      round(_presence_rate(alerts, 'device_profile') * 100),
      round(_presence_rate(alerts, 'change_context') * 100),
    ]
  else:
    presence_rates = _get_dict(live_report, 'recent_alert_presence', 'presence_rates')
    values = [
      round(float(presence_rates.get('topology_context', 0)) * 100),
      round(float(presence_rates.get('device_profile', 0)) * 100),
      round(float(presence_rates.get('change_context', 0)) * 100),
    ]
  return {'labels': labels, 'values': values}


def _build_stage_nodes(
  *,
  latest_alert: dict[str, Any] | None,
  latest_suggestion: dict[str, Any] | None,
  alerts_for_day: list[dict[str, Any]],
  suggestions_for_day: list[dict[str, Any]],
  raw_freshness_sec: int | None,
  history_backlog: bool | None,
  control_lookup: dict[str, str],
) -> list[dict[str, Any]]:
  return [
    {
      'id': 'lcore-source',
      'title': 'LCORE-D Edge Source',
      'subtitle': 'core-router telemetry + fault labels',
      'status': 'flowing' if raw_freshness_sec is not None else 'steady',
      'x': 0,
      'y': 90,
      'metrics': [
        {'label': 'mode', 'value': 'lcore stream'},
        {'label': 'signal', 'value': 'fault telemetry'},
      ],
    },
    {
      'id': 'ingest',
      'title': 'edge/lcore-streamer',
      'subtitle': 'canonicalize, checkpoint, stream',
      'status': 'watch' if history_backlog else 'flowing',
      'x': 240,
      'y': 90,
      'metrics': [
        {'label': 'parsed', 'value': 'control-loop audit'},
        {'label': 'backlog', 'value': _format_bool(history_backlog)},
      ],
    },
    {
      'id': 'forwarder',
      'title': 'edge-forwarder',
      'subtitle': 'canonical LCORE facts -> Kafka raw',
      'status': 'steady',
      'x': 520,
      'y': 90,
      'metrics': [
        {
          'label': 'lossless',
          'value': control_lookup.get('FORWARDER_FILTER_DROP_LOCAL_DENY', 'unknown'),
        },
        {
          'label': 'legacy filters',
          'value': control_lookup.get(
            'FORWARDER_FILTER_DROP_BROADCAST_MDNS_NBNS',
            'unknown',
          ),
        },
      ],
    },
    {
      'id': 'raw-topic',
      'title': 'netops.facts.raw.v1',
      'subtitle': 'real-time fact stream',
      'status': 'flowing' if _freshness_state(raw_freshness_sec) == 'ok' else 'watch',
      'x': 800,
      'y': 90,
      'metrics': [
        {'label': 'freshness', 'value': _format_age(raw_freshness_sec)},
        {'label': 'kind', 'value': 'raw topic'},
      ],
    },
    {
      'id': 'correlator',
      'title': 'core-correlator',
      'subtitle': 'quality gate + annotated fault rule',
      'status': 'flowing' if alerts_for_day else 'steady',
      'x': 1060,
      'y': 90,
      'metrics': [
        {
          'label': 'active rule',
          'value': 'annotated_fault_v1',
        },
        {
          'label': 'cooldown',
          'value': f"{control_lookup.get('RULE_ALERT_COOLDOWN_SEC', 'unknown')}s",
        },
      ],
    },
    {
      'id': 'alerts-topic',
      'title': 'netops.alerts.v1',
      'subtitle': 'alert bus',
      'status': 'flowing' if latest_alert else 'steady',
      'x': 1320,
      'y': 90,
      'metrics': [
        {'label': 'latest', 'value': _format_clock(_get_text(latest_alert, 'alert_ts'))},
        {'label': 'current day', 'value': f"{len(alerts_for_day)} alerts"},
      ],
    },
    {
      'id': 'alerts-sink',
      'title': 'core-alerts-sink',
      'subtitle': 'hourly JSONL audit',
      'status': 'steady',
      'x': 1060,
      'y': 260,
      'metrics': [
        {'label': 'bucket', 'value': 'alert_ts'},
        {'label': 'latest file', 'value': _latest_file_name(latest_alert, 'alert_ts')},
      ],
    },
    {
      'id': 'clickhouse',
      'title': 'ClickHouse',
      'subtitle': 'netops.alerts hot query store',
      'status': 'steady',
      'x': 1320,
      'y': 260,
      'metrics': [
        {'label': 'query role', 'value': 'history + context'},
        {'label': 'table', 'value': 'netops.alerts'},
      ],
    },
    {
      'id': 'aiops-agent',
      'title': 'core-aiops-agent',
      'subtitle': 'topology subgraph + bounded reasoning',
      'status': 'flowing' if latest_suggestion else 'steady',
      'x': 1580,
      'y': 90,
      'metrics': [
        {'label': 'scope', 'value': _closure_label(suggestions_for_day)},
        {
          'label': 'cluster gate',
          'value': (
            f"{control_lookup.get('AIOPS_CLUSTER_WINDOW_SEC', 'unknown')} / "
            f"{control_lookup.get('AIOPS_CLUSTER_MIN_ALERTS', 'unknown')} / "
            f"{control_lookup.get('AIOPS_CLUSTER_COOLDOWN_SEC', 'unknown')}"
          ),
        },
      ],
    },
    {
      'id': 'suggestions-topic',
      'title': 'netops.aiops.suggestions.v1',
      'subtitle': 'structured localization guidance',
      'status': 'flowing' if latest_suggestion else 'steady',
      'x': 1840,
      'y': 90,
      'metrics': [
        {'label': 'LLM gate', 'value': _llm_gate_label(latest_suggestion)},
        {'label': 'current day', 'value': f"{len(suggestions_for_day)} suggestions"},
      ],
    },
    {
      'id': 'remediation',
      'title': 'Remediation Loop',
      'subtitle': 'approval / execution / feedback',
      'status': 'planned',
      'x': 1840,
      'y': 260,
      'metrics': [
        {'label': 'status', 'value': 'reserved control point'},
        {'label': 'feedback', 'value': 'not yet wired'},
      ],
    },
  ]


def _build_timeline(
  suggestion: dict[str, Any] | None,
) -> list[dict[str, Any]]:
  if not suggestion:
    return [
      {
        'id': 'step-empty',
        'stageId': 'suggestions-topic',
        'stamp': 'waiting',
        'title': 'No live suggestion available',
        'detail': 'The gateway could not derive a suggestion record from the local sinks yet.',
      },
    ]
  return suggestion.get('timeline') or []


def _build_cluster_watch(
  alerts: list[dict[str, Any]],
  control_lookup: dict[str, str],
) -> list[dict[str, Any]]:
  cluster_window_sec = _safe_int(control_lookup.get('AIOPS_CLUSTER_WINDOW_SEC'), 600)
  cluster_min_alerts = _safe_int(control_lookup.get('AIOPS_CLUSTER_MIN_ALERTS'), 3)
  grouped: dict[tuple[str, str, str, str], list[datetime]] = defaultdict(list)

  for alert in alerts:
    alert_dt = _parse_ts(_get_text(alert, 'alert_ts'))
    if not alert_dt:
      continue
    rule_id = _get_text(alert, 'rule_id') or 'unknown'
    severity = _get_text(alert, 'severity') or 'warning'
    service = (
      _get_nested_text(alert, ('topology_context', 'service'))
      or _get_nested_text(alert, ('event_excerpt', 'service'))
      or 'unknown'
    )
    device = (
      _get_nested_text(alert, ('dimensions', 'src_device_key'))
      or _get_nested_text(alert, ('topology_context', 'src_device_key'))
      or _get_nested_text(alert, ('event_excerpt', 'src_device_key'))
      or 'unknown'
    )
    grouped[(rule_id, severity, service, device)].append(alert_dt)

  items: list[dict[str, Any]] = []
  for (rule_id, severity, service, device), timestamps in grouped.items():
    timestamps.sort()
    latest_ts = timestamps[-1]
    window_start = latest_ts - timedelta(seconds=cluster_window_sec)
    progress = sum(1 for ts in timestamps if ts >= window_start)
    remaining = max(0, cluster_min_alerts - progress)
    items.append(
      {
        'key': f'{rule_id} · {service} · {device}',
        'service': service,
        'device': device,
        'progress': min(progress, cluster_min_alerts),
        'target': cluster_min_alerts,
        'windowSec': cluster_window_sec,
        'note': (
          f'{progress} matching {severity} alert(s) seen in the last '
          f'{cluster_window_sec}s.'
          if remaining <= 0
          else f'{progress} matching {severity} alert(s) seen in the last '
          f'{cluster_window_sec}s; {remaining} more needed for cluster trigger.'
        ),
        '_latestTs': latest_ts,
      },
    )

  items.sort(
    key=lambda item: (item['progress'], item['_latestTs']),
    reverse=True,
  )
  trimmed = items[:3]
  for item in trimmed:
    item.pop('_latestTs', None)
  return trimmed


def _build_suggestion_story_timeline(
  suggestion: dict[str, Any],
  alert: dict[str, Any],
  control_lookup: dict[str, str],
  *,
  service: str,
  src_device_key: str,
) -> list[dict[str, Any]]:
  context = suggestion.get('context', {}) if isinstance(suggestion.get('context'), dict) else {}
  event_excerpt = alert.get('event_excerpt', {}) if isinstance(alert, dict) else {}
  metrics = alert.get('metrics', {}) if isinstance(alert, dict) else {}
  event_ts = _parse_ts(_get_text(event_excerpt, 'event_ts'))
  alert_ts = _parse_ts(_get_text(alert, 'alert_ts'))
  suggestion_ts = _parse_ts(_get_text(suggestion, 'suggestion_ts'))
  cluster_first_alert_ts = _parse_ts(_get_text(context, 'cluster_first_alert_ts'))
  cluster_last_alert_ts = _parse_ts(_get_text(context, 'cluster_last_alert_ts'))
  cluster_size = _safe_int(context.get('cluster_size'), 1)
  cluster_window_sec = _safe_int(
    context.get('cluster_window_sec'),
    _safe_int(control_lookup.get('AIOPS_CLUSTER_WINDOW_SEC'), 600),
  )

  evidence_blocks = [
    block
    for block, value in (
      ('topology_context', alert.get('topology_context') if alert else None),
      ('device_profile', alert.get('device_profile') if alert else None),
      ('change_context', alert.get('change_context') if alert else None),
    )
    if value
  ]

  timeline = [
    {
      'id': 'step-edge',
      'stageId': 'ingest',
      'stamp': _format_iso(event_ts),
      'title': 'LCORE telemetry fact observed',
      'detail': (
        f"LCORE {event_excerpt.get('type', 'telemetry')} / "
        f"{event_excerpt.get('subtype', 'monitoring')} {event_excerpt.get('action', 'observe')} "
        f"for device={src_device_key} scenario={_alert_scenario(alert)} entered the raw path."
      ),
    },
    {
      'id': 'step-correlation',
      'stageId': 'correlator',
      'stamp': _format_iso(alert_ts),
      'title': 'Deterministic fault rule satisfied',
      'detail': (
        f"{_get_text(alert, 'rule_id') or 'rule'} accepted label="
        f"{metrics.get('label_value', 'n/a')} scenario={_alert_scenario(alert)} "
        'without model participation.'
      ),
      'durationMs': _duration_ms(event_ts, alert_ts),
    },
    {
      'id': 'step-enrichment',
      'stageId': 'alerts-topic',
      'stamp': _format_iso(alert_ts),
      'title': 'Alert enrichment attached topology evidence',
      'detail': (
        'Current alert payload carried '
        + (', '.join(evidence_blocks) if evidence_blocks else 'no enriched evidence blocks')
        + '.'
      ),
    },
  ]

  if suggestion.get('suggestion_scope') == 'cluster' or cluster_size > 1:
    timeline.append(
      {
        'id': 'step-cluster',
        'stageId': 'cluster-window',
        'stamp': _format_iso(cluster_last_alert_ts or cluster_first_alert_ts),
        'title': 'Cluster gate reached same-key window',
        'detail': (
          f'Cluster aggregation observed {cluster_size} alert(s) inside the '
          f'{cluster_window_sec}s gate before suggestion emission.'
        ),
        'durationMs': _duration_ms(cluster_first_alert_ts, cluster_last_alert_ts),
      },
    )

  timeline.extend(
    [
      {
        'id': 'step-aiops',
        'stageId': 'suggestions-topic',
        'stamp': _format_iso(suggestion_ts),
        'title': f"AIOps {(_get_text(suggestion, 'suggestion_scope') or 'alert')}-scope guidance emitted",
        'detail': (
          f"Gate={_llm_gate_label(suggestion)}; provider={_get_text(context, 'provider') or 'template'} returned "
          f"{len(_string_list(suggestion.get('hypotheses')))} hypotheses and "
          f"{len(_string_list(suggestion.get('recommended_actions')))} recommended actions."
        ),
        'durationMs': _duration_ms(
          cluster_last_alert_ts
          if (_get_text(suggestion, 'suggestion_scope') == 'cluster' and cluster_last_alert_ts)
          else alert_ts,
          suggestion_ts,
        ),
      },
      {
        'id': 'step-control',
        'stageId': 'remediation',
        'stamp': 'control point',
        'title': 'Remediation loop still reserved',
        'detail': (
          'Execution feedback stays visible as the next operator surface rather '
          'than being faked as a live stage.'
        ),
      },
    ],
  )
  return timeline


def _build_stage_telemetry(
  suggestion: dict[str, Any],
  alert: dict[str, Any],
  control_lookup: dict[str, str],
  *,
  service: str,
  src_device_key: str,
) -> list[dict[str, Any]]:
  context = suggestion.get('context', {}) if isinstance(suggestion.get('context'), dict) else {}
  event_excerpt = alert.get('event_excerpt', {}) if isinstance(alert, dict) else {}
  event_ts = _parse_ts(_get_text(event_excerpt, 'event_ts'))
  alert_ts = _parse_ts(_get_text(alert, 'alert_ts'))
  suggestion_ts = _parse_ts(_get_text(suggestion, 'suggestion_ts'))
  cluster_first_alert_ts = _parse_ts(_get_text(context, 'cluster_first_alert_ts'))
  cluster_last_alert_ts = _parse_ts(_get_text(context, 'cluster_last_alert_ts'))
  cluster_size = _safe_int(context.get('cluster_size'), 1)
  cluster_window_sec = _safe_int(
    context.get('cluster_window_sec'),
    _safe_int(control_lookup.get('AIOPS_CLUSTER_WINDOW_SEC'), 600),
  )
  cluster_min_alerts = _safe_int(control_lookup.get('AIOPS_CLUSTER_MIN_ALERTS'), 3)
  scope = 'cluster' if _get_text(suggestion, 'suggestion_scope') == 'cluster' else 'alert'

  return [
    {
      'stageId': 'lcore-source',
      'mode': 'status',
      'state': 'active',
      'label': 'source',
      'value': 'lcore edge',
      'endedAt': _format_iso(event_ts),
    },
    {
      'stageId': 'ingest',
      'mode': 'timestamp',
      'state': 'complete',
      'label': 'parsed',
      'endedAt': _format_iso(event_ts),
    },
    {
      'stageId': 'forwarder',
      'mode': 'status',
      'state': 'complete',
      'label': 'handoff',
      'value': 'edge -> raw',
      'endedAt': _format_iso(event_ts),
    },
    {
      'stageId': 'raw-topic',
      'mode': 'timestamp',
      'state': 'complete',
      'label': 'observed',
      'endedAt': _format_iso(event_ts),
    },
    {
      'stageId': 'correlator',
      'mode': 'duration',
      'state': 'complete',
      'label': 'edge -> alert',
      'startedAt': _format_iso(event_ts),
      'endedAt': _format_iso(alert_ts),
      'durationMs': _duration_ms(event_ts, alert_ts),
    },
    {
      'stageId': 'alerts-topic',
      'mode': 'timestamp',
      'state': 'complete',
      'label': 'emitted',
      'endedAt': _format_iso(alert_ts),
    },
    {
      'stageId': 'cluster-window',
      'mode': 'gate',
      'state': 'complete' if scope == 'cluster' or cluster_size >= cluster_min_alerts else 'watch',
      'label': 'gate',
      'value': f'{cluster_size}/{cluster_min_alerts} in {cluster_window_sec}s',
      'startedAt': _format_iso(cluster_first_alert_ts),
      'endedAt': _format_iso(cluster_last_alert_ts),
      'durationMs': _duration_ms(cluster_first_alert_ts, cluster_last_alert_ts),
    },
    {
      'stageId': 'aiops-agent',
      'mode': 'duration',
      'state': 'complete',
      'label': 'alert -> suggestion',
      'startedAt': _format_iso(
        cluster_last_alert_ts
        if scope == 'cluster' and cluster_last_alert_ts
        else alert_ts
      ),
      'endedAt': _format_iso(suggestion_ts),
      'durationMs': _duration_ms(
        cluster_last_alert_ts
        if scope == 'cluster' and cluster_last_alert_ts
        else alert_ts,
        suggestion_ts,
      ),
    },
    {
      'stageId': 'suggestions-topic',
      'mode': 'timestamp',
      'state': 'complete',
      'label': 'published',
      'endedAt': _format_iso(suggestion_ts),
    },
    {
      'stageId': 'remediation',
      'mode': 'reserved',
      'state': 'planned',
      'label': 'next',
      'value': f'manual boundary · {service}/{src_device_key}',
    },
  ]


def _normalize_projection_basis(value: Any) -> dict[str, list[dict[str, str]]]:
  if not isinstance(value, dict):
    return {}
  normalized: dict[str, list[dict[str, str]]] = {}
  for key, raw_entries in value.items():
    if not isinstance(raw_entries, list):
      continue
    entries: list[dict[str, str]] = []
    for raw_entry in raw_entries:
      if not isinstance(raw_entry, dict):
        continue
      entry = {
        'label': _get_text(raw_entry, 'label') or 'source',
        'section': _get_text(raw_entry, 'section') or 'evidence',
        'field': _get_text(raw_entry, 'field') or 'field',
        'value': _get_text(raw_entry, 'value') or 'n/a',
        'reason': _get_text(raw_entry, 'reason') or 'basis',
      }
      entries.append(entry)
    if entries:
      normalized[str(key)] = entries
  return normalized


def _alert_scenario(alert: dict[str, Any]) -> str:
  return (
    _get_nested_text(alert, ('dimensions', 'fault_scenario'))
    or _get_nested_text(alert, ('metrics', 'label_value'))
    or 'unknown'
  )


def _llm_gate_label(suggestion: dict[str, Any] | None) -> str:
  if not suggestion:
    return 'n/a'
  evidence_bundle = (
    suggestion.get('evidence_bundle', {})
    if isinstance(suggestion.get('evidence_bundle'), dict)
    else {}
  )
  subgraph = (
    evidence_bundle.get('topology_subgraph')
    if isinstance(evidence_bundle.get('topology_subgraph'), dict)
    else {}
  )
  gate = (
    subgraph.get('llm_invocation_gate')
    if isinstance(subgraph.get('llm_invocation_gate'), dict)
    else {}
  )
  if gate:
    tier = _get_text(gate, 'budget_tier') or 'template_only'
    decision = 'invoke' if gate.get('should_invoke_llm') else 'skip'
    return f'{decision} · {tier}'
  context = suggestion.get('context', {}) if isinstance(suggestion.get('context'), dict) else {}
  return _get_text(context, 'provider') or 'template'


def _fallback_projection_basis(
  evidence_bundle: dict[str, Any],
  *,
  service: str,
  src_device_key: str,
  rule_id: str,
  provider: str,
) -> dict[str, list[dict[str, str]]]:
  topology = evidence_bundle.get('topology_context', {}) if isinstance(evidence_bundle.get('topology_context'), dict) else {}
  device = evidence_bundle.get('device_context', {}) if isinstance(evidence_bundle.get('device_context'), dict) else {}
  change = evidence_bundle.get('change_context', {}) if isinstance(evidence_bundle.get('change_context'), dict) else {}
  history = evidence_bundle.get('historical_context', {}) if isinstance(evidence_bundle.get('historical_context'), dict) else {}
  path_signature = (
    _get_text(topology, 'path_signature')
    or f"{_get_text(topology, 'srcintf') or 'unknown'}->{_get_text(topology, 'dstintf') or 'unknown'}"
  )
  return {
    'projector-trigger': [
      {
        'label': 'rule',
        'section': 'rule_context',
        'field': 'rule_id',
        'value': rule_id,
        'reason': 'Current suggestion is anchored to the deterministic alert rule.',
      },
      {
        'label': 'service',
        'section': 'topology_context',
        'field': 'service',
        'value': service,
        'reason': 'Current suggestion stays on the same service slice.',
      },
    ],
    'projector-aggregate': [
      {
        'label': 'recent similar',
        'section': 'historical_context',
        'field': 'recent_similar_1h',
        'value': str(history.get('recent_similar_1h', '0')),
        'reason': 'Historical recurrence is the closest currently mounted context signal.',
      },
    ],
    'projector-path': [
      {
        'label': 'path',
        'section': 'topology_context',
        'field': 'path_signature',
        'value': path_signature,
        'reason': 'Path evidence comes from the current alert enrichment block.',
      },
    ],
    'projector-device': [
      {
        'label': 'device',
        'section': 'device_context',
        'field': 'src_device_key',
        'value': src_device_key,
        'reason': 'Device identity anchors the current suggestion scope.',
      },
      {
        'label': 'change',
        'section': 'change_context',
        'field': 'change_refs',
        'value': ', '.join(_string_list(change.get('change_refs'))) or 'none',
        'reason': 'Change markers are included when the alert carried them.',
      },
    ],
    'projector-inference': [
      {
        'label': 'provider',
        'section': 'context',
        'field': 'provider',
        'value': provider,
        'reason': 'Inference mode is attached directly to the suggestion context.',
      },
    ],
    'projector-action': [
      {
        'label': 'service tuple',
        'section': 'topology_context',
        'field': 'srcip,dstip,service',
        'value': f"{_get_text(topology, 'srcip') or 'n/a'} -> {_get_text(topology, 'dstip') or 'n/a'} / {service}",
        'reason': 'Action view should remain tied to the tuple observed by the alert.',
      },
    ],
  }


def _build_suggestion_records(
  suggestions: list[dict[str, Any]],
  alerts_by_id: dict[str, dict[str, Any]],
  control_lookup: dict[str, str],
  *,
  limit: int | None = None,
) -> list[dict[str, Any]]:
  records: list[dict[str, Any]] = []
  sorted_suggestions = sorted(
    suggestions,
    key=lambda item: _parse_ts(_get_text(item, 'suggestion_ts')) or datetime.min.replace(tzinfo=timezone.utc),
    reverse=True,
  )
  if limit is not None:
    sorted_suggestions = sorted_suggestions[:limit]

  for suggestion in sorted_suggestions:
    context = suggestion.get('context', {}) if isinstance(suggestion.get('context'), dict) else {}
    evidence_bundle = (
      suggestion.get('evidence_bundle', {})
      if isinstance(suggestion.get('evidence_bundle'), dict)
      else {}
    )
    alert = alerts_by_id.get(_get_text(suggestion, 'alert_id') or '', {})

    service = (
      _get_text(context, 'service')
      or _get_nested_text(evidence_bundle, ('topology_context', 'service'))
      or _get_nested_text(alert, ('topology_context', 'service'))
      or 'unknown'
    )
    src_device_key = (
      _get_text(context, 'src_device_key')
      or _get_nested_text(evidence_bundle, ('topology_context', 'src_device_key'))
      or _get_nested_text(alert, ('dimensions', 'src_device_key'))
      or 'unknown'
    )
    confidence = float(suggestion.get('confidence', 0) or 0)
    timeline = _build_suggestion_story_timeline(
      suggestion,
      alert,
      control_lookup,
      service=service,
      src_device_key=src_device_key,
    )
    stage_telemetry = _build_stage_telemetry(
      suggestion,
      alert,
      control_lookup,
      service=service,
      src_device_key=src_device_key,
    )
    projection_basis = _normalize_projection_basis(
      suggestion.get('projection_basis')
      or _get_dict(suggestion, 'inference', 'raw_response', 'projection_basis')
    )
    if not projection_basis:
      projection_basis = _fallback_projection_basis(
        evidence_bundle,
        service=service,
        src_device_key=src_device_key,
        rule_id=_get_text(suggestion, 'rule_id') or 'unknown',
        provider=_get_text(context, 'provider') or 'template',
      )
    hypothesis_set = _normalize_hypothesis_set(
      suggestion.get('hypothesis_set'),
      suggestion=suggestion,
    )
    runbook_draft = _normalize_runbook_draft(
      suggestion.get('runbook_draft'),
      suggestion=suggestion,
    )
    review_verdict = _normalize_review_verdict(
      suggestion.get('review_verdict'),
      suggestion=suggestion,
      hypothesis_set=hypothesis_set,
    )
    topology_map = _normalize_mapping(evidence_bundle.get('topology_context'))
    subgraph = (
      evidence_bundle.get('topology_subgraph')
      if isinstance(evidence_bundle.get('topology_subgraph'), dict)
      else {}
    )
    gate = (
      subgraph.get('llm_invocation_gate')
      if isinstance(subgraph.get('llm_invocation_gate'), dict)
      else {}
    )
    if subgraph:
      topology_map['selected_node_count'] = str(subgraph.get('selected_node_count') or 0)
      topology_map['noise_node_count'] = str(subgraph.get('noise_node_count') or 0)
      topology_map['root_candidate_nodes'] = ', '.join(_string_list(subgraph.get('root_candidate_nodes'))) or 'none'
      topology_map['symptom_nodes'] = ', '.join(_string_list(subgraph.get('symptom_nodes'))) or 'none'
    if gate:
      topology_map['llm_gate'] = _llm_gate_label(suggestion)
      topology_map['llm_gate_reason'] = _get_text(gate, 'reason') or 'n/a'
    records.append(
      {
        'id': _get_text(suggestion, 'suggestion_id') or _get_text(suggestion, 'alert_id') or 'unknown',
        'alertId': _get_text(suggestion, 'alert_id') or '',
        'suggestionTs': _get_text(suggestion, 'suggestion_ts') or 'n/a',
        'scope': 'cluster' if _get_text(suggestion, 'suggestion_scope') == 'cluster' else 'alert',
        'ruleId': _get_text(suggestion, 'rule_id') or 'unknown',
        'severity': _get_text(suggestion, 'severity') or 'warning',
        'priority': _get_text(suggestion, 'priority') or 'P2',
        'summary': _get_text(suggestion, 'summary') or 'suggestion',
        'context': {
          'service': service,
          'srcDeviceKey': src_device_key,
          'clusterSize': _safe_int(context.get('cluster_size'), 1),
          'clusterWindowSec': _safe_int(context.get('cluster_window_sec'), 0),
          'clusterFirstAlertTs': _get_text(context, 'cluster_first_alert_ts') or 'n/a',
          'clusterLastAlertTs': _get_text(context, 'cluster_last_alert_ts') or 'n/a',
          'clusterSampleAlertIds': _string_list(context.get('cluster_sample_alert_ids')),
          'recentSimilar1h': _safe_int(context.get('recent_similar_1h'), 0),
          'provider': _get_text(context, 'provider') or 'template',
        },
        'evidenceBundle': {
          'topology': topology_map,
          'device': _normalize_mapping(evidence_bundle.get('device_context')),
          'change': _normalize_mapping(evidence_bundle.get('change_context')),
          'historical': _normalize_mapping(evidence_bundle.get('historical_context')),
        },
        'hypotheses': _string_list(suggestion.get('hypotheses')),
        'hypothesisSet': hypothesis_set,
        'recommendedActions': _string_list(suggestion.get('recommended_actions')),
        'runbookDraft': runbook_draft,
        'reviewVerdict': review_verdict,
        'confidence': round(confidence, 2),
        'confidenceLabel': _get_text(suggestion, 'confidence_label') or _confidence_label(confidence),
        'confidenceReason': _get_text(suggestion, 'confidence_reason')
        or 'Confidence is based on current alert evidence and recurrence context.',
        'projectionBasis': projection_basis,
        'timeline': timeline,
        'stageTelemetry': stage_telemetry,
      },
    )
  return records


def _normalize_hypothesis_set(
  value: Any,
  *,
  suggestion: dict[str, Any],
) -> dict[str, Any]:
  if isinstance(value, dict):
    items = []
    for raw_item in value.get('items', []):
      if not isinstance(raw_item, dict):
        continue
      items.append(
        {
          'hypothesisId': _get_text(raw_item, 'hypothesis_id') or 'unknown',
          'rank': _safe_int(raw_item.get('rank'), len(items) + 1),
          'statement': _get_text(raw_item, 'statement') or 'n/a',
          'confidenceScore': float(raw_item.get('confidence_score', 0) or 0),
          'confidenceLabel': _get_text(raw_item, 'confidence_label') or 'low',
          'supportEvidenceRefs': _string_list(raw_item.get('support_evidence_refs')),
          'contradictEvidenceRefs': _string_list(raw_item.get('contradict_evidence_refs')),
          'missingEvidenceRefs': _string_list(raw_item.get('missing_evidence_refs')),
          'nextBestAction': _get_text(raw_item, 'next_best_action') or '',
          'reviewState': _get_text(raw_item, 'review_state') or 'candidate',
        },
      )
    if items:
      summary = value.get('summary', {}) if isinstance(value.get('summary'), dict) else {}
      return {
        'setId': _get_text(value, 'set_id') or 'unknown',
        'primaryHypothesisId': _get_text(value, 'primary_hypothesis_id') or items[0]['hypothesisId'],
        'suggestionScope': 'cluster' if _get_text(value, 'suggestion_scope') == 'cluster' else 'alert',
        'items': items,
        'summary': {
          'totalHypotheses': _safe_int(summary.get('total_hypotheses'), len(items)),
          'directRefCount': _safe_int(summary.get('direct_ref_count'), 0),
          'supportingRefCount': _safe_int(summary.get('supporting_ref_count'), 0),
          'contradictoryRefCount': _safe_int(summary.get('contradictory_ref_count'), 0),
          'missingRefCount': _safe_int(summary.get('missing_ref_count'), 0),
        },
      }

  hypotheses = _string_list(suggestion.get('hypotheses'))
  recommended_actions = _string_list(suggestion.get('recommended_actions'))
  confidence = float(suggestion.get('confidence', 0) or 0)
  items = []
  for index, statement in enumerate(hypotheses[:3]):
    items.append(
      {
        'hypothesisId': f"fallback-h-{index + 1}",
        'rank': index + 1,
        'statement': statement,
        'confidenceScore': round(max(0.35, confidence - (index * 0.08)), 2),
        'confidenceLabel': _get_text(suggestion, 'confidence_label') or _confidence_label(confidence),
        'supportEvidenceRefs': [],
        'contradictEvidenceRefs': [],
        'missingEvidenceRefs': [],
        'nextBestAction': recommended_actions[index] if index < len(recommended_actions) else (recommended_actions[0] if recommended_actions else ''),
        'reviewState': 'candidate',
      },
    )
  return {
    'setId': 'fallback-hypothesis-set',
    'primaryHypothesisId': items[0]['hypothesisId'] if items else '',
    'suggestionScope': 'cluster' if _get_text(suggestion, 'suggestion_scope') == 'cluster' else 'alert',
    'items': items,
    'summary': {
      'totalHypotheses': len(items),
      'directRefCount': 0,
      'supportingRefCount': 0,
      'contradictoryRefCount': 0,
      'missingRefCount': 0,
    },
  }


def _normalize_review_verdict(
  value: Any,
  *,
  suggestion: dict[str, Any],
  hypothesis_set: dict[str, Any],
) -> dict[str, Any]:
  if isinstance(value, dict) and isinstance(value.get('checks'), dict):
    checks = value.get('checks', {})
    return {
      'verdictId': _get_text(value, 'verdict_id') or 'unknown',
      'suggestionScope': 'cluster' if _get_text(value, 'suggestion_scope') == 'cluster' else 'alert',
      'verdictStatus': _get_text(value, 'verdict_status') or 'operator_review',
      'recommendedDisposition': _get_text(value, 'recommended_disposition') or 'project_with_operator_boundary',
      'approvalRequired': _get_bool(value, 'approval_required') is not False,
      'blockingIssues': _string_list(value.get('blocking_issues')),
      'checks': {
        'evidenceSufficiency': _normalize_review_check(checks.get('evidence_sufficiency')),
        'temporalFreshness': _normalize_review_check(checks.get('temporal_freshness')),
        'topologyConsistency': _normalize_review_check(checks.get('topology_consistency')),
        'overreachRisk': _normalize_review_check(checks.get('overreach_risk')),
        'remediationExecutability': _normalize_review_check(checks.get('remediation_executability')),
        'rollbackReadiness': _normalize_review_check(checks.get('rollback_readiness')),
      },
      'reviewSummary': _get_text(value, 'review_summary') or 'review verdict attached',
    }

  return {
    'verdictId': 'fallback-review-verdict',
    'suggestionScope': 'cluster' if _get_text(suggestion, 'suggestion_scope') == 'cluster' else 'alert',
    'verdictStatus': 'operator_review',
    'recommendedDisposition': 'project_with_operator_boundary',
    'approvalRequired': True,
    'blockingIssues': [],
    'checks': {
      'evidenceSufficiency': _normalize_review_check(
        {
          'status': 'bounded',
          'detail': f"hypotheses={len(hypothesis_set.get('items') or [])}",
        },
      ),
      'temporalFreshness': _normalize_review_check({'status': 'fresh', 'detail': 'control-loop snapshot attached'}),
      'topologyConsistency': _normalize_review_check({'status': 'partial', 'detail': 'topology summary attached'}),
      'overreachRisk': _normalize_review_check({'status': 'guarded', 'detail': 'operator boundary retained'}),
      'remediationExecutability': _normalize_review_check({'status': 'draft_only', 'detail': 'template suggestion path'}),
      'rollbackReadiness': _normalize_review_check({'status': 'pending', 'detail': 'no structured rollback verdict in legacy suggestion'}),
    },
    'reviewSummary': _get_text(suggestion, 'confidence_reason') or 'legacy suggestion fallback review',
  }


def _normalize_runbook_draft(
  value: Any,
  *,
  suggestion: dict[str, Any],
) -> dict[str, Any]:
  if isinstance(value, dict):
    applicability = value.get('applicability', {}) if isinstance(value.get('applicability'), dict) else {}
    approval_boundary = value.get('approval_boundary', {}) if isinstance(value.get('approval_boundary'), dict) else {}
    change_summary = value.get('change_summary', {}) if isinstance(value.get('change_summary'), dict) else {}
    return {
      'planId': _get_text(value, 'plan_id') or 'unknown',
      'planScope': 'cluster' if _get_text(value, 'plan_scope') == 'cluster' else 'alert',
      'planStatus': _get_text(value, 'plan_status') or 'draft_ready',
      'title': _get_text(value, 'title') or 'Runbook draft',
      'applicability': {
        'ruleId': _get_text(applicability, 'rule_id') or 'unknown',
        'service': _get_text(applicability, 'service') or 'unknown',
        'pathSignature': _get_text(applicability, 'path_signature') or '',
      },
      'hypothesisRef': _get_text(value, 'hypothesis_ref') or '',
      'hypothesisStatement': _get_text(value, 'hypothesis_statement') or '',
      'prechecks': _string_list(value.get('prechecks')),
      'operatorActions': _string_list(value.get('operator_actions')),
      'boundaries': _string_list(value.get('boundaries')),
      'rollbackGuidance': _string_list(value.get('rollback_guidance')),
      'approvalBoundary': {
        'approvalRequired': _get_bool(approval_boundary, 'approval_required') is True,
        'executionMode': _get_text(approval_boundary, 'execution_mode') or 'human_gated',
        'writePathAllowed': _get_bool(approval_boundary, 'write_path_allowed') is True,
      },
      'evidenceRefs': _string_list(value.get('evidence_refs')),
      'changeSummary': {
        'suspectedChange': _get_bool(change_summary, 'suspected_change') is True,
        'changeRefs': _string_list(change_summary.get('change_refs')),
      },
    }

  context = suggestion.get('context', {}) if isinstance(suggestion.get('context'), dict) else {}
  return {
    'planId': 'fallback-runbook-draft',
    'planScope': 'cluster' if _get_text(suggestion, 'suggestion_scope') == 'cluster' else 'alert',
    'planStatus': 'draft_ready',
    'title': 'Runbook draft',
    'applicability': {
      'ruleId': _get_text(suggestion, 'rule_id') or 'unknown',
      'service': _get_text(context, 'service') or 'unknown',
      'pathSignature': '',
    },
    'hypothesisRef': '',
    'hypothesisStatement': '',
    'prechecks': [],
    'operatorActions': _string_list(suggestion.get('recommended_actions')),
    'boundaries': ['guidance only'],
    'rollbackGuidance': [],
    'approvalBoundary': {
      'approvalRequired': True,
      'executionMode': 'human_gated',
      'writePathAllowed': False,
    },
    'evidenceRefs': [],
    'changeSummary': {
      'suspectedChange': False,
      'changeRefs': [],
    },
  }


def _normalize_review_check(value: Any) -> dict[str, str]:
  if isinstance(value, dict):
    return {
      'status': _get_text(value, 'status') or 'unknown',
      'detail': _get_text(value, 'detail') or 'n/a',
    }
  return {
    'status': 'unknown',
    'detail': 'n/a',
  }


def _build_feed(
  alerts: list[dict[str, Any]],
  suggestions: list[dict[str, Any]],
  live_report: dict[str, Any],
) -> list[dict[str, str]]:
  feed_items: list[tuple[datetime, dict[str, str]]] = []

  raw_sample = _get_dict(live_report, 'kafka_topics', 'raw', 'latest_partition_sample')
  raw_payload = raw_sample.get('payload_summary', {}) if isinstance(raw_sample, dict) else {}
  raw_ts = _parse_ts(
    _get_text(raw_sample, 'payload_ts')
    or _get_text(raw_sample, 'broker_ts')
    or _get_text(live_report, 'report_ts'),
  )
  if raw_ts:
    raw_service = _get_text(raw_payload, 'service') or 'raw'
    raw_device = _get_text(raw_payload, 'src_device_key') or 'unknown'
    feed_items.append(
      (
        raw_ts,
        {
          'id': f"feed-raw-{_format_iso(raw_ts)}-{raw_service}",
          'stamp': _format_iso(raw_ts),
          'kind': 'raw',
          'title': f'Raw sample observed for {raw_service}',
          'detail': (
            f"src_device_key={raw_device} action={_get_text(raw_payload, 'action') or 'event'} "
            'from the latest control-loop audit.'
          ),
          'service': raw_service,
          'device': raw_device,
        },
      ),
    )

  for alert in sorted(
    alerts,
    key=lambda item: _parse_ts(_get_text(item, 'alert_ts')) or datetime.min.replace(tzinfo=timezone.utc),
    reverse=True,
  )[:4]:
    alert_ts = _parse_ts(_get_text(alert, 'alert_ts'))
    if not alert_ts:
      continue
    service = (
      _get_nested_text(alert, ('topology_context', 'service'))
      or _get_nested_text(alert, ('event_excerpt', 'service'))
      or 'unknown'
    )
    device = _get_nested_text(alert, ('dimensions', 'src_device_key')) or 'unknown'
    evidence_flags = []
    if alert.get('topology_context'):
      evidence_flags.append('topology')
    if alert.get('device_profile'):
      evidence_flags.append('device')
    if alert.get('change_context'):
      evidence_flags.append('change')
    feed_items.append(
      (
        alert_ts,
        {
          'id': f"feed-alert-{_get_text(alert, 'alert_id') or service}",
          'stamp': _format_iso(alert_ts),
          'kind': 'alert',
          'title': f"Alert {_get_text(alert, 'rule_id') or 'rule'} for {service}",
          'detail': (
            f"device={device}; evidence="
            + (', '.join(evidence_flags) if evidence_flags else 'none')
          ),
          'service': service,
          'device': device,
          'relatedAlertId': _get_text(alert, 'alert_id') or '',
          'evidence': ', '.join(evidence_flags) if evidence_flags else 'none',
        },
      ),
    )

  for suggestion in sorted(
    suggestions,
    key=lambda item: _parse_ts(_get_text(item, 'suggestion_ts')) or datetime.min.replace(tzinfo=timezone.utc),
    reverse=True,
  )[:4]:
    suggestion_ts = _parse_ts(_get_text(suggestion, 'suggestion_ts'))
    if not suggestion_ts:
      continue
    context = suggestion.get('context', {}) if isinstance(suggestion.get('context'), dict) else {}
    service = _get_text(context, 'service') or 'unknown'
    device = _get_text(context, 'src_device_key') or 'unknown'
    scope = _get_text(suggestion, 'suggestion_scope') or 'alert'
    provider = _get_text(context, 'provider') or 'template'
    action_count = len(_string_list(suggestion.get('recommended_actions')))
    hypothesis_count = len(_string_list(suggestion.get('hypotheses')))
    feed_items.append(
      (
        suggestion_ts,
        {
          'id': f"feed-suggestion-{_get_text(suggestion, 'suggestion_id') or service}",
          'stamp': _format_iso(suggestion_ts),
          'kind': 'suggestion',
          'title': f'{scope}-scope suggestion for {service}',
          'detail': (
            f"provider={provider}; actions={action_count} "
            f"hypotheses={hypothesis_count}"
          ),
          'service': service,
          'device': device,
          'scope': 'cluster' if scope == 'cluster' else 'alert',
          'relatedAlertId': _get_text(suggestion, 'alert_id') or '',
          'relatedSuggestionId': _get_text(suggestion, 'suggestion_id') or '',
          'provider': provider,
          'actionCount': str(action_count),
          'hypothesisCount': str(hypothesis_count),
        },
      ),
    )

  feed_items.sort(key=lambda item: item[0], reverse=True)
  top_items = feed_items[:6]
  if raw_ts and not any(item[1]['kind'] == 'raw' for item in top_items):
    top_items = top_items[:5] + [next(item for item in feed_items if item[1]['kind'] == 'raw')]
  return [item[1] for item in top_items]


def _load_env_map(path: Path) -> dict[str, str]:
  if not path.exists():
    return {}
  document = yaml.safe_load(path.read_text())
  containers = _get_nested_value(document, ('spec', 'template', 'spec', 'containers'))
  if not isinstance(containers, list):
    return {}
  env_map: dict[str, str] = {}
  for container in containers:
    if not isinstance(container, dict):
      continue
    for item in container.get('env', []):
      if not isinstance(item, dict):
        continue
      name = item.get('name')
      if isinstance(name, str):
        env_map[name] = str(item.get('value', ''))
  return env_map


def _load_latest_live_report(observability_dir: Path) -> dict[str, Any]:
  candidates = sorted(observability_dir.glob('live-control-loop-check-*.json'))
  if not candidates:
    candidates = sorted(observability_dir.glob('live-runtime-check-*.json'))
  if not candidates:
    return {}
  try:
    return json.loads(candidates[-1].read_text())
  except json.JSONDecodeError:
    return {}


def _load_recent_jsonl(
  directory: Path,
  prefix: str,
  *,
  max_files: int,
) -> list[dict[str, Any]]:
  entries: list[dict[str, Any]] = []
  for path in sorted(directory.glob(f'{prefix}-*.jsonl'))[-max_files:]:
    try:
      with path.open('rb') as handle:
        handle.seek(0, 2)
        size = handle.tell()
        tail_bytes = min(size, 2 * 1024 * 1024)
        handle.seek(max(0, size - tail_bytes))
        lines = handle.read().decode('utf-8', errors='ignore').splitlines()
        if size > tail_bytes and lines:
          lines = lines[1:]
        for line in lines:
          line = line.strip()
          if not line:
            continue
          payload = json.loads(line)
          if isinstance(payload, dict):
            payload.setdefault('_source_file', path.name)
            entries.append(payload)
    except FileNotFoundError:
      continue
    except json.JSONDecodeError:
      continue
  return entries


def _filter_by_date(
  items: list[dict[str, Any]],
  key: str,
  target_date,
) -> list[dict[str, Any]]:
  return [
    item
    for item in items
    if (_parse_ts(_get_text(item, key)) and _parse_ts(_get_text(item, key)).date() == target_date)
  ]


def _latest_by_ts(items: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
  ranked = sorted(
    items,
    key=lambda item: _parse_ts(_get_text(item, key)) or datetime.min.replace(tzinfo=timezone.utc),
    reverse=True,
  )
  return ranked[0] if ranked else None


def _resolve_branch(settings: Settings) -> str:
  if settings.branch_hint:
    return settings.branch_hint
  head_path = settings.repo_root / '.git' / 'HEAD'
  if not head_path.exists():
    return 'ops-console'
  head_value = head_path.read_text().strip()
  if head_value.startswith('ref: '):
    return head_value.rsplit('/', 1)[-1]
  return head_value[:12]


def _presence_rate(items: list[dict[str, Any]], key: str) -> float:
  if not items:
    return 0.0
  populated = sum(1 for item in items if item.get(key))
  return populated / len(items)


def _format_bool(value: bool | None) -> str:
  if value is None:
    return 'unknown'
  return 'true' if value else 'false'


def _format_age(value: int | None) -> str:
  if value is None:
    return 'n/a'
  return f'{value}s'


def _freshness_state(value: int | None) -> str:
  if value is None:
    return 'neutral'
  if value <= 30:
    return 'ok'
  if value <= 300:
    return 'watch'
  return 'alert'


def _closure_label(suggestions: list[dict[str, Any]]) -> str:
  if any(
    (item.get('scope') == 'cluster') or (item.get('suggestion_scope') == 'cluster')
    for item in suggestions
  ):
    return 'cluster-scope live'
  if suggestions:
    return 'alert-scope live'
  return 'no suggestion yet'


def _latest_file_name(item: dict[str, Any] | None, ts_key: str) -> str:
  if not item:
    return 'n/a'
  return str(item.get('_source_file', f'{ts_key}.jsonl'))


def _slot_key(value: Any) -> str:
  timestamp = _parse_ts(str(value)) if value else None
  return timestamp.strftime('%H:%M') if timestamp else 'n/a'


def _format_clock(value: str | None) -> str:
  timestamp = _parse_ts(value)
  if not timestamp:
    return 'n/a'
  if timestamp.microsecond:
    return timestamp.strftime('%H:%M:%S.%f')[:-3] + ' UTC'
  return timestamp.strftime('%H:%M:%S UTC')


def _format_stamp(value: datetime | None) -> str:
  if not value:
    return 'n/a'
  if value.microsecond:
    return value.strftime('%H:%M:%S.%f')[:-3]
  return value.strftime('%H:%M:%S')


def _format_iso(value: datetime | None) -> str:
  if not value:
    return 'n/a'
  return value.isoformat()


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
  if not start or not end:
    return None
  return max(0, int((end - start).total_seconds() * 1000))


def _normalize_mapping(value: Any) -> dict[str, Any]:
  if not isinstance(value, dict):
    return {}
  normalized: dict[str, Any] = {}
  for key, raw in value.items():
    if isinstance(raw, list):
      normalized[key] = [str(item) for item in raw]
    elif raw is None:
      normalized[key] = None
    elif isinstance(raw, (str, int, float, bool)):
      normalized[key] = raw
    else:
      normalized[key] = str(raw)
  return normalized


def _confidence_label(value: float) -> str:
  if value >= 0.8:
    return 'high'
  if value >= 0.5:
    return 'medium'
  return 'low'


def _string_list(value: Any) -> list[str]:
  if not isinstance(value, list):
    return []
  return [str(item) for item in value]


def _parse_ts(value: str | None) -> datetime | None:
  if not value:
    return None
  try:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
  except ValueError:
    return None
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
  return parsed.astimezone(timezone.utc)


def _safe_int(value: Any, default: int) -> int:
  try:
    return int(value)
  except (TypeError, ValueError):
    return default


def _get_text(item: Any, *path: str) -> str | None:
  value = _get_nested_value(item, path)
  return str(value) if value not in (None, '') else None


def _get_nested_text(item: Any, path: tuple[str, ...]) -> str | None:
  value = _get_nested_value(item, path)
  return str(value) if value not in (None, '') else None


def _get_number(item: Any, *path: str) -> int | None:
  value = _get_nested_value(item, path)
  return int(value) if isinstance(value, (int, float)) else None


def _get_bool(item: Any, *path: str) -> bool | None:
  value = _get_nested_value(item, path)
  return value if isinstance(value, bool) else None


def _get_dict(item: Any, *path: str) -> dict[str, Any]:
  value = _get_nested_value(item, path)
  return value if isinstance(value, dict) else {}


def _get_nested_value(item: Any, path: tuple[str, ...] | list[str]) -> Any:
  current = item
  for key in path:
    if not isinstance(current, dict):
      return None
    current = current.get(key)
  return current


def _max_dt(*items: datetime | None) -> datetime:
  available = [item for item in items if item is not None]
  return max(available) if available else datetime.now(timezone.utc)


def build_control_loop_stream_delta(
  previous: dict[str, Any],
  current: dict[str, Any],
) -> dict[str, Any] | None:
  previous_feed_ids = {
    item.get('id')
    for item in previous.get('feed', [])
    if isinstance(item, dict) and item.get('id')
  }
  current_feed = [
    item for item in current.get('feed', []) if isinstance(item, dict) and item.get('id')
  ]
  new_feed = [item for item in current_feed if item.get('id') not in previous_feed_ids]

  if new_feed:
    primary = new_feed[0]
    delta_kind = primary.get('kind') or 'system'
    if any(
      item.get('kind') == 'suggestion' and item.get('scope') == 'cluster'
      for item in new_feed
    ):
      delta_kind = 'cluster'

    return {
      'id': f"delta-{primary.get('id')}",
      'emittedAt': _format_iso(datetime.now(timezone.utc)),
      'kind': delta_kind,
      'stageIds': _stage_ids_for_feed_items(new_feed),
      'feedIds': [item['id'] for item in new_feed],
      'reason': 'feed',
    }

  previous_cluster = tuple(
    (item.get('key'), item.get('progress'), item.get('target'))
    for item in previous.get('clusterWatch', [])
    if isinstance(item, dict)
  )
  current_cluster = tuple(
    (item.get('key'), item.get('progress'), item.get('target'))
    for item in current.get('clusterWatch', [])
    if isinstance(item, dict)
  )
  if previous_cluster != current_cluster:
    return {
      'id': f"delta-cluster-{_format_iso(datetime.now(timezone.utc))}",
      'emittedAt': _format_iso(datetime.now(timezone.utc)),
      'kind': 'cluster',
      'stageIds': ['cluster-window'],
      'feedIds': [],
      'reason': 'cluster-watch',
    }

  previous_control_loop = (
    _get_nested_value(previous, ('controlLoop', 'latestAlertTs')),
    _get_nested_value(previous, ('controlLoop', 'latestSuggestionTs')),
    _get_nested_value(previous, ('defaultSuggestionId',)),
  )
  current_control_loop = (
    _get_nested_value(current, ('controlLoop', 'latestAlertTs')),
    _get_nested_value(current, ('controlLoop', 'latestSuggestionTs')),
    _get_nested_value(current, ('defaultSuggestionId',)),
  )
  if previous_control_loop != current_control_loop:
    return {
      'id': f"delta-system-{_format_iso(datetime.now(timezone.utc))}",
      'emittedAt': _format_iso(datetime.now(timezone.utc)),
      'kind': 'system',
      'stageIds': [],
      'feedIds': [],
      'reason': 'system',
    }

  return None


def _stage_ids_for_feed_items(items: list[dict[str, Any]]) -> list[str]:
  ordered_ids: list[str] = []
  for item in items:
    for stage_id in _stage_ids_for_feed_item(item):
      if stage_id not in ordered_ids:
        ordered_ids.append(stage_id)
  return ordered_ids


def _stage_ids_for_feed_item(item: dict[str, Any]) -> list[str]:
  kind = item.get('kind')
  if kind == 'raw':
    return ['lcore-source', 'ingest', 'forwarder', 'raw-topic']
  if kind == 'alert':
    return ['correlator', 'alerts-topic', 'cluster-window']
  if kind == 'suggestion':
    if item.get('scope') == 'cluster':
      return ['cluster-window', 'aiops-agent', 'suggestions-topic', 'remediation']
    return ['aiops-agent', 'suggestions-topic', 'remediation']
  return []
