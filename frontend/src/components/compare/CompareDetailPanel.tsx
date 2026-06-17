import { useEffect } from 'react'
import { exportCompareSampleDetail } from '../../utils/compareExport'
import type { CompareProviderEvaluation, CompareSampleUnit } from '../../types'

interface CompareDetailPanelProps {
  locale: 'en' | 'zh'
  isOpen: boolean
  sample: CompareSampleUnit | undefined
  onClose: () => void
}

function metricLabel(locale: 'en' | 'zh', value: number | null) {
  if (value === null) {
    return locale === 'zh' ? '待接入' : 'pending'
  }
  return `${Math.round(value * 100)}%`
}

function timingLabel(locale: 'en' | 'zh', value: number | null, unit: 'ms' | 'usd') {
  if (value === null) {
    return locale === 'zh' ? '待接入' : 'pending'
  }
  return unit === 'ms' ? `${Math.round(value)} ms` : `$${value.toFixed(3)}`
}

function statusLabel(locale: 'en' | 'zh', status: string) {
  if (locale !== 'zh') {
    return status
  }
  if (status === 'ready') return '就绪'
  if (status === 'placeholder') return '占位'
  if (status === 'failed') return '失败'
  if (status === 'unavailable') return '未接入'
  return status
}

function EvaluationColumn(props: {
  locale: 'en' | 'zh'
  title: string
  evaluation: CompareProviderEvaluation
  accent: 'baseline' | 'llm'
}) {
  return (
    <article className={`compare-detail-column compare-detail-column-${props.accent}`}>
      <header className="compare-detail-column-head">
        <div>
          <p>{props.title}</p>
          <strong>{props.evaluation.providerName}</strong>
        </div>
        <span className={`compare-status-chip status-${props.evaluation.status}`}>
          {statusLabel(props.locale, props.evaluation.status)}
        </span>
      </header>

      <div className="compare-detail-metrics">
        <div>
          <span>{props.locale === 'zh' ? '完整度' : 'Completeness'}</span>
          <strong>{metricLabel(props.locale, props.evaluation.metrics.explanationCompleteness)}</strong>
        </div>
        <div>
          <span>{props.locale === 'zh' ? '可执行性' : 'Actionability'}</span>
          <strong>{metricLabel(props.locale, props.evaluation.metrics.actionability)}</strong>
        </div>
        <div>
          <span>{props.locale === 'zh' ? '绑定率' : 'Binding'}</span>
          <strong>{metricLabel(props.locale, props.evaluation.metrics.evidenceBinding)}</strong>
        </div>
        <div>
          <span>{props.locale === 'zh' ? '稳定性' : 'Stability'}</span>
          <strong>{metricLabel(props.locale, props.evaluation.metrics.stability)}</strong>
        </div>
        <div>
          <span>{props.locale === 'zh' ? '时延' : 'Latency'}</span>
          <strong>{timingLabel(props.locale, props.evaluation.timing.latencyMs, 'ms')}</strong>
        </div>
        <div>
          <span>{props.locale === 'zh' ? '成本' : 'Cost'}</span>
          <strong>{timingLabel(props.locale, props.evaluation.timing.estimatedCostUsd, 'usd')}</strong>
        </div>
      </div>

      <div className="compare-detail-output">
        {props.evaluation.outputBlocks.map((block) => (
          <section key={`${props.title}-${block.title}`} className="compare-output-block">
            <span>{block.title}</span>
            <ul>
              {block.lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </article>
  )
}

function EvidenceMappingCard(props: {
  locale: 'en' | 'zh'
  sample: CompareSampleUnit
}) {
  const references = [
    ...props.sample.baseline.evidenceReferences.map((reference) => ({
      ...reference,
      provider: 'Baseline',
    })),
    ...props.sample.llm.evidenceReferences.map((reference) => ({
      ...reference,
      provider: 'LLM',
    })),
  ]

  return (
    <article className="compare-evidence-card">
      <strong>{props.locale === 'zh' ? '证据映射' : 'Evidence Map'}</strong>
      <div className="compare-reference-map compare-reference-map-drawer">
        {references.map((reference) => (
          <div
            key={reference.id}
            className={`compare-reference-row ${reference.provider === 'Baseline' ? 'baseline' : 'llm'}`}
          >
            <span className="compare-reference-provider">
              {props.locale === 'zh' && reference.provider === 'Baseline'
                ? '基线'
                : reference.provider}
            </span>
            <span className="compare-reference-source">
              {reference.sourceSection}.{reference.sourceField}
            </span>
            <span className="compare-reference-line" />
            <span className="compare-reference-claim">{reference.claim}</span>
          </div>
        ))}
      </div>
    </article>
  )
}

export function CompareDetailPanel({
  locale,
  isOpen,
  sample,
  onClose,
}: CompareDetailPanelProps) {
  useEffect(() => {
    if (!isOpen) {
      return
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        onClose()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onClose])

  if (!sample) {
    return null
  }

  return (
    <div className={`compare-detail-overlay ${isOpen ? 'is-open' : ''}`} aria-hidden={!isOpen}>
      <button
        className="compare-detail-backdrop"
        type="button"
        onClick={onClose}
        aria-label={locale === 'zh' ? '关闭对比详情抽屉' : 'Close detail drawer'}
      />
      <aside
        className="compare-detail-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={locale === 'zh' ? '对比详情抽屉' : 'Comparison detail drawer'}
      >
        <div className="compare-detail-shell">
          <header className="compare-detail-drawer-head">
            <div>
              <p className="compare-eyebrow">{locale === 'zh' ? 'A/B 抽屉' : 'A/B Drawer'}</p>
              <h3 className="compare-section-title">
                {sample.bundleId} · {sample.ruleId}
              </h3>
              <p className="compare-section-copy compare-section-copy-tight">
                {sample.service} · {sample.path} · {sample.replay.runId}
              </p>
            </div>
            <div className="compare-detail-head-actions">
              <button
                className="compare-action"
                type="button"
                onClick={() => exportCompareSampleDetail(sample)}
              >
                {locale === 'zh' ? '导出详情' : 'Export Detail'}
              </button>
              <button className="compare-action compare-action-accent" type="button" onClick={onClose}>
                {locale === 'zh' ? '关闭' : 'Close'}
              </button>
            </div>
          </header>

          <div className="compare-detail-grid">
            <EvaluationColumn locale={locale} title={locale === 'zh' ? '规则基线' : 'Baseline'} evaluation={sample.baseline} accent="baseline" />
            <EvaluationColumn locale={locale} title="LLM" evaluation={sample.llm} accent="llm" />
          </div>

          <div className="compare-evidence-section compare-evidence-section-drawer">
            <article className="compare-evidence-card">
              <strong>{locale === 'zh' ? '证据包' : 'Evidence Bundle'}</strong>
              <div className="compare-evidence-grid">
                <section>
                  <span>{locale === 'zh' ? '拓扑' : 'Topology'}</span>
                  <ul>
                    {Object.entries(sample.evidenceBundle.topology).map(([key, value]) => (
                      <li key={key}>
                        <code>{key}</code> {Array.isArray(value) ? value.join(', ') : String(value)}
                      </li>
                    ))}
                  </ul>
                </section>
                <section>
                  <span>{locale === 'zh' ? '设备' : 'Device'}</span>
                  <ul>
                    {Object.entries(sample.evidenceBundle.device).map(([key, value]) => (
                      <li key={key}>
                        <code>{key}</code> {Array.isArray(value) ? value.join(', ') : String(value)}
                      </li>
                    ))}
                  </ul>
                </section>
                <section>
                  <span>{locale === 'zh' ? '变更' : 'Change'}</span>
                  <ul>
                    {Object.entries(sample.evidenceBundle.change).map(([key, value]) => (
                      <li key={key}>
                        <code>{key}</code> {Array.isArray(value) ? value.join(', ') : String(value)}
                      </li>
                    ))}
                  </ul>
                </section>
                <section>
                  <span>{locale === 'zh' ? '历史' : 'Historical'}</span>
                  <ul>
                    {Object.entries(sample.evidenceBundle.historical).map(([key, value]) => (
                      <li key={key}>
                        <code>{key}</code> {Array.isArray(value) ? value.join(', ') : String(value)}
                      </li>
                    ))}
                  </ul>
                </section>
              </div>
            </article>

            <EvidenceMappingCard locale={locale} sample={sample} />

            <article className="compare-evidence-card">
              <strong>{locale === 'zh' ? '无证据支撑内容' : 'Unsupported Claims'}</strong>
              {sample.llm.unsupportedClaims.length > 0 ? (
                <ul className="compare-unsupported-list">
                  {sample.llm.unsupportedClaims.map((claim) => (
                    <li key={claim.id}>
                      <span className={`compare-status-chip severity-${claim.severity}`}>
                        {locale === 'zh'
                          ? claim.severity === 'high'
                            ? '高'
                            : claim.severity === 'medium'
                              ? '中'
                              : '低'
                          : claim.severity}
                      </span>
                      <div>
                        <strong>{claim.claim}</strong>
                        <p>{claim.reason}</p>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="compare-empty-copy">
                  {locale === 'zh'
                    ? '当前样本没有记录无证据支撑内容。'
                    : 'No unsupported claim markers for the active sample.'}
                </p>
              )}
            </article>

            <article className="compare-evidence-card">
              <strong>{locale === 'zh' ? '审计备注' : 'Audit Notes'}</strong>
              <ul className="compare-note-list compare-note-list-tight">
                {sample.reviewNotes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
                {sample.baseline.notes.map((note) => (
                  <li key={`baseline-${note}`}>{locale === 'zh' ? '基线：' : 'Baseline: '}{note}</li>
                ))}
                {sample.llm.notes.map((note) => (
                  <li key={`llm-${note}`}>LLM: {note}</li>
                ))}
              </ul>

              <div className="compare-timing-ledger">
                <span>{sample.replay.replayLabel}</span>
                <span>{sample.replay.runId}</span>
                <span>{locale === 'zh' ? '输入' : 'input'} {sample.llm.timing.inputTokens ?? 0}</span>
                <span>{locale === 'zh' ? '输出' : 'output'} {sample.llm.timing.outputTokens ?? 0}</span>
                <span>{locale === 'zh' ? '成本' : 'cost'} {timingLabel(locale, sample.llm.timing.estimatedCostUsd, 'usd')}</span>
              </div>
            </article>
          </div>
        </div>
      </aside>
    </div>
  )
}
