import type { CompareDatasetFilters, CompareWorkbenchDataset } from '../../types'

interface CompareWorkbenchHeaderProps {
  locale: 'en' | 'zh'
  datasets: readonly CompareWorkbenchDataset[]
  activeDatasetId: string
  filters: CompareDatasetFilters
  replayOptions: Array<[string, string]>
  providerOptions: Array<[string, string]>
  severityOptions: Array<[string, string]>
  ruleOptions: Array<[string, string]>
  serviceOptions: Array<[string, string]>
  statusOptions: Array<[string, string]>
  onDatasetChange: (datasetId: string) => void
  onFilterChange: (patch: Partial<CompareDatasetFilters>) => void
  onExportJson: () => void
  onExportCsv: () => void
}

function SelectField(props: {
  locale: 'en' | 'zh'
  label: string
  value: string
  options: Array<[string, string]>
  onChange: (value: string) => void
}) {
  return (
    <label className="compare-control compare-control-select">
      <span className="compare-control-label">{props.label}</span>
      <select
        className="compare-select"
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
      >
        <option value="all">{props.locale === 'zh' ? '全部' : 'All'}</option>
        {props.options.map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
    </label>
  )
}

export function CompareWorkbenchHeader({
  locale,
  datasets,
  activeDatasetId,
  filters,
  replayOptions,
  providerOptions,
  severityOptions,
  ruleOptions,
  serviceOptions,
  statusOptions,
  onDatasetChange,
  onFilterChange,
  onExportJson,
  onExportCsv,
}: CompareWorkbenchHeaderProps) {
  const activeDataset =
    datasets.find((dataset) => dataset.id === activeDatasetId) ?? datasets[0]
  const datasetDescription =
    locale === 'zh'
      ? activeDataset.id === 'paired-eval-fixture'
        ? '同一条告警与证据包分别绑定模板基线输出和后续模型增强输出，用于对照评测。'
        : '当前阶段只有规则基线输出，模型侧保留为空位，用于后续接入同一套评测图形。'
      : activeDataset.description

  return (
    <section className="section compare-shell-section">
      <div className="compare-workbench-header">
        <div className="compare-header-title-block">
          <p className="compare-eyebrow">
            {locale === 'zh' ? '评测工作台' : 'Evaluation Workbench'}
          </p>
          <h2 className="compare-workbench-title">
            {locale === 'zh'
              ? '规则基线与模型增强对照评测'
              : 'Baseline Template vs Future LLM Evaluation'}
          </h2>
          <p className="compare-workbench-copy">{datasetDescription}</p>
        </div>

        <div className="compare-header-actions">
          <button className="compare-action" type="button" onClick={onExportJson}>
            {locale === 'zh' ? '导出 JSON' : 'Export JSON'}
          </button>
          <button className="compare-action compare-action-accent" type="button" onClick={onExportCsv}>
            {locale === 'zh' ? '导出 CSV' : 'Export CSV'}
          </button>
        </div>
      </div>

      <div className="compare-controls-grid">
        <label className="compare-control compare-control-select">
          <span className="compare-control-label">
            {locale === 'zh' ? '数据集' : 'Dataset'}
          </span>
          <select
            className="compare-select"
            value={activeDatasetId}
            onChange={(event) => onDatasetChange(event.target.value)}
          >
            {datasets.map((dataset) => (
              <option key={dataset.id} value={dataset.id}>
                {locale === 'zh'
                  ? dataset.id === 'paired-eval-fixture'
                    ? '成对评测样例'
                    : '基线单侧样例'
                  : dataset.label}
              </option>
            ))}
          </select>
        </label>

        <SelectField
          locale={locale}
          label={locale === 'zh' ? '回放批次' : 'Replay'}
          value={filters.replayId}
          options={replayOptions}
          onChange={(value) => onFilterChange({ replayId: value })}
        />

        <SelectField
          locale={locale}
          label={locale === 'zh' ? '模型提供器' : 'Provider'}
          value={filters.providerName}
          options={providerOptions}
          onChange={(value) => onFilterChange({ providerName: value })}
        />

        <SelectField
          locale={locale}
          label={locale === 'zh' ? '严重级别' : 'Severity'}
          value={filters.severity}
          options={severityOptions}
          onChange={(value) => onFilterChange({ severity: value })}
        />

        <SelectField
          locale={locale}
          label={locale === 'zh' ? '规则' : 'Rule'}
          value={filters.ruleId}
          options={ruleOptions}
          onChange={(value) => onFilterChange({ ruleId: value })}
        />

        <SelectField
          locale={locale}
          label={locale === 'zh' ? '服务' : 'Service'}
          value={filters.service}
          options={serviceOptions}
          onChange={(value) => onFilterChange({ service: value })}
        />

        <SelectField
          locale={locale}
          label={locale === 'zh' ? '行状态' : 'Row State'}
          value={filters.status}
          options={statusOptions
            .filter(([value]) => value !== 'all')
            .map(([value, label]) => [
              value,
              locale === 'zh'
                ? value === 'paired'
                  ? '成对就绪'
                  : value === 'placeholder'
                    ? '模型占位'
                    : '模型失败'
                : label,
            ])}
          onChange={(value) => onFilterChange({ status: value })}
        />

        <label className="compare-control compare-control-search">
          <span className="compare-control-label">
            {locale === 'zh' ? '搜索' : 'Search'}
          </span>
          <input
            className="compare-search"
            type="search"
            value={filters.query}
            onChange={(event) => onFilterChange({ query: event.target.value })}
            placeholder={locale === 'zh' ? 'bundle / alert / 规则 / 设备' : 'bundle / alert / rule / device'}
          />
        </label>
      </div>
    </section>
  )
}
