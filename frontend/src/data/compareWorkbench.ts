import baselineOnlyJson from '../../fixtures/compare/eval-baseline-only.json'
import pairedFixtureJson from '../../fixtures/compare/eval-paired-fixture.json'
import type {
  CompareDatasetFilters,
  CompareKpiCard,
  CompareMetricBundle,
  CompareProviderEvaluation,
  CompareSampleUnit,
  CompareWorkbenchDataset,
} from '../types'

const pairedFixture = pairedFixtureJson as CompareWorkbenchDataset
const baselineOnlyFixture = baselineOnlyJson as CompareWorkbenchDataset

export const compareWorkbenchDatasets = [
  pairedFixture,
  baselineOnlyFixture,
] as const satisfies CompareWorkbenchDataset[]

export const compareMetricDefinitions = [
  {
    id: 'explanationCompleteness',
    label: 'Explanation Completeness',
    note: 'Device, service, path, change, and history coverage.',
    higherIsBetter: true,
    format: 'percent',
  },
  {
    id: 'actionability',
    label: 'Actionability',
    note: 'Specificity and directness of the recommended actions.',
    higherIsBetter: true,
    format: 'percent',
  },
  {
    id: 'evidenceBinding',
    label: 'Evidence Binding',
    note: 'Claim coverage mapped back to explicit evidence fields.',
    higherIsBetter: true,
    format: 'percent',
  },
  {
    id: 'stability',
    label: 'Stability',
    note: 'Replay consistency under repeated execution.',
    higherIsBetter: true,
    format: 'percent',
  },
  {
    id: 'hallucinationRate',
    label: 'Hallucination Rate',
    note: 'Unsupported claims over the compared output unit.',
    higherIsBetter: false,
    format: 'percent',
  },
  {
    id: 'latencyMs',
    label: 'Latency',
    note: 'Per-suggestion latency.',
    higherIsBetter: false,
    format: 'ms',
  },
  {
    id: 'estimatedCostUsd',
    label: 'Cost',
    note: 'Estimated per-suggestion inference cost.',
    higherIsBetter: false,
    format: 'usd',
  },
  {
    id: 'failureRate',
    label: 'Failure Rate',
    note: 'Observed provider failure share.',
    higherIsBetter: false,
    format: 'percent',
  },
] as const

type CompareMetricKey = keyof CompareMetricBundle

function normalize(text: string) {
  return text.trim().toLowerCase()
}

function isMetricKey(value: string): value is CompareMetricKey {
  return compareMetricDefinitions.some((item) => item.id === value)
}

export function getCompareDataset(datasetId: string) {
  return (
    compareWorkbenchDatasets.find((dataset) => dataset.id === datasetId) ??
    compareWorkbenchDatasets[0]
  )
}

export function buildDefaultFilters(dataset: CompareWorkbenchDataset): CompareDatasetFilters {
  return {
    replayId: dataset.defaultReplayId,
    providerName: 'all',
    severity: 'all',
    ruleId: 'all',
    service: 'all',
    status: 'all',
    query: '',
  }
}

export function buildFilterOptions(dataset: CompareWorkbenchDataset) {
  const replays = unique(dataset.samples.map((sample) => [sample.replay.replayId, sample.replay.replayLabel]))
  const rules = unique(dataset.samples.map((sample) => [sample.ruleId, sample.ruleId]))
  const severities = unique(dataset.samples.map((sample) => [sample.severity, sample.severity]))
  const services = unique(dataset.samples.map((sample) => [sample.service, sample.service]))
  const providers = unique(
    dataset.samples
      .map((sample) => sample.llm.providerName)
      .filter((providerName) => providerName && providerName !== 'llm-slot')
      .map((providerName) => [providerName, providerName]),
  )

  return {
    replays,
    rules,
    severities,
    services,
    providers,
    statuses: [
      ['all', 'All Rows'],
      ['paired', 'Paired Ready'],
      ['placeholder', 'LLM Placeholder'],
      ['failed', 'LLM Failed'],
    ] as Array<[string, string]>,
  }
}

function unique(entries: Array<[string, string]>) {
  const seen = new Set<string>()
  const items: Array<[string, string]> = []
  for (const [value, label] of entries) {
    if (!value || seen.has(value)) {
      continue
    }
    seen.add(value)
    items.push([value, label])
  }
  return items
}

export function filterCompareSamples(
  dataset: CompareWorkbenchDataset,
  filters: CompareDatasetFilters,
) {
  const query = normalize(filters.query)
  return dataset.samples.filter((sample) => {
    if (filters.replayId !== 'all' && sample.replay.replayId !== filters.replayId) {
      return false
    }
    if (filters.providerName !== 'all' && sample.llm.providerName !== filters.providerName) {
      return false
    }
    if (filters.severity !== 'all' && sample.severity !== filters.severity) {
      return false
    }
    if (filters.ruleId !== 'all' && sample.ruleId !== filters.ruleId) {
      return false
    }
    if (filters.service !== 'all' && sample.service !== filters.service) {
      return false
    }
    if (filters.status === 'paired' && sample.llm.status !== 'ready') {
      return false
    }
    if (filters.status === 'placeholder' && sample.llm.status !== 'placeholder') {
      return false
    }
    if (filters.status === 'failed' && sample.llm.status !== 'failed') {
      return false
    }
    if (!query) {
      return true
    }
    const haystack = [
      sample.bundleId,
      sample.alertId,
      sample.ruleId,
      sample.service,
      sample.device,
      sample.path,
      sample.baseline.providerName,
      sample.llm.providerName,
    ]
      .join(' ')
      .toLowerCase()
    return haystack.includes(query)
  })
}

function metricValue(side: CompareProviderEvaluation, metricKey: CompareMetricKey) {
  return side.metrics[metricKey]
}

function averageMetric(
  samples: CompareSampleUnit[],
  sideKey: 'baseline' | 'llm',
  metricKey: CompareMetricKey,
) {
  const values = samples
    .map((sample) => metricValue(sample[sideKey], metricKey))
    .filter((value): value is number => typeof value === 'number')
  if (values.length === 0) {
    return null
  }
  const total = values.reduce((sum, value) => sum + value, 0)
  return total / values.length
}

function formatMetricDisplay(
  value: number | null,
  format: 'percent' | 'ms' | 'usd',
) {
  if (value === null) {
    return 'pending'
  }
  if (format === 'percent') {
    return `${Math.round(value * 100)}%`
  }
  if (format === 'ms') {
    return `${Math.round(value)} ms`
  }
  return `$${value.toFixed(3)}`
}

function deltaState(
  baseline: number | null,
  llm: number | null,
  higherIsBetter: boolean,
): CompareKpiCard['deltaState'] {
  if (baseline === null || llm === null) {
    return 'pending'
  }
  const delta = llm - baseline
  if (Math.abs(delta) < 0.0001) {
    return 'flat'
  }
  const improved = higherIsBetter ? delta > 0 : delta < 0
  return improved ? 'improved' : 'regressed'
}

function deltaDisplay(
  baseline: number | null,
  llm: number | null,
  format: 'percent' | 'ms' | 'usd',
) {
  if (baseline === null || llm === null) {
    return 'waiting'
  }
  const delta = llm - baseline
  if (format === 'percent') {
    return `${delta > 0 ? '+' : ''}${Math.round(delta * 100)} pts`
  }
  if (format === 'ms') {
    return `${delta > 0 ? '+' : ''}${Math.round(delta)} ms`
  }
  return `${delta > 0 ? '+' : ''}$${delta.toFixed(3)}`
}

export function summarizeCompareKpis(samples: CompareSampleUnit[]): CompareKpiCard[] {
  return compareMetricDefinitions.map((definition) => {
    const ruleValue = averageMetric(samples, 'baseline', definition.id)
    const llmValue = averageMetric(samples, 'llm', definition.id)
    return {
      id: definition.id,
      label: definition.label,
      ruleValue,
      llmValue,
      ruleDisplay: formatMetricDisplay(ruleValue, definition.format),
      llmDisplay: formatMetricDisplay(llmValue, definition.format),
      deltaDisplay: deltaDisplay(ruleValue, llmValue, definition.format),
      deltaState: deltaState(ruleValue, llmValue, definition.higherIsBetter),
      note: definition.note,
    }
  })
}

function groupAverage(
  samples: CompareSampleUnit[],
  key: 'ruleId' | 'severity' | 'service' | 'replay',
  metricKeys: CompareMetricKey[],
) {
  const map = new Map<string, CompareSampleUnit[]>()
  for (const sample of samples) {
    const groupKey =
      key === 'replay' ? sample.replay.replayLabel : sample[key]
    const bucket = map.get(groupKey) ?? []
    bucket.push(sample)
    map.set(groupKey, bucket)
  }

  return Array.from(map.entries()).map(([label, bucket]) => ({
    label,
    baseline: Object.fromEntries(
      metricKeys.map((metricKey) => [metricKey, averageMetric(bucket, 'baseline', metricKey)]),
    ),
    llm: Object.fromEntries(
      metricKeys.map((metricKey) => [metricKey, averageMetric(bucket, 'llm', metricKey)]),
    ),
  }))
}

export function buildRuleGroups(samples: CompareSampleUnit[]) {
  return groupAverage(samples, 'ruleId', [
    'explanationCompleteness',
    'actionability',
    'evidenceBinding',
  ])
}

export function buildSeverityGroups(samples: CompareSampleUnit[]) {
  return groupAverage(samples, 'severity', [
    'stability',
    'auditability',
    'failureRate',
  ])
}

export function buildReplayGroups(samples: CompareSampleUnit[]) {
  return groupAverage(samples, 'replay', [
    'stability',
    'auditability',
    'latencyMs',
    'estimatedCostUsd',
  ])
}

export function buildMetricMatrix(
  samples: CompareSampleUnit[],
  metricKeys: CompareMetricKey[],
) {
  return samples.map((sample) => ({
    id: sample.id,
    label: sample.bundleId,
    baseline: Object.fromEntries(
      metricKeys.map((metricKey) => [metricKey, metricValue(sample.baseline, metricKey)]),
    ),
    llm: Object.fromEntries(
      metricKeys.map((metricKey) => [metricKey, metricValue(sample.llm, metricKey)]),
    ),
  }))
}

export function buildProviderScatter(samples: CompareSampleUnit[]) {
  return samples.flatMap((sample) => [
    {
      id: `${sample.id}-baseline`,
      label: sample.bundleId,
      provider: 'Baseline',
      x: sample.baseline.metrics.evidenceBinding,
      y: sample.baseline.metrics.actionability,
      alertId: sample.alertId,
      ruleId: sample.ruleId,
      state: sample.baseline.status,
    },
    {
      id: `${sample.id}-llm`,
      label: sample.bundleId,
      provider: 'LLM',
      x: sample.llm.metrics.evidenceBinding,
      y: sample.llm.metrics.actionability,
      alertId: sample.alertId,
      ruleId: sample.ruleId,
      state: sample.llm.status,
    },
  ])
}

export function buildMetricDistribution(
  samples: CompareSampleUnit[],
  metricKey: CompareMetricKey,
) {
  return {
    baseline: samples
      .map((sample) => metricValue(sample.baseline, metricKey))
      .filter((value): value is number => typeof value === 'number'),
    llm: samples
      .map((sample) => metricValue(sample.llm, metricKey))
      .filter((value): value is number => typeof value === 'number'),
  }
}

export function buildStatusCounts(samples: CompareSampleUnit[]) {
  return {
    baselineReady: samples.filter((sample) => sample.baseline.status === 'ready').length,
    llmReady: samples.filter((sample) => sample.llm.status === 'ready').length,
    llmPlaceholder: samples.filter((sample) => sample.llm.status === 'placeholder').length,
    llmFailed: samples.filter((sample) => sample.llm.status === 'failed').length,
    hallucinationFlags: samples.filter((sample) => sample.llm.unsupportedClaims.length > 0).length,
  }
}

export function formatCellMetric(
  sample: CompareSampleUnit,
  sideKey: 'baseline' | 'llm',
  metricKey: string,
) {
  if (!isMetricKey(metricKey)) {
    return 'n/a'
  }
  const definition = compareMetricDefinitions.find((item) => item.id === metricKey)
  if (!definition) {
    return 'n/a'
  }
  return formatMetricDisplay(metricValue(sample[sideKey], metricKey), definition.format)
}

export function sortCompareSamples(
  samples: CompareSampleUnit[],
  sortKey: string,
  direction: 'asc' | 'desc',
) {
  const factor = direction === 'asc' ? 1 : -1
  const items = [...samples]
  items.sort((left, right) => {
    const leftValue = getSortValue(left, sortKey)
    const rightValue = getSortValue(right, sortKey)

    if (typeof leftValue === 'number' && typeof rightValue === 'number') {
      return (leftValue - rightValue) * factor
    }
    return String(leftValue).localeCompare(String(rightValue)) * factor
  })
  return items
}

function getSortValue(sample: CompareSampleUnit, sortKey: string) {
  if (sortKey === 'bundleId') {
    return sample.bundleId
  }
  if (sortKey === 'alertId') {
    return sample.alertId
  }
  if (sortKey === 'ruleId') {
    return sample.ruleId
  }
  if (sortKey === 'severity') {
    return sample.severity
  }
  if (sortKey === 'service') {
    return sample.service
  }
  if (sortKey === 'device') {
    return sample.device
  }
  if (sortKey === 'baselineStatus') {
    return sample.baselineStatus
  }
  if (sortKey === 'llmStatus') {
    return sample.llmStatus
  }
  if (sortKey.startsWith('baseline.')) {
    return getNestedMetricValue(sample.baseline.metrics, sortKey.replace('baseline.', ''))
  }
  if (sortKey.startsWith('llm.')) {
    return getNestedMetricValue(sample.llm.metrics, sortKey.replace('llm.', ''))
  }
  if (sortKey === 'replayLabel') {
    return sample.replay.replayLabel
  }
  return sample.bundleId
}

function getNestedMetricValue(
  metrics: CompareMetricBundle,
  metricKey: string,
) {
  if (!isMetricKey(metricKey)) {
    return -1
  }
  return metrics[metricKey] ?? -1
}
