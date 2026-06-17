import type { ReactNode } from 'react'
import {
  buildMetricDistribution,
  buildReplayGroups,
  buildRuleGroups,
  buildSeverityGroups,
  buildStatusCounts,
} from '../../data/compareWorkbench'
import type {
  CompareMetricBundle,
  CompareProviderEvaluation,
  CompareSampleUnit,
  CompareTabId,
} from '../../types'

interface CompareChartsProps {
  locale: 'en' | 'zh'
  activeTab: CompareTabId
  samples: CompareSampleUnit[]
  selectedSampleId: string
  onInspectSample: (sampleId: string) => void
}

type MetricKey = keyof CompareMetricBundle

function pick(locale: 'en' | 'zh', en: string, zh: string) {
  return locale === 'zh' ? zh : en
}

function percentLabel(locale: 'en' | 'zh', value: number | null) {
  if (value === null) {
    return pick(locale, 'pending', '待接入')
  }
  return `${Math.round(value * 100)}%`
}

function msLabel(locale: 'en' | 'zh', value: number | null) {
  if (value === null) {
    return pick(locale, 'pending', '待接入')
  }
  return `${Math.round(value)} ms`
}

function usdLabel(locale: 'en' | 'zh', value: number | null) {
  if (value === null) {
    return pick(locale, 'pending', '待接入')
  }
  return `$${value.toFixed(3)}`
}

function numberOrNull(value: number | null | undefined) {
  return typeof value === 'number' ? value : null
}

function metricValue(evaluation: CompareProviderEvaluation, metricKey: MetricKey) {
  return evaluation.metrics[metricKey]
}

function averageMetric(
  samples: CompareSampleUnit[],
  side: 'baseline' | 'llm',
  metricKey: MetricKey,
) {
  const values = samples
    .map((sample) => metricValue(sample[side], metricKey))
    .filter((value): value is number => typeof value === 'number')
  if (values.length === 0) {
    return null
  }
  return values.reduce((sum, value) => sum + value, 0) / values.length
}

function buildServiceGroups(samples: CompareSampleUnit[], metricKeys: MetricKey[]) {
  const groups = new Map<string, CompareSampleUnit[]>()
  for (const sample of samples) {
    const bucket = groups.get(sample.service) ?? []
    bucket.push(sample)
    groups.set(sample.service, bucket)
  }

  return Array.from(groups.entries()).map(([label, bucket]) => ({
    label,
    baseline: Object.fromEntries(
      metricKeys.map((metricKey) => [metricKey, averageMetric(bucket, 'baseline', metricKey)]),
    ),
    llm: Object.fromEntries(
      metricKeys.map((metricKey) => [metricKey, averageMetric(bucket, 'llm', metricKey)]),
    ),
  }))
}

function domain(values: number[], fallback: [number, number], padding = 0.08) {
  if (values.length === 0) {
    return fallback
  }
  const min = Math.min(...values)
  const max = Math.max(...values)
  if (Math.abs(max - min) < 0.0001) {
    return [Math.max(0, min - 0.05), max + 0.05] as [number, number]
  }
  const range = max - min
  return [Math.max(0, min - range * padding), max + range * padding] as [number, number]
}

function scale(value: number, [min, max]: [number, number], size: number) {
  if (max - min < 0.0001) {
    return size * 0.5
  }
  return ((value - min) / (max - min)) * size
}

function selectedSample(samples: CompareSampleUnit[], selectedSampleId: string) {
  return samples.find((sample) => sample.id === selectedSampleId) ?? samples[0]
}

function comparisonDistance(
  sample: CompareSampleUnit,
  metricX: MetricKey,
  metricY: MetricKey,
) {
  const baselineX = numberOrNull(metricValue(sample.baseline, metricX))
  const baselineY = numberOrNull(metricValue(sample.baseline, metricY))
  const llmX = numberOrNull(metricValue(sample.llm, metricX))
  const llmY = numberOrNull(metricValue(sample.llm, metricY))
  if (baselineX === null || baselineY === null || llmX === null || llmY === null) {
    return 0
  }
  return Math.hypot(llmX - baselineX, llmY - baselineY)
}

function heroHighlights(
  samples: CompareSampleUnit[],
  selectedSampleId: string,
  metricX: MetricKey,
  metricY: MetricKey,
) {
  const ranked = [...samples]
    .sort(
      (left, right) =>
        comparisonDistance(right, metricX, metricY) -
        comparisonDistance(left, metricX, metricY),
    )
    .slice(0, 4)
    .map((sample) => sample.id)

  return new Set([selectedSampleId, ...ranked])
}

function compactText(text: string, max = 28) {
  if (text.length <= max) {
    return text
  }
  return `${text.slice(0, Math.max(10, max - 1))}…`
}

function sampleTag(index: number) {
  return `B${String(index + 1).padStart(2, '0')}`
}

function FieldFrame(props: {
  title: string
  caption?: string
  note?: string
  className?: string
  children: ReactNode
}) {
  return (
    <article className={`compare-frame ${props.className ?? ''}`.trim()}>
      <header className="compare-frame-head compare-frame-head-field">
        <div>
          <strong>{props.title}</strong>
          {props.caption ? <span>{props.caption}</span> : null}
        </div>
        {props.note ? <div className="compare-field-note">{props.note}</div> : null}
      </header>
      {props.children}
    </article>
  )
}

function FieldLegend(props: {
  locale: 'en' | 'zh'
  x: number
  y: number
  width?: number
}) {
  const legendWidth = props.width ?? 172
  return (
    <g transform={`translate(${props.x} ${props.y})`} className="compare-field-legend">
      <rect x={0} y={0} width={legendWidth} height={54} className="compare-field-legend-plane" />
      <rect x={12} y={15} width={8} height={8} className="compare-point-baseline compare-point-baseline-hero" />
      <text x={30} y={22} className="compare-axis-label">
        {pick(props.locale, 'baseline', '基线')}
      </text>
      <circle cx={16} cy={38} r={5} className="compare-point-llm compare-point-llm-hero" />
      <text x={30} y={40} className="compare-axis-label">
        LLM
      </text>
      <line x1={100} y1={18} x2={132} y2={18} className="compare-pair-link compare-pair-link-legend" />
      <text x={140} y={22} className="compare-axis-label">
        {pick(props.locale, 'grounded', '有支撑')}
      </text>
      <line x1={100} y1={36} x2={132} y2={36} className="compare-pair-link is-missing" />
      <text x={140} y={40} className="compare-axis-label">
        {pick(props.locale, 'unsupported', '无支撑')}
      </text>
    </g>
  )
}

function PairedConstellation(props: {
  locale: 'en' | 'zh'
  samples: CompareSampleUnit[]
  selectedSampleId: string
  metricX: MetricKey
  metricY: MetricKey
  title: string
  xLabel: string
  yLabel: string
  note: string
  onInspectSample: (sampleId: string) => void
  legendPlacement?: {
    x: number
    y: number
    width?: number
  }
  xLabelY?: number
}) {
  const width = 1180
  const height = 640
  const left = 92
  const right = 42
  const top = 62
  const bottom = 92
  const chartWidth = width - left - right
  const chartHeight = height - top - bottom
  const xValues = props.samples.flatMap((sample) => {
    const values = [
      numberOrNull(metricValue(sample.baseline, props.metricX)),
      numberOrNull(metricValue(sample.llm, props.metricX)),
    ]
    return values.filter((value): value is number => value !== null)
  })
  const yValues = props.samples.flatMap((sample) => {
    const values = [
      numberOrNull(metricValue(sample.baseline, props.metricY)),
      numberOrNull(metricValue(sample.llm, props.metricY)),
    ]
    return values.filter((value): value is number => value !== null)
  })
  const xDomain = domain(xValues, [0, 1])
  const yDomain = domain(yValues, [0, 1])
  const highlights = heroHighlights(
    props.samples,
    props.selectedSampleId,
    props.metricX,
    props.metricY,
  )
  const focusSample = selectedSample(props.samples, props.selectedSampleId)
  const focusIndex = Math.max(
    0,
    props.samples.findIndex((sample) => sample.id === focusSample?.id),
  )
  const focusBaselineX =
    numberOrNull(metricValue(focusSample.baseline, props.metricX)) ?? 0
  const focusBaselineY =
    numberOrNull(metricValue(focusSample.baseline, props.metricY)) ?? 0
  const focusX =
    numberOrNull(metricValue(focusSample.llm, props.metricX)) ??
    focusBaselineX
  const focusY =
    numberOrNull(metricValue(focusSample.llm, props.metricY)) ??
    focusBaselineY
  const focusGuideX = left + scale(focusX, xDomain, chartWidth)
  const focusGuideY = top + chartHeight - scale(focusY, yDomain, chartHeight)
  const calloutAnchorX = Math.min(width - 240, focusGuideX + 54)
  const calloutAnchorY = Math.max(top + 54, focusGuideY - 48)

  return (
    <FieldFrame
      title={props.title}
      caption={pick(props.locale, 'same alert / same evidence bundle', '同一告警 / 同一证据包')}
      note={props.note}
      className="compare-frame-hero compare-frame-benchmark-field"
    >
      <svg className="compare-hero-svg compare-hero-svg-field" viewBox={`0 0 ${width} ${height}`} role="img">
        <rect x="1" y="1" width={width - 2} height={height - 2} className="compare-hero-plane" />
        <rect x={left} y={top} width={chartWidth * 0.36} height={chartHeight} className="compare-threshold-band compare-threshold-band-low" />
        <rect x={left + chartWidth * 0.36} y={top} width={chartWidth * 0.28} height={chartHeight} className="compare-threshold-band compare-threshold-band-mid" />
        <rect x={left + chartWidth * 0.64} y={top} width={chartWidth * 0.36} height={chartHeight} className="compare-threshold-band compare-threshold-band-high" />
        <line x1={left - 36} y1={top + chartHeight * 0.24} x2={width - right} y2={top + chartHeight * 0.24} className="compare-zone-line" />
        <line x1={left - 18} y1={top + chartHeight * 0.62} x2={width - right} y2={top + chartHeight * 0.62} className="compare-zone-line is-strong" />
        <line x1={left + chartWidth * 0.34} y1={top - 24} x2={left + chartWidth * 0.34} y2={height - bottom + 18} className="compare-zone-line" />
        <line x1={left + chartWidth * 0.68} y1={top - 24} x2={left + chartWidth * 0.68} y2={height - bottom + 18} className="compare-zone-line is-strong" />
        <line x1={left - 60} y1={height - bottom + 22} x2={width - 14} y2={top - 10} className="compare-field-ray" />
        <line x1={left - 10} y1={top - 28} x2={width - right + 28} y2={top + chartHeight * 0.52} className="compare-field-ray" />
        <line x1={left + chartWidth * 0.14} y1={height - 18} x2={left + chartWidth * 0.14} y2={top - 22} className="compare-structural-guide" />
        <line x1={left + chartWidth * 0.84} y1={height - 18} x2={left + chartWidth * 0.84} y2={top - 22} className="compare-structural-guide" />

        {[0, 0.25, 0.5, 0.75, 1].map((tick) => {
          const y = top + chartHeight - chartHeight * tick
          const x = left + chartWidth * tick
          return (
            <g key={tick}>
              <line x1={left - 24} y1={y} x2={width - right} y2={y} className="compare-guide" />
              <line x1={x} y1={top - 16} x2={x} y2={height - bottom + 22} className="compare-guide compare-guide-vertical" />
              <text x={left - 14} y={y + 3} className="compare-axis-label" textAnchor="end">
                {Math.round(tick * 100)}
              </text>
              <text x={x} y={height - 22} className="compare-axis-label" textAnchor="middle">
                {Math.round(tick * 100)}
              </text>
            </g>
          )
        })}

        <line x1={left - 24} y1={top + chartHeight} x2={width - right} y2={top + chartHeight} className="compare-axis compare-axis-strong" />
        <line x1={left} y1={top - 18} x2={left} y2={top + chartHeight + 22} className="compare-axis compare-axis-strong" />
        <line x1={focusGuideX} y1={top - 16} x2={focusGuideX} y2={height - bottom + 22} className="compare-focus-guide" />
        <line x1={left - 24} y1={focusGuideY} x2={width - right} y2={focusGuideY} className="compare-focus-guide" />
        <circle cx={focusGuideX} cy={focusGuideY} r={22} className="compare-focus-ring" />

        {props.samples.map((sample, index) => {
          const baselineX = numberOrNull(metricValue(sample.baseline, props.metricX))
          const baselineY = numberOrNull(metricValue(sample.baseline, props.metricY))
          const llmX = numberOrNull(metricValue(sample.llm, props.metricX))
          const llmY = numberOrNull(metricValue(sample.llm, props.metricY))
          if (baselineX === null || baselineY === null) {
            return null
          }

          const startX = left + scale(baselineX, xDomain, chartWidth)
          const startY = top + chartHeight - scale(baselineY, yDomain, chartHeight)
          const endX = llmX === null ? startX : left + scale(llmX, xDomain, chartWidth)
          const endY = llmY === null ? startY : top + chartHeight - scale(llmY, yDomain, chartHeight)
          const isHighlighted = highlights.has(sample.id)
          const isSelected = sample.id === props.selectedSampleId
          const tag = sampleTag(index)

          return (
            <g
              key={sample.id}
              className={`compare-hero-pair ${isSelected ? 'is-selected' : ''}`}
              onClick={() => props.onInspectSample(sample.id)}
            >
              <line x1={startX} y1={startY} x2={endX} y2={endY} className={`compare-pair-link ${llmX === null || llmY === null ? 'is-missing' : ''}`} />
              <line x1={startX} y1={startY} x2={startX - 16} y2={startY - 16} className="compare-ghost-link" />
              <line x1={endX} y1={endY} x2={endX + 16} y2={endY + 16} className="compare-ghost-link" />
              <rect x={startX - 5} y={startY - 5} width={10} height={10} className="compare-point-baseline compare-point-baseline-hero" />
              {llmX === null || llmY === null ? (
                <circle cx={endX} cy={endY} r={7} className="compare-point-missing" />
              ) : (
                <circle cx={endX} cy={endY} r={7} className="compare-point-llm compare-point-llm-hero" />
              )}
              <text x={startX - 14} y={startY - 12} className="compare-index-mark" textAnchor="end">
                {tag}
              </text>
              {isHighlighted ? (
                <>
                  <line x1={endX + 10} y1={endY} x2={endX + 42} y2={endY} className="compare-callout-lead" />
                  <text x={endX + 48} y={endY - 5} className="compare-hero-label">
                    {sample.bundleId}
                  </text>
                  <text x={endX + 48} y={endY + 11} className="compare-hero-label-subtle">
                    {sample.ruleId}
                  </text>
                </>
              ) : null}
            </g>
          )
        })}

        <polyline
          points={`${focusGuideX},${focusGuideY} ${focusGuideX + 26},${focusGuideY - 26} ${calloutAnchorX},${calloutAnchorY}`}
          className="compare-selected-callout-line"
        />
        <text x={calloutAnchorX + 12} y={calloutAnchorY - 10} className="compare-benchmark-caption">
          {sampleTag(focusIndex)} / {focusSample.bundleId}
        </text>
        <text x={calloutAnchorX + 12} y={calloutAnchorY + 10} className="compare-hero-label-subtle">
          {focusSample.ruleId} · {compactText(focusSample.service, 18)}
        </text>
        <text x={calloutAnchorX + 12} y={calloutAnchorY + 28} className="compare-hero-label-subtle">
          {pick(props.locale, 'delta', '差值')} {percentLabel(props.locale, metricValue(focusSample.llm, props.metricY))} / {percentLabel(props.locale, metricValue(focusSample.llm, props.metricX))}
        </text>

        <text x={left - 6} y={top - 26} className="compare-benchmark-caption">
          {props.yLabel}
        </text>
        <text
          x={width - right}
          y={props.xLabelY ?? height - 14}
          className="compare-benchmark-caption"
          textAnchor="end"
        >
          {props.xLabel}
        </text>
        <text x={left + 8} y={top + 18} className="compare-secondary-label">
          {pick(props.locale, 'low evidence field', '低证据区带')}
        </text>
        <text x={left + chartWidth * 0.68 + 8} y={top + 18} className="compare-secondary-label">
          {pick(props.locale, 'high evidence field', '高证据区带')}
        </text>
        <FieldLegend
          locale={props.locale}
          x={props.legendPlacement?.x ?? left}
          y={props.legendPlacement?.y ?? height - 66}
          width={props.legendPlacement?.width}
        />
      </svg>
    </FieldFrame>
  )
}

function GroupDeltaBars(props: {
  locale: 'en' | 'zh'
  title: string
  groups: Array<{
    label: string
    baseline: Record<string, number | null>
    llm: Record<string, number | null>
  }>
  metricKey: MetricKey
}) {
  const width = 560
  const height = 260
  const left = 28
  const right = 18
  const top = 26
  const bottom = 54
  const chartWidth = width - left - right
  const chartHeight = height - top - bottom
  const rowWidth = chartWidth / Math.max(props.groups.length, 1)
  const barWidth = Math.min(18, rowWidth / 4)

  return (
    <FieldFrame title={props.title} caption={props.metricKey} className="compare-frame-diagnostic">
      <svg className="compare-svg" viewBox={`0 0 ${width} ${height}`} role="img">
        <line x1={left} y1={top + chartHeight} x2={width - right} y2={top + chartHeight} className="compare-axis compare-axis-strong" />
        <line x1={left} y1={top - 8} x2={left} y2={top + chartHeight} className="compare-axis" />
        {[0.25, 0.5, 0.75, 1].map((tick) => (
          <g key={tick}>
            <line x1={left} y1={top + chartHeight - chartHeight * tick} x2={width - right} y2={top + chartHeight - chartHeight * tick} className="compare-guide" />
            <text x={left - 8} y={top + chartHeight - chartHeight * tick + 3} textAnchor="end" className="compare-axis-label">
              {Math.round(tick * 100)}
            </text>
          </g>
        ))}
        {props.groups.map((group, index) => {
          const baseline = group.baseline[props.metricKey] ?? 0
          const llm = group.llm[props.metricKey] ?? 0
          const x = left + rowWidth * index + rowWidth / 2
          return (
            <g key={group.label}>
              <line x1={left + rowWidth * index} y1={top - 8} x2={left + rowWidth * index} y2={top + chartHeight} className="compare-guide compare-guide-vertical" />
              <rect
                x={x - barWidth - 5}
                y={top + chartHeight - chartHeight * baseline}
                width={barWidth}
                height={chartHeight * baseline}
                className="compare-bar compare-bar-baseline"
              />
              <rect
                x={x + 5}
                y={top + chartHeight - chartHeight * llm}
                width={barWidth}
                height={chartHeight * llm}
                className="compare-bar compare-bar-llm"
              />
              <line
                x1={x - barWidth / 2 - 5}
                y1={top + chartHeight - chartHeight * baseline}
                x2={x + barWidth / 2 + 5}
                y2={top + chartHeight - chartHeight * llm}
                className="compare-callout-lead"
              />
              <text x={x} y={height - 18} textAnchor="middle" className="compare-axis-label">
                {group.label}
              </text>
            </g>
          )
        })}
      </svg>
    </FieldFrame>
  )
}

function RuleMetricMatrix(props: {
  locale: 'en' | 'zh'
  groups: Array<{
    label: string
    baseline: Record<string, number | null>
    llm: Record<string, number | null>
  }>
  metricKeys: MetricKey[]
}) {
  return (
    <FieldFrame
      title={pick(props.locale, 'Diagnostic Rule Matrix', '规则诊断矩阵')}
      caption={pick(props.locale, 'baseline / llm / delta stack', '基线 / 模型 / 差值层')}
      className="compare-frame-diagnostic"
    >
      <div className="compare-rule-matrix">
        <div className="compare-rule-matrix-head">
          <span>{pick(props.locale, 'rule', '规则')}</span>
          {props.metricKeys.map((metricKey) => (
            <span key={metricKey}>{metricKey}</span>
          ))}
        </div>
        {props.groups.map((group, groupIndex) => (
          <div key={group.label} className="compare-rule-matrix-row">
            <div className="compare-rule-matrix-label">
              <em>{sampleTag(groupIndex)}</em>
              <strong>{group.label}</strong>
            </div>
            {props.metricKeys.map((metricKey) => {
              const baseline = group.baseline[metricKey]
              const llm = group.llm[metricKey]
              const delta = baseline === null || llm === null ? null : llm - baseline
              const baselineWidth = `${Math.max(6, (baseline ?? 0) * 100)}%`
              const llmWidth = `${Math.max(6, (llm ?? 0) * 100)}%`
              return (
                <div
                  key={`${group.label}-${metricKey}`}
                  className={`compare-rule-matrix-cell ${delta !== null && delta >= 0 ? 'improved' : 'regressed'}`}
                >
                  <span className="compare-rule-cell-kicker">{metricKey}</span>
                  <div className="compare-rule-cell-layer">
                    <i className="baseline" style={{ width: baselineWidth }} />
                    <strong>{percentLabel(props.locale, baseline)}</strong>
                  </div>
                  <div className="compare-rule-cell-layer">
                    <i className="llm" style={{ width: llmWidth }} />
                    <strong>{percentLabel(props.locale, llm)}</strong>
                  </div>
                  <div className="compare-rule-cell-delta">
                    {delta === null
                      ? pick(props.locale, 'pending', '待接入')
                      : `${delta > 0 ? '+' : ''}${Math.round(delta * 100)}p`}
                  </div>
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </FieldFrame>
  )
}

function EvidenceGroundingMap(props: {
  locale: 'en' | 'zh'
  sample: CompareSampleUnit | undefined
  onInspectSample: (sampleId: string) => void
}) {
  if (!props.sample) {
    return (
      <FieldFrame
        title={pick(props.locale, 'Grounding Wiring', '证据接线图')}
        caption={pick(props.locale, 'select bundle', '请选择样本')}
        className="compare-frame-diagnostic"
      >
        <div className="compare-empty-state">
          {pick(props.locale, 'No active bundle.', '当前没有选中样本。')}
        </div>
      </FieldFrame>
    )
  }

  const references = props.sample.llm.evidenceReferences.slice(0, 5)
  const unsupported = props.sample.llm.unsupportedClaims.slice(0, 3)
  const evidenceKeys = Array.from(
    new Map(
      references.map((reference) => [
        `${reference.sourceSection}.${reference.sourceField}`,
        reference,
      ]),
    ).values(),
  )
  const width = 980
  const height = 320
  const leftX = 120
  const rightX = 710
  const unsupportedX = 820
  const topY = 54
  const evidenceGap = Math.max(48, 220 / Math.max(evidenceKeys.length, 1))
  const claimGap = Math.max(42, 220 / Math.max(references.length, 1))
  const unsupportedGap = Math.max(42, 180 / Math.max(unsupported.length, 1))
  const evidenceY = (index: number) => topY + index * evidenceGap
  const claimY = (index: number) => topY + index * claimGap
  const unsupportedY = (index: number) => 92 + index * unsupportedGap
  const evidenceLookup = new Map(
    evidenceKeys.map((reference, index) => [
      `${reference.sourceSection}.${reference.sourceField}`,
      evidenceY(index),
    ]),
  )

  return (
    <FieldFrame
      title={pick(props.locale, 'Grounding Wiring', '证据接线图')}
      caption={props.sample.bundleId}
      note={pick(props.locale, 'evidence -> claim / broken = unsupported', '证据 -> 结论 / 断线 = 无支撑')}
      className="compare-frame-diagnostic"
    >
      <div className="compare-grounding-diagram">
        <svg className="compare-grounding-svg" viewBox={`0 0 ${width} ${height}`} role="img">
          <line x1={leftX - 54} y1={30} x2={leftX - 54} y2={height - 24} className="compare-structural-guide" />
          <line x1={rightX - 24} y1={30} x2={rightX - 24} y2={height - 24} className="compare-structural-guide" />
          <line x1={unsupportedX - 18} y1={78} x2={unsupportedX - 18} y2={height - 24} className="compare-zone-line" />
          <text x={leftX - 54} y={18} className="compare-benchmark-caption">
            {pick(props.locale, 'evidence nodes', '证据节点')}
          </text>
          <text x={rightX - 24} y={18} className="compare-benchmark-caption">
            {pick(props.locale, 'claim / action nodes', '结论 / 动作节点')}
          </text>
          <text x={unsupportedX - 18} y={64} className="compare-benchmark-caption">
            {pick(props.locale, 'unsupported', '无支撑')}
          </text>

          {references.map((reference, index) => {
            const evidenceKey = `${reference.sourceSection}.${reference.sourceField}`
            const startY = evidenceLookup.get(evidenceKey) ?? topY
            const endY = claimY(index)
            const midX = 474 + index * 8
            return (
              <polyline
                key={reference.id}
                points={`${leftX + 110},${startY} ${midX},${startY} ${midX},${endY} ${rightX - 112},${endY}`}
                className={`compare-grounding-wire ${reference.supported ? 'is-supported' : 'is-unsupported'}`}
              />
            )
          })}

          {unsupported.map((item, index) => {
            const y = unsupportedY(index)
            return (
              <g key={item.id}>
                <line x1={unsupportedX - 92} y1={y} x2={unsupportedX - 28} y2={y} className="compare-grounding-wire compare-grounding-wire-broken" />
                <line x1={unsupportedX - 18} y1={y} x2={unsupportedX + 4} y2={y} className="compare-grounding-wire compare-grounding-wire-broken" />
              </g>
            )
          })}

          {evidenceKeys.map((reference, index) => {
            const y = evidenceY(index)
            return (
              <g key={`${reference.sourceSection}.${reference.sourceField}`}>
                <rect x={leftX - 2} y={y - 14} width={118} height={30} rx="0" className="compare-grounding-node compare-grounding-node-evidence" />
                <text x={leftX + 10} y={y - 2} className="compare-axis-label">
                  {sampleTag(index)}
                </text>
                <text x={leftX + 10} y={y + 11} className="compare-grounding-node-text">
                  {compactText(`${reference.sourceSection}.${reference.sourceField}`, 18)}
                </text>
              </g>
            )
          })}

          {references.map((reference, index) => {
            const y = claimY(index)
            return (
              <g key={`claim-${reference.id}`}>
                <rect x={rightX - 122} y={y - 14} width={160} height={30} rx="0" className="compare-grounding-node compare-grounding-node-claim" />
                <text x={rightX - 110} y={y + 2} className="compare-grounding-node-text">
                  {compactText(reference.claim, 26)}
                </text>
              </g>
            )
          })}

          {unsupported.map((item, index) => {
            const y = unsupportedY(index)
            return (
              <g key={`unsupported-${item.id}`}>
                <rect x={unsupportedX + 8} y={y - 14} width={140} height={30} rx="0" className="compare-grounding-node compare-grounding-node-unsupported" />
                <text x={unsupportedX + 20} y={y + 2} className="compare-grounding-node-text">
                  {compactText(item.claim, 22)}
                </text>
              </g>
            )
          })}
        </svg>

        <button className="compare-action compare-grounding-action" type="button" onClick={() => props.onInspectSample(props.sample!.id)}>
          {pick(props.locale, 'Open A/B', '打开 A/B')}
        </button>
      </div>
    </FieldFrame>
  )
}

function ReplayMatrix(props: {
  locale: 'en' | 'zh'
  samples: CompareSampleUnit[]
  onInspectSample: (sampleId: string) => void
}) {
  return (
    <div className="compare-replay-matrix">
      {props.samples.map((sample, index) => {
        const runIndex = Number(sample.replay.runId.replace('run-', '')) || 1
        const cells = Array.from({ length: sample.replay.runCount }, (_, cellIndex) => cellIndex + 1)
        return (
          <button
            key={sample.id}
            className="compare-replay-row"
            type="button"
            onClick={() => props.onInspectSample(sample.id)}
          >
            <span className="compare-replay-label">
              <em>{sampleTag(index)}</em>
              <strong>{sample.bundleId}</strong>
            </span>
            <div className="compare-replay-cells">
              {cells.map((cell) => (
                <span
                  key={`${sample.id}-${cell}`}
                  className={`compare-replay-cell ${cell === runIndex ? 'is-active' : ''} ${sample.llm.status === 'ready' ? 'llm-ready' : 'llm-pending'}`}
                >
                  <em style={{ opacity: sample.baseline.metrics.stability ?? 0.2 }} />
                  <i style={{ opacity: sample.llm.metrics.stability ?? 0.2 }} />
                </span>
              ))}
            </div>
            <span className="compare-replay-tail">{percentLabel(props.locale, sample.llm.metrics.stability)}</span>
          </button>
        )
      })}
    </div>
  )
}

function TimingTrajectory(props: {
  locale: 'en' | 'zh'
  samples: CompareSampleUnit[]
  selectedSampleId: string
  onInspectSample: (sampleId: string) => void
}) {
  const width = 1180
  const height = 620
  const left = 92
  const right = 42
  const top = 54
  const bottom = 92
  const chartWidth = width - left - right
  const chartHeight = height - top - bottom
  const costs = props.samples.flatMap((sample) => {
    const values = [
      sample.baseline.metrics.estimatedCostUsd ?? 0,
      sample.llm.metrics.estimatedCostUsd ?? 0,
    ]
    return values.filter((value) => typeof value === 'number')
  })
  const actionability = props.samples.flatMap((sample) => {
    const values = [
      sample.baseline.metrics.actionability,
      sample.llm.metrics.actionability,
    ]
    return values.filter((value): value is number => typeof value === 'number')
  })
  const costDomain = domain(costs, [0, 0.02], 0.18)
  const actionDomain = domain(actionability, [0, 1], 0.1)
  const highlights = heroHighlights(
    props.samples,
    props.selectedSampleId,
    'estimatedCostUsd',
    'actionability',
  )
  const focusSample = selectedSample(props.samples, props.selectedSampleId)
  const focusIndex = Math.max(
    0,
    props.samples.findIndex((sample) => sample.id === focusSample?.id),
  )
  const focusX = focusSample.llm.metrics.estimatedCostUsd ?? focusSample.baseline.metrics.estimatedCostUsd ?? 0
  const focusY = focusSample.llm.metrics.actionability ?? focusSample.baseline.metrics.actionability ?? 0
  const focusGuideX = left + scale(focusX, costDomain, chartWidth)
  const focusGuideY = top + chartHeight - scale(focusY, actionDomain, chartHeight)

  return (
    <FieldFrame
      title={pick(props.locale, 'Cost / Quality Benchmark Field', '成本 / 质量基准场')}
      caption={pick(props.locale, 'same bundle paired trajectory', '同一 bundle 成对轨迹')}
      note={pick(props.locale, 'radius = latency / zone = cost risk', '半径 = 时延 / 区带 = 成本风险')}
      className="compare-frame-hero compare-frame-benchmark-field"
    >
      <svg className="compare-hero-svg compare-hero-svg-field" viewBox={`0 0 ${width} ${height}`} role="img">
        <rect x="1" y="1" width={width - 2} height={height - 2} className="compare-hero-plane" />
        <rect x={left} y={top} width={chartWidth * 0.42} height={chartHeight} className="compare-threshold-band compare-threshold-band-low" />
        <rect x={left + chartWidth * 0.42} y={top} width={chartWidth * 0.26} height={chartHeight} className="compare-threshold-band compare-threshold-band-mid" />
        <rect x={left + chartWidth * 0.68} y={top} width={chartWidth * 0.32} height={chartHeight} className="compare-threshold-band compare-threshold-band-risk" />
        <line x1={left + chartWidth * 0.68} y1={top - 18} x2={left + chartWidth * 0.68} y2={height - bottom + 20} className="compare-zone-line is-strong" />
        <line x1={left - 24} y1={top + chartHeight * 0.35} x2={width - right} y2={top + chartHeight * 0.35} className="compare-zone-line" />
        <line x1={left - 24} y1={top + chartHeight * 0.65} x2={width - right} y2={top + chartHeight * 0.65} className="compare-zone-line is-strong" />
        <line x1={left - 42} y1={height - bottom + 24} x2={width - 16} y2={top + chartHeight * 0.22} className="compare-field-ray" />
        <line x1={left - 6} y1={top - 26} x2={width - 24} y2={top + chartHeight * 0.78} className="compare-field-ray" />

        {[0.25, 0.5, 0.75, 1].map((tick) => (
          <g key={tick}>
            <line x1={left - 24} y1={top + chartHeight - chartHeight * tick} x2={width - right} y2={top + chartHeight - chartHeight * tick} className="compare-guide" />
            <text x={left - 10} y={top + chartHeight - chartHeight * tick + 3} textAnchor="end" className="compare-axis-label">
              {Math.round(tick * 100)}
            </text>
          </g>
        ))}

        <line x1={left - 24} y1={top + chartHeight} x2={width - right} y2={top + chartHeight} className="compare-axis compare-axis-strong" />
        <line x1={left} y1={top - 18} x2={left} y2={top + chartHeight + 22} className="compare-axis compare-axis-strong" />
        <line x1={focusGuideX} y1={top - 14} x2={focusGuideX} y2={height - bottom + 22} className="compare-focus-guide" />
        <line x1={left - 22} y1={focusGuideY} x2={width - right} y2={focusGuideY} className="compare-focus-guide" />
        <circle cx={focusGuideX} cy={focusGuideY} r={22} className="compare-focus-ring" />

        {props.samples.map((sample, index) => {
          const baselineCost = sample.baseline.metrics.estimatedCostUsd ?? 0
          const llmCost = sample.llm.metrics.estimatedCostUsd
          const baselineAction = sample.baseline.metrics.actionability
          const llmAction = sample.llm.metrics.actionability
          if (baselineAction === null) {
            return null
          }
          const startX = left + scale(baselineCost, costDomain, chartWidth)
          const startY = top + chartHeight - scale(baselineAction, actionDomain, chartHeight)
          const endX = llmCost === null ? startX : left + scale(llmCost, costDomain, chartWidth)
          const endY = llmAction === null ? startY : top + chartHeight - scale(llmAction, actionDomain, chartHeight)
          const baseRadius = 4 + ((sample.baseline.metrics.latencyMs ?? 0) / 1500) * 8
          const llmRadius = 5 + ((sample.llm.metrics.latencyMs ?? 0) / 1500) * 8
          const isHighlighted = highlights.has(sample.id)
          const isSelected = sample.id === props.selectedSampleId
          return (
            <g
              key={sample.id}
              className={`compare-hero-pair ${isSelected ? 'is-selected' : ''}`}
              onClick={() => props.onInspectSample(sample.id)}
            >
              <line x1={startX} y1={startY} x2={endX} y2={endY} className={`compare-pair-link ${llmAction === null || llmCost === null ? 'is-missing' : ''}`} />
              <rect x={startX - baseRadius / 2} y={startY - baseRadius / 2} width={baseRadius} height={baseRadius} className="compare-point-baseline compare-point-baseline-hero" />
              {llmAction === null || llmCost === null ? (
                <circle cx={endX} cy={endY} r={llmRadius} className="compare-point-missing" />
              ) : (
                <circle cx={endX} cy={endY} r={llmRadius} className="compare-point-llm compare-point-llm-hero" />
              )}
              <text x={startX - 12} y={startY - 10} className="compare-index-mark" textAnchor="end">
                {sampleTag(index)}
              </text>
              {isHighlighted ? (
                <>
                  <line x1={endX + 8} y1={endY} x2={endX + 32} y2={endY} className="compare-callout-lead" />
                  <text x={endX + 38} y={endY - 4} className="compare-hero-label">
                    {sample.bundleId}
                  </text>
                </>
              ) : null}
            </g>
          )
        })}

        <polyline
          points={`${focusGuideX},${focusGuideY} ${focusGuideX + 24},${focusGuideY - 24} ${Math.min(width - 210, focusGuideX + 138)},${Math.max(top + 50, focusGuideY - 56)}`}
          className="compare-selected-callout-line"
        />
        <text x={Math.min(width - 198, focusGuideX + 150)} y={Math.max(top + 40, focusGuideY - 66)} className="compare-benchmark-caption">
          {sampleTag(focusIndex)} / {focusSample.bundleId}
        </text>
        <text x={Math.min(width - 198, focusGuideX + 150)} y={Math.max(top + 58, focusGuideY - 48)} className="compare-hero-label-subtle">
          {usdLabel(props.locale, focusSample.llm.metrics.estimatedCostUsd)} · {percentLabel(props.locale, focusSample.llm.metrics.actionability)}
        </text>
        <text x={left - 6} y={top - 26} className="compare-benchmark-caption">
          {pick(props.locale, 'actionability', '可执行性')}
        </text>
        <text x={width - right} y={height - 14} className="compare-benchmark-caption" textAnchor="end">
          {pick(props.locale, 'estimated cost usd', '估算成本 usd')}
        </text>
        <FieldLegend locale={props.locale} x={left} y={height - 66} />
      </svg>
    </FieldFrame>
  )
}

function DistributionBox(props: {
  locale: 'en' | 'zh'
  title: string
  baseline: number[]
  llm: number[]
  unit: 'ms' | 'usd'
}) {
  const width = 420
  const height = 190
  const left = 28
  const right = 16
  const top = 18
  const bottom = 34
  const chartWidth = width - left - right
  const chartHeight = height - top - bottom
  const maxValue = Math.max(...props.baseline, ...props.llm, 1)

  function quantile(values: number[], position: number) {
    if (values.length === 0) {
      return 0
    }
    const sorted = [...values].sort((leftValue, rightValue) => leftValue - rightValue)
    const index = (sorted.length - 1) * position
    const lower = Math.floor(index)
    const upper = Math.ceil(index)
    if (lower === upper) {
      return sorted[lower]
    }
    const weight = index - lower
    return sorted[lower] * (1 - weight) + sorted[upper] * weight
  }

  function render(values: number[], centerX: number, className: string) {
    if (values.length === 0) {
      return null
    }
    const q1 = quantile(values, 0.25)
    const median = quantile(values, 0.5)
    const q3 = quantile(values, 0.75)
    const low = quantile(values, 0)
    const high = quantile(values, 1)
    const y = (value: number) => top + chartHeight - (value / maxValue) * chartHeight
    return (
      <g className={className}>
        <line x1={centerX} y1={y(low)} x2={centerX} y2={y(high)} className="compare-axis" />
        <rect x={centerX - 26} y={y(q3)} width={52} height={Math.max(8, y(q1) - y(q3))} className="compare-box" />
        <line x1={centerX - 26} y1={y(median)} x2={centerX + 26} y2={y(median)} className="compare-axis compare-axis-strong" />
      </g>
    )
  }

  return (
    <FieldFrame
      title={props.title}
      caption={props.unit === 'ms'
        ? pick(props.locale, 'latency spread', '时延分布')
        : pick(props.locale, 'cost spread', '成本分布')}
      className="compare-frame-diagnostic"
    >
      <svg className="compare-svg" viewBox={`0 0 ${width} ${height}`} role="img">
        <line x1={left} y1={top + chartHeight} x2={width - right} y2={top + chartHeight} className="compare-axis compare-axis-strong" />
        <line x1={left} y1={top} x2={left} y2={top + chartHeight} className="compare-axis" />
        {render(props.baseline, left + chartWidth * 0.3, 'compare-box-baseline')}
        {render(props.llm, left + chartWidth * 0.7, 'compare-box-llm')}
        <text x={left + chartWidth * 0.3} y={height - 12} textAnchor="middle" className="compare-axis-label">
          {pick(props.locale, 'Baseline', '基线')}
        </text>
        <text x={left + chartWidth * 0.7} y={height - 12} textAnchor="middle" className="compare-axis-label">
          LLM
        </text>
      </svg>
    </FieldFrame>
  )
}

function StatusRack(props: {
  locale: 'en' | 'zh'
  counts: {
    baselineReady: number
    llmReady: number
    llmPlaceholder: number
    llmFailed: number
    hallucinationFlags: number
  }
}) {
  return (
    <FieldFrame
      title={pick(props.locale, 'Timing Ledger', '运行账本')}
      caption={pick(props.locale, 'provider state counts', '提供器状态计数')}
      className="compare-frame-diagnostic"
    >
      <div className="compare-status-rack">
        <div>
          <span>{pick(props.locale, 'baseline ready', '基线就绪')}</span>
          <strong>{props.counts.baselineReady}</strong>
        </div>
        <div>
          <span>{pick(props.locale, 'llm ready', '模型就绪')}</span>
          <strong>{props.counts.llmReady}</strong>
        </div>
        <div>
          <span>{pick(props.locale, 'placeholder', '占位')}</span>
          <strong>{props.counts.llmPlaceholder}</strong>
        </div>
        <div>
          <span>{pick(props.locale, 'failed', '失败')}</span>
          <strong>{props.counts.llmFailed}</strong>
        </div>
        <div>
          <span>{pick(props.locale, 'unsupported', '无支撑')}</span>
          <strong>{props.counts.hallucinationFlags}</strong>
        </div>
      </div>
    </FieldFrame>
  )
}

export function CompareCharts({
  locale,
  activeTab,
  samples,
  selectedSampleId,
  onInspectSample,
}: CompareChartsProps) {
  const rules = buildRuleGroups(samples)
  const severities = buildSeverityGroups(samples)
  const replays = buildReplayGroups(samples)
  const services = buildServiceGroups(samples, [
    'explanationCompleteness',
    'actionability',
    'evidenceBinding',
  ])
  const selected = selectedSample(samples, selectedSampleId)
  const latencyDistribution = buildMetricDistribution(samples, 'latencyMs')
  const costDistribution = buildMetricDistribution(samples, 'estimatedCostUsd')
  const statusCounts = buildStatusCounts(samples)

  if (activeTab === 'explanation') {
    return (
      <div className="compare-benchmark-field">
        <PairedConstellation
          locale={locale}
          samples={samples}
          selectedSampleId={selectedSampleId}
          metricX="explanationCompleteness"
          metricY="evidenceBinding"
          title={pick(locale, 'Explanation Benchmark Field', '解释基准场')}
          xLabel={pick(locale, 'explanation completeness', '解释完整度')}
          yLabel={pick(locale, 'evidence binding', '证据绑定率')}
          note={pick(locale, 'paired provider outputs / same bundle', '同一 bundle 的两条提供器输出')}
          onInspectSample={onInspectSample}
          legendPlacement={{ x: 108, y: 438, width: 204 }}
          xLabelY={608}
        />

        <div className="compare-secondary-grid compare-secondary-grid-benchmark">
          <RuleMetricMatrix locale={locale} groups={rules} metricKeys={['explanationCompleteness', 'evidenceBinding']} />
          <EvidenceGroundingMap locale={locale} sample={selected} onInspectSample={onInspectSample} />
          <GroupDeltaBars locale={locale} title={pick(locale, 'Service Slice', '服务切片')} groups={services} metricKey="explanationCompleteness" />
        </div>
      </div>
    )
  }

  if (activeTab === 'action') {
    return (
      <div className="compare-benchmark-field">
        <PairedConstellation
          locale={locale}
          samples={samples}
          selectedSampleId={selectedSampleId}
          metricX="evidenceBinding"
          metricY="actionability"
          title={pick(locale, 'Action Benchmark Field', '动作基准场')}
          xLabel={pick(locale, 'evidence binding', '证据绑定率')}
          yLabel={pick(locale, 'actionability', '可执行性')}
          note={pick(locale, 'claim grounding / action specificity', '结论落点与动作具体性')}
          onInspectSample={onInspectSample}
        />

        <div className="compare-secondary-grid compare-secondary-grid-benchmark">
          <GroupDeltaBars locale={locale} title={pick(locale, 'Rule Slice', '规则切片')} groups={rules} metricKey="actionability" />
          <GroupDeltaBars locale={locale} title={pick(locale, 'Severity Slice', '级别切片')} groups={severities} metricKey="auditability" />
          <RuleMetricMatrix locale={locale} groups={rules} metricKeys={['actionability', 'hallucinationRate']} />
        </div>
      </div>
    )
  }

  if (activeTab === 'stability') {
    return (
      <div className="compare-benchmark-field">
        <FieldFrame
          title={pick(locale, 'Replay Stability Matrix', '回放稳定矩阵')}
          caption={pick(locale, 'run position / stability / provider status', '运行位次 / 稳定性 / 提供器状态')}
          note={pick(locale, 'same bundle repeated replay ledger', '同一 bundle 的重复回放账本')}
          className="compare-frame-hero compare-frame-benchmark-field"
        >
          <ReplayMatrix locale={locale} samples={samples} onInspectSample={onInspectSample} />
        </FieldFrame>

        <div className="compare-secondary-grid compare-secondary-grid-benchmark">
          <GroupDeltaBars locale={locale} title={pick(locale, 'Replay Slice', '回放切片')} groups={replays} metricKey="stability" />
          <GroupDeltaBars locale={locale} title={pick(locale, 'Severity Slice', '级别切片')} groups={severities} metricKey="failureRate" />
          <StatusRack locale={locale} counts={statusCounts} />
        </div>
      </div>
    )
  }

  return (
    <div className="compare-benchmark-field">
      <TimingTrajectory
        locale={locale}
        samples={samples}
        selectedSampleId={selectedSampleId}
        onInspectSample={onInspectSample}
      />

      <div className="compare-secondary-grid compare-secondary-grid-benchmark">
        <DistributionBox
          locale={locale}
          title={pick(locale, 'Latency Distribution', '时延分布')}
          baseline={latencyDistribution.baseline}
          llm={latencyDistribution.llm}
          unit="ms"
        />
        <DistributionBox
          locale={locale}
          title={pick(locale, 'Cost Distribution', '成本分布')}
          baseline={costDistribution.baseline}
          llm={costDistribution.llm}
          unit="usd"
        />
        <FieldFrame
          title={pick(locale, 'Timing Slice', '运行时切片')}
          caption={selected ? selected.bundleId : pick(locale, 'no active bundle', '当前无选中样本')}
          note={
            selected
              ? [
                  `B ${msLabel(locale, selected.baseline.timing.latencyMs)}`,
                  `L ${msLabel(locale, selected.llm.timing.latencyMs)}`,
                  `${pick(locale, 'cost', '成本')} ${usdLabel(locale, selected.llm.timing.estimatedCostUsd)}`,
                ].join(' / ')
              : undefined
          }
          className="compare-frame-diagnostic"
        >
          <div className="compare-timing-micro">
            <div>
              <span>{pick(locale, 'input tokens', '输入 token')}</span>
              <strong>{selected?.llm.timing.inputTokens ?? 0}</strong>
            </div>
            <div>
              <span>{pick(locale, 'output tokens', '输出 token')}</span>
              <strong>{selected?.llm.timing.outputTokens ?? 0}</strong>
            </div>
            <div>
              <span>{pick(locale, 'failure', '失败')}</span>
              <strong>{selected?.llm.timing.failure ? pick(locale, 'yes', '是') : pick(locale, 'no', '否')}</strong>
            </div>
            <div>
              <span>{pick(locale, 'consistency', '一致性')}</span>
              <strong>{percentLabel(locale, selected?.llm.timing.replayConsistency ?? null)}</strong>
            </div>
          </div>
        </FieldFrame>
      </div>
    </div>
  )
}
