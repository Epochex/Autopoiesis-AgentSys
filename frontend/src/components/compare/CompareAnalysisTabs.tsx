import { CompareCharts } from './CompareCharts'
import type { CompareSampleUnit, CompareTabId } from '../../types'

function buildTabs(locale: 'en' | 'zh'): Array<{ id: CompareTabId; label: string; subtitle: string }> {
  if (locale === 'zh') {
    return [
      {
        id: 'explanation',
        label: '解释质量',
        subtitle: '同一 bundle 下的完整度与证据挂接情况。',
      },
      {
        id: 'action',
        label: '行动质量',
        subtitle: '建议动作是否具体，是否便于人工复核。',
      },
      {
        id: 'stability',
        label: '稳定性与审计性',
        subtitle: '重复回放一致性、复核路径与失败可见性。',
      },
      {
        id: 'timing',
        label: '成本与运行时',
        subtitle: '时延、成本与提供器运行行为。',
      },
    ]
  }

  return [
    {
      id: 'explanation',
      label: 'Explanation Quality',
      subtitle: 'Completeness and evidence attachment on the same bundle.',
    },
    {
      id: 'action',
      label: 'Action Quality',
      subtitle: 'Specificity and review usefulness of the suggested action path.',
    },
    {
      id: 'stability',
      label: 'Stability & Auditability',
      subtitle: 'Replay consistency, review path, and failure visibility.',
    },
    {
      id: 'timing',
      label: 'Cost & Latency',
      subtitle: 'Latency, spend, and provider-side latency and cost behavior.',
    },
  ]
}

interface CompareAnalysisTabsProps {
  locale: 'en' | 'zh'
  activeTab: CompareTabId
  samples: CompareSampleUnit[]
  selectedSampleId: string
  onTabChange: (tabId: CompareTabId) => void
  onInspectSample: (sampleId: string) => void
}

export function CompareAnalysisTabs({
  locale,
  activeTab,
  samples,
  selectedSampleId,
  onTabChange,
  onInspectSample,
}: CompareAnalysisTabsProps) {
  const tabs = buildTabs(locale)
  const activeTabMeta = tabs.find((tab) => tab.id === activeTab) ?? tabs[0]

  return (
    <section className="section compare-shell-section">
      <div className="compare-section-head">
        <div>
          <h3 className="compare-section-title">
            {locale === 'zh' ? '评测主图场' : 'Benchmark Field'}
          </h3>
        </div>
        <span className="compare-inline-note">{activeTabMeta.subtitle}</span>
      </div>

      <div
        className="compare-tab-strip"
        role="tablist"
        aria-label={locale === 'zh' ? '对比分析标签' : 'Compare analysis tabs'}
      >
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={`compare-tab ${tab.id === activeTab ? 'is-active' : ''}`}
            type="button"
            role="tab"
            aria-selected={tab.id === activeTab}
            onClick={() => onTabChange(tab.id)}
          >
            <span>{tab.label}</span>
            <small>{tab.subtitle}</small>
          </button>
        ))}
      </div>

      <CompareCharts
        locale={locale}
        activeTab={activeTab}
        samples={samples}
        selectedSampleId={selectedSampleId}
        onInspectSample={onInspectSample}
      />
    </section>
  )
}
