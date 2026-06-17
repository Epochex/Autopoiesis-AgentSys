import type { CompareSampleUnit } from '../../types'

interface CompareSampleMatrixProps {
  locale: 'en' | 'zh'
  samples: CompareSampleUnit[]
  selectedSampleId: string
  onSampleFocus: (sampleId: string) => void
  onInspectSample: (sampleId: string) => void
}

function percentLabel(locale: 'en' | 'zh', value: number | null) {
  if (value === null) {
    return locale === 'zh' ? '待接入' : 'pending'
  }
  return `${Math.round(value * 100)}%`
}

function deltaLabel(locale: 'en' | 'zh', baseline: number | null, llm: number | null) {
  if (baseline === null || llm === null) {
    return locale === 'zh' ? '待接入' : 'pending'
  }
  const delta = llm - baseline
  return locale === 'zh'
    ? `${delta > 0 ? '+' : ''}${Math.round(delta * 100)}`
    : `${delta > 0 ? '+' : ''}${Math.round(delta * 100)}`
}

function msLabel(locale: 'en' | 'zh', value: number | null) {
  if (value === null) {
    return locale === 'zh' ? '待接入' : 'pending'
  }
  return `${Math.round(value)}ms`
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

function StripRail(props: {
  locale: 'en' | 'zh'
  label: string
  baseline: number | null
  llm: number | null
}) {
  return (
    <div className="compare-strip-rail">
      <span>{props.label}</span>
      <div className="compare-strip-rail-track">
        <i
          className="compare-strip-rail-fill baseline"
          style={{ width: `${Math.max(4, (props.baseline ?? 0) * 100)}%` }}
        />
        <i
          className="compare-strip-rail-fill llm"
          style={{ width: `${Math.max(4, (props.llm ?? 0) * 100)}%` }}
        />
      </div>
      <strong>{deltaLabel(props.locale, props.baseline, props.llm)}</strong>
    </div>
  )
}

export function CompareSampleMatrix({
  locale,
  samples,
  selectedSampleId,
  onSampleFocus,
  onInspectSample,
}: CompareSampleMatrixProps) {
  const selectedSample =
    samples.find((sample) => sample.id === selectedSampleId) ?? samples[0]

  return (
    <section className="section compare-shell-section">
      <div className="compare-section-head">
        <div>
          <h3 className="compare-section-title">
            {locale === 'zh' ? '样本比较墙' : 'Bundle Comparison Wall'}
          </h3>
        </div>
        <span className="compare-inline-note">
          {locale === 'zh' ? `${samples.length} 个比较单元` : `${samples.length} comparison units`}
        </span>
      </div>

      {selectedSample ? (
        <div className="compare-strip-summary">
          <span className="compare-strip-summary-index">
            {String(
              samples.findIndex((sample) => sample.id === selectedSample.id) + 1,
            ).padStart(2, '0')}
          </span>
          <strong>{selectedSample.bundleId}</strong>
          <span>{selectedSample.ruleId}</span>
          <span>{selectedSample.service}</span>
          <span>{selectedSample.device}</span>
          <span>{selectedSample.replay.runId}</span>
          <button
            className="compare-action compare-summary-action"
            type="button"
            onClick={() => onInspectSample(selectedSample.id)}
          >
            {locale === 'zh' ? '打开审计' : 'Open Detail'}
          </button>
        </div>
      ) : null}

      <div className="compare-strip-wall">
        {samples.map((sample, index) => (
          <article
            key={sample.id}
            className={`compare-strip-row ${sample.id === selectedSampleId ? 'is-selected' : ''}`}
            onClick={() => onSampleFocus(sample.id)}
          >
            <div className="compare-strip-index">
              <span>{String(index + 1).padStart(2, '0')}</span>
              <i />
            </div>

            <div className="compare-strip-head">
              <div>
                <strong>{sample.bundleId}</strong>
                <span>{sample.ruleId}</span>
              </div>
              <div className="compare-strip-meta">
                <span>{sample.service}</span>
                <span>{sample.device}</span>
                <span>{sample.severity}</span>
              </div>
            </div>

            <div className="compare-strip-metrics">
              <StripRail
                locale={locale}
                label={locale === 'zh' ? '解释' : 'Explain'}
                baseline={sample.baseline.metrics.explanationCompleteness}
                llm={sample.llm.metrics.explanationCompleteness}
              />
              <StripRail
                locale={locale}
                label={locale === 'zh' ? '动作' : 'Action'}
                baseline={sample.baseline.metrics.actionability}
                llm={sample.llm.metrics.actionability}
              />
              <StripRail
                locale={locale}
                label={locale === 'zh' ? '绑定' : 'Binding'}
                baseline={sample.baseline.metrics.evidenceBinding}
                llm={sample.llm.metrics.evidenceBinding}
              />
            </div>

            <div className="compare-strip-timing">
              <div className="compare-strip-timing-block">
                <span>{locale === 'zh' ? '基线' : 'B'}</span>
                <strong>{statusLabel(locale, sample.baseline.status)}</strong>
              </div>
              <div className="compare-strip-timing-block">
                <span>LLM</span>
                <strong>{statusLabel(locale, sample.llm.status)}</strong>
              </div>
              <div className="compare-strip-timing-block">
                <span>{locale === 'zh' ? '时延' : 'Latency'}</span>
                <strong>
                  {msLabel(locale, sample.baseline.timing.latencyMs)} / {msLabel(locale, sample.llm.timing.latencyMs)}
                </strong>
              </div>
              <div className="compare-strip-timing-block">
                <span>{locale === 'zh' ? '虚构率' : 'Hallucination'}</span>
                <strong>{percentLabel(locale, sample.llm.metrics.hallucinationRate)}</strong>
              </div>
            </div>

            <button
              className="compare-action compare-strip-action"
              type="button"
              onClick={(event) => {
                event.stopPropagation()
                onInspectSample(sample.id)
              }}
            >
              {locale === 'zh' ? '详情' : 'Inspect'}
            </button>
          </article>
        ))}
      </div>
    </section>
  )
}
