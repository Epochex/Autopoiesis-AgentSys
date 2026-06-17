import type { CompareExportFormat, CompareSampleUnit } from '../types'

function downloadBlob(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

function escapeCsv(value: string | number | boolean | null) {
  if (value === null) {
    return ''
  }
  const text = String(value)
  if (text.includes('"') || text.includes(',') || text.includes('\n')) {
    return `"${text.replaceAll('"', '""')}"`
  }
  return text
}

function sampleRow(sample: CompareSampleUnit) {
  return {
    bundle_id: sample.bundleId,
    alert_id: sample.alertId,
    rule_id: sample.ruleId,
    severity: sample.severity,
    service: sample.service,
    device: sample.device,
    baseline_status: sample.baseline.status,
    llm_status: sample.llm.status,
    baseline_explanation_completeness: sample.baseline.metrics.explanationCompleteness,
    llm_explanation_completeness: sample.llm.metrics.explanationCompleteness,
    baseline_actionability: sample.baseline.metrics.actionability,
    llm_actionability: sample.llm.metrics.actionability,
    baseline_evidence_binding: sample.baseline.metrics.evidenceBinding,
    llm_evidence_binding: sample.llm.metrics.evidenceBinding,
    llm_hallucination_rate: sample.llm.metrics.hallucinationRate,
    baseline_latency_ms: sample.baseline.metrics.latencyMs,
    llm_latency_ms: sample.llm.metrics.latencyMs,
    baseline_cost_usd: sample.baseline.metrics.estimatedCostUsd,
    llm_cost_usd: sample.llm.metrics.estimatedCostUsd,
    baseline_failure_rate: sample.baseline.metrics.failureRate,
    llm_failure_rate: sample.llm.metrics.failureRate,
    replay_id: sample.replay.replayId,
    replay_label: sample.replay.replayLabel,
    replay_run_id: sample.replay.runId,
  }
}

export function exportCompareSamples(
  samples: CompareSampleUnit[],
  datasetLabel: string,
  format: CompareExportFormat,
) {
  const safeDataset = datasetLabel.toLowerCase().replaceAll(/[^a-z0-9]+/g, '-')
  if (format === 'json') {
    downloadBlob(
      `${safeDataset}-compare-samples.json`,
      JSON.stringify(samples, null, 2),
      'application/json',
    )
    return
  }

  const rows = samples.map(sampleRow)
  const headers = Object.keys(rows[0] ?? { bundle_id: '' })
  const csv = [
    headers.join(','),
    ...rows.map((row) =>
      headers
        .map((header) => escapeCsv((row as Record<string, string | number | boolean | null>)[header] ?? null))
        .join(','),
    ),
  ].join('\n')

  downloadBlob(
    `${safeDataset}-compare-samples.csv`,
    csv,
    'text/csv;charset=utf-8',
  )
}

export function exportCompareSampleDetail(sample: CompareSampleUnit) {
  downloadBlob(
    `${sample.bundleId}-detail.json`,
    JSON.stringify(sample, null, 2),
    'application/json',
  )
}
