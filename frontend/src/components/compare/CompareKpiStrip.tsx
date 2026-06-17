import type { CompareKpiCard } from '../../types'

interface CompareKpiStripProps {
  cards: CompareKpiCard[]
  locale: 'en' | 'zh'
}

const zhLabels: Record<string, { label: string; note: string }> = {
  explanationCompleteness: {
    label: '解释完整度',
    note: '设备、服务、路径、变更与历史覆盖。',
  },
  actionability: {
    label: '建议可执行性',
    note: '处置动作是否具体并可直接落地。',
  },
  evidenceBinding: {
    label: '证据绑定率',
    note: '输出内容回指显式证据字段的程度。',
  },
  stability: {
    label: '稳定性',
    note: '重复回放下的一致性。',
  },
  hallucinationRate: {
    label: '虚构率',
    note: '输出中缺乏证据支撑的内容比例。',
  },
  latencyMs: {
    label: '时延',
    note: '单条建议生成时延。',
  },
  estimatedCostUsd: {
    label: '成本',
    note: '单条建议的估算调用成本。',
  },
  failureRate: {
    label: '失败率',
    note: '提供器失败占比。',
  },
}

export function CompareKpiStrip({ cards, locale }: CompareKpiStripProps) {
  function localizeDisplay(value: string) {
    if (locale !== 'zh') {
      return value
    }
    if (value === 'pending') {
      return '待接入'
    }
    if (value === 'waiting') {
      return '待接入'
    }
    return value
  }

  return (
    <section className="section compare-shell-section">
      <div className="compare-section-head">
        <div>
          <h3 className="compare-section-title">
            {locale === 'zh' ? '基准指标带' : 'Benchmark Strip'}
          </h3>
        </div>
      </div>

      <div className="compare-kpi-strip">
        {cards.map((card) => {
          const zh = zhLabels[card.id]
          return (
          <article key={card.id} className={`compare-kpi-card delta-${card.deltaState}`}>
            <div className="compare-kpi-head">
              <span className="compare-kpi-label">
                {locale === 'zh' && zh ? zh.label : card.label}
              </span>
              <span className="compare-kpi-delta">{localizeDisplay(card.deltaDisplay)}</span>
            </div>

            <div className="compare-kpi-values">
              <div>
                <span className="compare-kpi-side">{locale === 'zh' ? '基线' : 'Baseline'}</span>
                <strong>{localizeDisplay(card.ruleDisplay)}</strong>
              </div>
              <div>
                <span className="compare-kpi-side">LLM</span>
                <strong>{localizeDisplay(card.llmDisplay)}</strong>
              </div>
            </div>
            <span className="compare-kpi-micro-note">
              {locale === 'zh' && zh ? zh.note : card.note}
            </span>
          </article>
        )})}
      </div>
    </section>
  )
}
