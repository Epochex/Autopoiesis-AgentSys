import type { ControlLoopSnapshot, StageTelemetry, SuggestionRecord } from '../types'
import { formatMaybeTimestamp, formatPreciseDurationMs } from '../utils/time'

interface ControlLoopVisualPanelsProps {
  snapshot: ControlLoopSnapshot
  selectedSuggestion: SuggestionRecord
  locale: 'en' | 'zh'
}

interface LatencyRow {
  label: string
  durationMs: number
}

interface EvidenceCoverageRow {
  label: string
  value: number
  detail: string
}

interface IncidentEnvelopeRow {
  label: string
  value: string
}

interface ProcessTraceSegment {
  id: string
  title: string
  detail: string
  value: string
  tone: 'raw' | 'alert' | 'suggestion' | 'neutral'
  state: StageTelemetry['state']
}

function buildPolyline(values: number[], width: number, height: number) {
  const max = Math.max(...values, 1)
  const innerWidth = width - 40
  const innerHeight = height - 36
  const step = values.length > 1 ? innerWidth / (values.length - 1) : 0

  return values
    .map((value, index) => {
      const x = 20 + step * index
      const y = 12 + innerHeight - (value / max) * innerHeight
      return `${x},${y}`
    })
    .join(' ')
}

function numericFromValue(value: unknown) {
  return typeof value === 'number' ? value : 0
}

function printableValue(value: unknown) {
  if (Array.isArray(value)) {
    return value.filter((item) => typeof item === 'string' && item.trim().length > 0).join(', ')
  }
  if (typeof value === 'string') {
    return value.trim()
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return ''
}

function hasVisibleValue(value: unknown) {
  if (Array.isArray(value)) {
    return value.some((item) => printableValue(item).length > 0)
  }

  return printableValue(value).length > 0
}

function evidenceCoverageValue(bundle: Record<string, unknown>) {
  const entries = Object.values(bundle)
  if (entries.length === 0) {
    return 0
  }

  const present = entries.filter((value) => hasVisibleValue(value)).length
  return Math.round((present / entries.length) * 100)
}

function evidenceCoverageRows(
  selectedSuggestion: SuggestionRecord,
  locale: 'en' | 'zh',
): EvidenceCoverageRow[] {
  const rows: Array<{
    label: string
    bundle: Record<string, unknown>
    fallback: string
  }> = [
    {
      label: locale === 'zh' ? '拓扑' : 'Topology',
      bundle: selectedSuggestion.evidenceBundle.topology,
      fallback:
        locale === 'zh' ? '当前事件未提供拓扑上下文。' : 'No topology context was attached to this incident.',
    },
    {
      label: locale === 'zh' ? '设备' : 'Device',
      bundle: selectedSuggestion.evidenceBundle.device,
      fallback:
        locale === 'zh' ? '当前事件未提供设备上下文。' : 'No device context was attached to this incident.',
    },
    {
      label: locale === 'zh' ? '变更 / 历史' : 'Change / Historical',
      bundle: {
        ...selectedSuggestion.evidenceBundle.change,
        ...selectedSuggestion.evidenceBundle.historical,
      },
      fallback:
        locale === 'zh'
          ? '当前事件未提供变更或历史上下文。'
          : 'No change or historical context was attached to this incident.',
    },
  ]

  return rows.map(({ label, bundle, fallback }) => {
    const firstFacts = Object.entries(bundle)
      .filter(([, value]) => hasVisibleValue(value))
      .slice(0, 2)
      .map(([key, value]) => `${key}=${printableValue(value)}`)

    return {
      label,
      value: evidenceCoverageValue(bundle),
      detail: firstFacts.join(' · ') || fallback,
    }
  })
}

function incidentEnvelopeRows(
  selectedSuggestion: SuggestionRecord,
  locale: 'en' | 'zh',
): IncidentEnvelopeRow[] {
  const sampleIds = selectedSuggestion.context.clusterSampleAlertIds
    .slice(0, 2)
    .join(', ')

  return [
    {
      label: locale === 'zh' ? '首个告警' : 'first alert',
      value: formatMaybeTimestamp(selectedSuggestion.context.clusterFirstAlertTs) || '-',
    },
    {
      label: locale === 'zh' ? '最后告警' : 'last alert',
      value: formatMaybeTimestamp(selectedSuggestion.context.clusterLastAlertTs) || '-',
    },
    {
      label: locale === 'zh' ? '样本告警' : 'sample alerts',
      value: sampleIds || selectedSuggestion.alertId,
    },
    {
      label: locale === 'zh' ? '动作数' : 'actions',
      value: String(selectedSuggestion.recommendedActions.length),
    },
    {
      label: locale === 'zh' ? '假设数' : 'hypotheses',
      value: String(selectedSuggestion.hypotheses.length),
    },
    {
      label: locale === 'zh' ? '推理提供方' : 'provider',
      value: selectedSuggestion.context.provider,
    },
  ]
}

function processTraceSegments(
  selectedSuggestion: SuggestionRecord,
  locale: 'en' | 'zh',
): ProcessTraceSegment[] {
  const stageLookup = new Map(
    (selectedSuggestion.stageTelemetry ?? []).map((item) => [item.stageId, item]),
  )
  const orderedStages: Array<{
    id: string
    enTitle: string
    zhTitle: string
    tone: ProcessTraceSegment['tone']
    fallbackValue: string
  }> = [
    {
      id: 'lcore-source',
      enTitle: 'Source signal',
      zhTitle: '源信号',
      tone: 'raw',
      fallbackValue: locale === 'zh' ? '实时来源' : 'live source',
    },
    {
      id: 'ingest',
      enTitle: 'Edge parse',
      zhTitle: '边缘解析',
      tone: 'raw',
      fallbackValue: locale === 'zh' ? '已解析' : 'parsed',
    },
    {
      id: 'raw-topic',
      enTitle: 'Raw topic',
      zhTitle: '原始主题',
      tone: 'raw',
      fallbackValue: locale === 'zh' ? '已进入流' : 'streamed',
    },
    {
      id: 'correlator',
      enTitle: 'Rule trigger',
      zhTitle: '规则触发',
      tone: 'alert',
      fallbackValue: locale === 'zh' ? '规则判定' : 'rule decision',
    },
    {
      id: 'alerts-topic',
      enTitle: 'Alert emitted',
      zhTitle: '告警发出',
      tone: 'alert',
      fallbackValue: locale === 'zh' ? '告警总线' : 'alert bus',
    },
    {
      id: 'cluster-window',
      enTitle: 'Cluster gate',
      zhTitle: '聚合门槛',
      tone: 'alert',
      fallbackValue: locale === 'zh' ? '门槛观测' : 'gate watch',
    },
    {
      id: 'aiops-agent',
      enTitle: 'AIOps inference',
      zhTitle: 'AIOps 推理',
      tone: 'suggestion',
      fallbackValue: locale === 'zh' ? '生成建议' : 'suggestion build',
    },
    {
      id: 'suggestions-topic',
      enTitle: 'Suggestion emitted',
      zhTitle: '建议发出',
      tone: 'suggestion',
      fallbackValue: locale === 'zh' ? '建议输出' : 'suggestion output',
    },
    {
      id: 'remediation',
      enTitle: 'Operator boundary',
      zhTitle: '人工边界',
      tone: 'neutral',
      fallbackValue: locale === 'zh' ? '人工处理' : 'manual boundary',
    },
  ]

  return orderedStages
    .map((stage) => {
      const telemetry = stageLookup.get(stage.id)
      if (!telemetry && stage.id !== 'remediation') {
        return null
      }

      const value =
        telemetry?.mode === 'duration'
          ? formatPreciseDurationMs(telemetry.durationMs)
          : telemetry?.value ||
            formatMaybeTimestamp(telemetry?.endedAt ?? telemetry?.startedAt, 'time') ||
            stage.fallbackValue

      return {
        id: stage.id,
        title: locale === 'zh' ? stage.zhTitle : stage.enTitle,
        detail:
          telemetry?.label ??
          (locale === 'zh' ? '运行阶段' : 'control-loop stage'),
        value,
        tone: stage.tone,
        state: telemetry?.state ?? (stage.id === 'remediation' ? 'planned' : 'steady'),
      }
    })
    .filter((item): item is ProcessTraceSegment => item !== null)
}

function clusterWatchRows(
  snapshot: ControlLoopSnapshot,
  selectedSuggestion: SuggestionRecord,
) {
  const matchesSelection = (item: ControlLoopSnapshot['clusterWatch'][number]) =>
    item.service === selectedSuggestion.context.service &&
    item.device === selectedSuggestion.context.srcDeviceKey

  return snapshot.clusterWatch
    .slice()
    .sort((left, right) => {
      const leftMatch = matchesSelection(left) ? 1 : 0
      const rightMatch = matchesSelection(right) ? 1 : 0
      if (leftMatch !== rightMatch) {
        return rightMatch - leftMatch
      }

      const leftRatio = left.target > 0 ? left.progress / left.target : 0
      const rightRatio = right.target > 0 ? right.progress / right.target : 0
      return rightRatio - leftRatio
    })
}

function latencyRows(selectedSuggestion: SuggestionRecord, locale: 'en' | 'zh'): LatencyRow[] {
  const stageLookup = new Map(
    (selectedSuggestion.stageTelemetry ?? []).map((item) => [item.stageId, item]),
  )
  const orderedStages: Array<[string, string, string]> = [
    ['correlator', 'edge -> alert', '边缘到告警'],
    ['cluster-window', 'cluster gate', '聚合门槛'],
    ['aiops-agent', 'alert -> suggestion', '告警到建议'],
  ]

  return orderedStages
    .map(([stageId, enLabel, zhLabel]) => {
      const telemetry = stageLookup.get(stageId)
      if (!telemetry || telemetry.durationMs === null || telemetry.durationMs === undefined) {
        return null
      }

      if (
        stageId === 'cluster-window' &&
        telemetry.mode === 'gate' &&
        telemetry.durationMs <= 0
      ) {
        return null
      }

      return {
        label: locale === 'zh' ? zhLabel : enLabel,
        durationMs: telemetry.durationMs,
      }
    })
    .filter((item): item is LatencyRow => item !== null)
}

function summaryLine(telemetry: StageTelemetry[] | undefined, locale: 'en' | 'zh') {
  const measured = (telemetry ?? []).filter(
    (item) =>
      item.mode === 'duration' &&
      item.durationMs !== null &&
      item.durationMs !== undefined,
  )
  const totalMs = measured.reduce((sum, item) => sum + (item.durationMs ?? 0), 0)

  if (measured.length > 0) {
    return locale === 'zh'
      ? `当前可测阶段累计耗时 ${formatPreciseDurationMs(totalMs)}`
      : `Measured transition budget: ${formatPreciseDurationMs(totalMs)}`
  }

  return locale === 'zh'
    ? '只有带真实阶段遥测的建议，才会在这里显示耗时。'
    : 'Measured transition budget appears only when the selected suggestion carries real stage telemetry.'
}

export function ControlLoopVisualPanels({
  snapshot,
  selectedSuggestion,
  locale,
}: ControlLoopVisualPanelsProps) {
  const deviceLabel =
    printableValue(selectedSuggestion.evidenceBundle.device.device_name) ||
    selectedSuggestion.context.srcDeviceKey
  const latency = latencyRows(selectedSuggestion, locale)
  const processTrace = processTraceSegments(selectedSuggestion, locale)
  const evidenceCoverage = evidenceCoverageRows(selectedSuggestion, locale)
  const incidentEnvelope = incidentEnvelopeRows(selectedSuggestion, locale)
  const clusterRows = clusterWatchRows(snapshot, selectedSuggestion)
  const latencyMax = Math.max(...latency.map((row) => row.durationMs), 1)
  const hasCadence =
    snapshot.cadence.labels.length > 0 &&
    (snapshot.cadence.alerts.length > 0 || snapshot.cadence.suggestions.length > 0)
  const hasEvidenceCoverage = evidenceCoverage.length > 0
  const hasClusterWatch = clusterRows.length > 0
  const cadenceMax = Math.max(
    ...snapshot.cadence.alerts,
    ...snapshot.cadence.suggestions,
    1,
  )
  const alertPolyline = buildPolyline(snapshot.cadence.alerts, 560, 220)
  const suggestionPolyline = buildPolyline(snapshot.cadence.suggestions, 560, 220)

  return (
    <section className="section visual-strip visual-strip-expanded">
      <div className="section-header">
        <div>
          <h2 className="section-title">
            {locale === 'zh' ? '支撑可视化' : 'Supporting Visuals'}
          </h2>
          <span className="section-subtitle">
            {locale === 'zh'
              ? '这块不再依赖外部图表懒加载。节奏、证据、阶段成本和重复路径都会直接稳定显示。'
              : 'This field no longer depends on lazy chart chunks. Cadence, evidence, stage cost, and repeated paths render directly.'}
          </span>
        </div>
        <span className="section-kicker">
          {locale === 'zh' ? '直接可见 / 不再空白' : 'always visible / no blank field'}
        </span>
      </div>

      <div className="signal-visual-grid signal-visual-grid-expanded">
        <article className="chart-card chart-card-dark">
          <div className="chart-meta">
            <strong>{locale === 'zh' ? '节奏对齐' : 'Cadence alignment'}</strong>
            <p>
              {locale === 'zh'
                ? '同一时间窗内，建议产出有没有跟上告警到达。'
                : 'Whether suggestion emission stays close to alert arrival inside the same window.'}
            </p>
          </div>

          {hasCadence ? (
            <div className="sparkline-shell">
              <div className="sparkline-legend">
                <span className="sparkline-chip tone-alert">
                  {locale === 'zh' ? '告警' : 'alerts'}
                </span>
                <span className="sparkline-chip tone-suggestion">
                  {locale === 'zh' ? '建议' : 'suggestions'}
                </span>
                <strong>
                  {locale === 'zh' ? '峰值' : 'peak'} {cadenceMax}
                </strong>
              </div>

              <svg
                className="sparkline-svg"
                viewBox="0 0 560 220"
                preserveAspectRatio="none"
                role="img"
                aria-label={locale === 'zh' ? '节奏折线' : 'cadence sparkline'}
              >
                {[0, 1, 2, 3].map((index) => {
                  const y = 18 + index * 46
                  return (
                    <line
                      key={y}
                      x1="20"
                      y1={y}
                      x2="540"
                      y2={y}
                      className="sparkline-grid"
                    />
                  )
                })}

                <polyline points={alertPolyline} className="sparkline-path tone-alert" />
                <polyline
                  points={suggestionPolyline}
                  className="sparkline-path tone-suggestion"
                />

                {snapshot.cadence.alerts.map((value, index) => {
                  const x =
                    snapshot.cadence.alerts.length > 1
                      ? 20 + ((560 - 40) / (snapshot.cadence.alerts.length - 1)) * index
                      : 280
                  const y = 12 + (220 - 36) - (value / cadenceMax) * (220 - 36)
                  return (
                    <rect
                      key={`a-${snapshot.cadence.labels[index]}`}
                      x={x - 3.5}
                      y={y - 3.5}
                      width="7"
                      height="7"
                      className="sparkline-point tone-alert"
                    />
                  )
                })}

                {snapshot.cadence.suggestions.map((value, index) => {
                  const x =
                    snapshot.cadence.suggestions.length > 1
                      ? 20 + ((560 - 40) / (snapshot.cadence.suggestions.length - 1)) * index
                      : 280
                  const y = 12 + (220 - 36) - (value / cadenceMax) * (220 - 36)
                  return (
                    <circle
                      key={`s-${snapshot.cadence.labels[index]}`}
                      cx={x}
                      cy={y}
                      r="4"
                      className="sparkline-point tone-suggestion"
                    />
                  )
                })}
              </svg>

              <div className="sparkline-axis">
                {snapshot.cadence.labels.map((label) => (
                  <span key={label}>{label}</span>
                ))}
              </div>
            </div>
          ) : (
            <div className="chart-empty">
              <strong>{locale === 'zh' ? '节奏数据暂不可用' : 'Cadence data unavailable'}</strong>
              <p>
                {locale === 'zh'
                  ? '当前快照没有提供足够的告警 / 建议节奏序列，所以这里先明确显示空态。'
                  : 'The current snapshot does not carry enough alert/suggestion cadence samples, so this panel shows an explicit empty state.'}
              </p>
            </div>
          )}
        </article>

        <article className="chart-card">
          <div className="chart-meta">
            <strong>{locale === 'zh' ? '证据覆盖' : 'Evidence coverage'}</strong>
            <p>
              {locale === 'zh'
                ? '当前事件路径上的拓扑、设备和变化上下文附着比例。'
                : 'Topology, device, and change context rates on the current incident path.'}
            </p>
          </div>

          {hasEvidenceCoverage ? (
            <div className="coverage-stack">
              {evidenceCoverage.map((row) => (
                <article key={row.label} className="coverage-row">
                  <div className="coverage-meta">
                    <strong>{row.label}</strong>
                    <span>{row.value}%</span>
                  </div>
                  <div className="coverage-bar" aria-hidden="true">
                    <span style={{ width: `${Math.max(6, row.value)}%` }} />
                  </div>
                  <p>{row.detail}</p>
                </article>
              ))}
            </div>
          ) : (
            <div className="chart-empty">
              <strong>
                {locale === 'zh' ? '证据覆盖暂不可用' : 'Evidence coverage unavailable'}
              </strong>
              <p>
                {locale === 'zh'
                  ? '当前事件没有足够的拓扑、设备或变化上下文。'
                  : 'The current incident does not carry enough topology, device, or change context.'}
              </p>
            </div>
          )}
        </article>

        <article className="chart-card">
          <div className="chart-meta">
            <strong>{locale === 'zh' ? '阶段耗时' : 'Stage latency'}</strong>
            <p>{summaryLine(selectedSuggestion.stageTelemetry, locale)}</p>
          </div>

          {latency.length > 0 ? (
            <div className="latency-stack">
              {latency.map((row) => (
                <article key={row.label} className="latency-row">
                  <div className="latency-meta">
                    <strong>{row.label}</strong>
                    <span>{formatPreciseDurationMs(row.durationMs)}</span>
                  </div>
                  <div className="latency-bar" aria-hidden="true">
                    <span style={{ width: `${(row.durationMs / latencyMax) * 100}%` }} />
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="chart-empty">
              <strong>
                {locale === 'zh' ? '阶段耗时暂不可用' : 'Latency telemetry unavailable'}
              </strong>
              <p>
                {locale === 'zh'
                  ? '当选中的建议没有真实阶段遥测时，这里明确显示不可用，而不是整块空掉。'
                  : 'When the selected suggestion does not carry measured stage telemetry, this panel explicitly shows that state instead of disappearing.'}
              </p>
            </div>
          )}
        </article>

        <article className="chart-card">
          <div className="chart-meta">
            <strong>{locale === 'zh' ? '历史事件档案' : 'Historical incident dossier'}</strong>
            <p>
              {locale === 'zh'
                ? '把当前历史事件的时间窗、样本和动作规模直接摊开，避免只剩一条摘要。'
                : 'Expose the selected incident window, samples, and action scale directly instead of collapsing everything into one summary.'}
            </p>
          </div>

          <div className="reading-grid incident-envelope-grid">
            {incidentEnvelope.map((row) => (
              <article key={row.label} className="reading-card">
                <span>{row.label}</span>
                <strong>{row.value}</strong>
              </article>
            ))}
          </div>
        </article>

        <article className="chart-card">
          <div className="chart-meta">
            <strong>{locale === 'zh' ? '重复路径监视' : 'Repeated path watch'}</strong>
            <p>
              {locale === 'zh'
                ? '哪些路径最接近从单次告警升级成可聚合的重复模式。'
                : 'Which paths are closest to becoming a repeated pattern instead of a one-off alert.'}
            </p>
          </div>

          {hasClusterWatch ? (
            <div className="cluster-watch-stack">
              {clusterRows.map((item) => {
                const ratio = item.target > 0 ? (item.progress / item.target) * 100 : 0
                const isSelectedPath =
                  item.service === selectedSuggestion.context.service &&
                  item.device === selectedSuggestion.context.srcDeviceKey

                return (
                  <article
                    key={item.key}
                    className={`cluster-watch-row ${isSelectedPath ? 'is-selected' : ''}`}
                  >
                    <div className="cluster-watch-head">
                      <div>
                        <strong>{item.service}</strong>
                        <span>{item.device}</span>
                      </div>
                      <span>
                        {item.progress}/{item.target}
                      </span>
                    </div>
                    <div className="cluster-watch-bar" aria-hidden="true">
                      <span style={{ width: `${Math.max(8, ratio)}%` }} />
                    </div>
                    <p>{item.note}</p>
                  </article>
                )
              })}
            </div>
          ) : (
            <div className="chart-empty">
              <strong>
                {locale === 'zh' ? '重复路径监视暂不可用' : 'Repeated-path watch unavailable'}
              </strong>
              <p>
                {locale === 'zh'
                  ? '当前快照没有可供比较的重复路径统计。'
                  : 'The current snapshot does not carry repeated-path watch rows yet.'}
              </p>
            </div>
          )}
        </article>

        <article className="chart-card chart-card-span-2">
          <div className="chart-meta">
            <strong>{locale === 'zh' ? '事件流程树与关键读数' : 'Incident process tree and readings'}</strong>
            <p>
              {locale === 'zh'
                ? '把当前历史事件从信号进入、规则触发到建议产出完整摊开成流程树，再把关键读数直接并排显示。'
                : 'Lay out the selected historical incident from source signal to suggestion emission as a process tree, then keep the few fields that drive judgment visible.'}
            </p>
          </div>

          {processTrace.length > 0 ? (
            <div className="process-trace">
              {processTrace.map((segment, index) => (
                <div key={segment.id} className="process-trace-segment">
                  <article
                    className={`process-trace-card tone-${segment.tone} state-${segment.state}`}
                  >
                    <span>{segment.detail}</span>
                    <strong>{segment.title}</strong>
                    <p>{segment.value}</p>
                  </article>
                  {index < processTrace.length - 1 ? (
                    <div className="process-trace-connector" aria-hidden="true">
                      <span />
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          ) : (
            <div className="chart-empty chart-empty-inline">
              <strong>
                {locale === 'zh'
                  ? '当前事件还没有阶段轨迹'
                  : 'No stage trace is attached to this incident yet'}
              </strong>
              <p>
                {locale === 'zh'
                  ? '当历史事件缺少阶段遥测时，这里会明确显示空态，而不是整块留白。'
                  : 'When stage telemetry is missing, this panel stays explicit instead of collapsing into a blank field.'}
              </p>
            </div>
          )}

          <div className="reading-grid">
            <article className="reading-card">
              <span>{locale === 'zh' ? '服务' : 'service'}</span>
              <strong>{selectedSuggestion.context.service}</strong>
            </article>
            <article className="reading-card">
              <span>{locale === 'zh' ? '设备' : 'device'}</span>
              <strong>{deviceLabel}</strong>
            </article>
            <article className="reading-card">
              <span>{locale === 'zh' ? '作用域' : 'scope'}</span>
              <strong>{selectedSuggestion.scope}</strong>
            </article>
            <article className="reading-card">
              <span>{locale === 'zh' ? '置信度' : 'confidence'}</span>
              <strong>{selectedSuggestion.confidenceLabel}</strong>
            </article>
            <article className="reading-card">
              <span>{locale === 'zh' ? '近一小时相似告警' : 'recent similar / 1h'}</span>
              <strong>{numericFromValue(selectedSuggestion.context.recentSimilar1h)}</strong>
            </article>
            <article className="reading-card">
              <span>{locale === 'zh' ? '聚合门槛' : 'cluster gate'}</span>
              <strong>
                {selectedSuggestion.stageTelemetry?.find(
                  (item) => item.stageId === 'cluster-window',
                )?.value ??
                  (selectedSuggestion.context.clusterWindowSec > 0
                    ? `${selectedSuggestion.context.clusterSize}/${selectedSuggestion.context.clusterWindowSec}s`
                    : locale === 'zh'
                      ? '尚未达到聚合门槛'
                      : 'not yet cluster-legible')}
              </strong>
            </article>
          </div>
        </article>
      </div>
    </section>
  )
}
