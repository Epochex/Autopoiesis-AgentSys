import { formatCellMetric } from '../../data/compareWorkbench'
import type { CompareSampleUnit } from '../../types'

interface CompareSampleTableProps {
  locale: 'en' | 'zh'
  samples: CompareSampleUnit[]
  selectedSampleId: string
  sortKey: string
  sortDirection: 'asc' | 'desc'
  onRowSelect: (sampleId: string) => void
  onSortChange: (sortKey: string) => void
}

const columns = [
  { id: 'bundleId', label: 'bundle_id' },
  { id: 'alertId', label: 'alert_id' },
  { id: 'ruleId', label: 'rule_id' },
  { id: 'severity', label: 'severity' },
  { id: 'service', label: 'service' },
  { id: 'device', label: 'device' },
  { id: 'baselineStatus', label: 'baseline' },
  { id: 'llmStatus', label: 'llm' },
  { id: 'baseline.explanationCompleteness', label: 'exp. / B' },
  { id: 'llm.explanationCompleteness', label: 'exp. / L' },
  { id: 'baseline.actionability', label: 'act. / B' },
  { id: 'llm.actionability', label: 'act. / L' },
  { id: 'baseline.evidenceBinding', label: 'bind / B' },
  { id: 'llm.evidenceBinding', label: 'bind / L' },
  { id: 'llm.hallucinationRate', label: 'halluc.' },
  { id: 'baseline.latencyMs', label: 'lat. / B' },
  { id: 'llm.latencyMs', label: 'lat. / L' },
  { id: 'llm.estimatedCostUsd', label: 'cost' },
  { id: 'llm.failureRate', label: 'fail' },
  { id: 'replayLabel', label: 'replay' },
] as const

function localizedColumnLabel(
  locale: 'en' | 'zh',
  label: string,
) {
  if (locale !== 'zh') {
    return label
  }

  const map: Record<string, string> = {
    bundle_id: 'bundle_id',
    alert_id: '告警ID',
    rule_id: '规则ID',
    severity: '级别',
    service: '服务',
    device: '设备',
    baseline: '基线',
    llm: '模型',
    'exp. / B': '解释/基线',
    'exp. / L': '解释/模型',
    'act. / B': '动作/基线',
    'act. / L': '动作/模型',
    'bind / B': '绑定/基线',
    'bind / L': '绑定/模型',
    'halluc.': '虚构',
    'lat. / B': '时延/基线',
    'lat. / L': '时延/模型',
    cost: '成本',
    fail: '失败',
    replay: '回放',
  }
  return map[label] ?? label
}

function localizedStatus(locale: 'en' | 'zh', status: string) {
  if (locale !== 'zh') {
    return status
  }
  if (status === 'ready') return '就绪'
  if (status === 'placeholder') return '占位'
  if (status === 'failed') return '失败'
  if (status === 'unavailable') return '未接入'
  return status
}

function localizeCellValue(locale: 'en' | 'zh', value: string) {
  if (locale !== 'zh') {
    return value
  }
  if (value === 'pending') return '待接入'
  if (value === 'n/a') return '无'
  return value
}

function SortButton(props: {
  locale: 'en' | 'zh'
  columnId: string
  label: string
  sortKey: string
  sortDirection: 'asc' | 'desc'
  onSortChange: (sortKey: string) => void
}) {
  const isActive = props.sortKey === props.columnId
  return (
    <button
      type="button"
      className={`compare-sort-button ${isActive ? 'is-active' : ''}`}
      onClick={() => props.onSortChange(props.columnId)}
    >
      {localizedColumnLabel(props.locale, props.label)}
      <span>{isActive ? (props.sortDirection === 'asc' ? '↑' : '↓') : '·'}</span>
    </button>
  )
}

export function CompareSampleTable({
  locale,
  samples,
  selectedSampleId,
  sortKey,
  sortDirection,
  onRowSelect,
  onSortChange,
}: CompareSampleTableProps) {
  return (
    <div className="compare-table-wrap">
      <table className="compare-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.id}>
                <SortButton
                  locale={locale}
                  columnId={column.id}
                  label={column.label}
                  sortKey={sortKey}
                  sortDirection={sortDirection}
                  onSortChange={onSortChange}
                />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {samples.map((sample) => (
            <tr
              key={sample.id}
              className={sample.id === selectedSampleId ? 'is-selected' : ''}
              onClick={() => onRowSelect(sample.id)}
            >
              <td>{sample.bundleId}</td>
              <td>{sample.alertId}</td>
              <td>{sample.ruleId}</td>
              <td>{sample.severity}</td>
              <td>{sample.service}</td>
              <td>{sample.device}</td>
              <td>{localizedStatus(locale, sample.baseline.status)}</td>
              <td>{localizedStatus(locale, sample.llm.status)}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'baseline', 'explanationCompleteness'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'llm', 'explanationCompleteness'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'baseline', 'actionability'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'llm', 'actionability'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'baseline', 'evidenceBinding'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'llm', 'evidenceBinding'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'llm', 'hallucinationRate'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'baseline', 'latencyMs'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'llm', 'latencyMs'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'llm', 'estimatedCostUsd'))}</td>
              <td>{localizeCellValue(locale, formatCellMetric(sample, 'llm', 'failureRate'))}</td>
              <td>{sample.replay.replayLabel}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
