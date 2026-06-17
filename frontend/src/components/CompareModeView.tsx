import {
  startTransition,
  useDeferredValue,
  useMemo,
  useState,
} from 'react'
import './CompareModeView.css'
import {
  buildDefaultFilters,
  buildFilterOptions,
  compareWorkbenchDatasets,
  getCompareDataset,
  sortCompareSamples,
  summarizeCompareKpis,
  filterCompareSamples,
} from '../data/compareWorkbench'
import { CompareAnalysisTabs } from './compare/CompareAnalysisTabs'
import { CompareDetailPanel } from './compare/CompareDetailPanel'
import { CompareKpiStrip } from './compare/CompareKpiStrip'
import { CompareSampleMatrix } from './compare/CompareSampleMatrix'
import { CompareSampleTable } from './compare/CompareSampleTable'
import { CompareWorkbenchHeader } from './compare/CompareWorkbenchHeader'
import { exportCompareSamples } from '../utils/compareExport'
import type { CompareDatasetFilters, CompareTabId } from '../types'

interface CompareModeViewProps {
  locale: 'en' | 'zh'
}

export function CompareModeView({ locale }: CompareModeViewProps) {
  const [activeDatasetId, setActiveDatasetId] = useState(compareWorkbenchDatasets[0].id)
  const dataset = useMemo(() => getCompareDataset(activeDatasetId), [activeDatasetId])
  const [filters, setFilters] = useState<CompareDatasetFilters>(() => buildDefaultFilters(dataset))
  const [activeTab, setActiveTab] = useState<CompareTabId>('explanation')
  const [sortKey, setSortKey] = useState('bundleId')
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('asc')
  const [selectedSampleId, setSelectedSampleId] = useState(dataset.samples[0]?.id ?? '')
  const [isDetailOpen, setIsDetailOpen] = useState(false)

  const deferredQuery = useDeferredValue(filters.query)
  const effectiveFilters = useMemo(
    () => ({
      ...filters,
      query: deferredQuery,
    }),
    [deferredQuery, filters],
  )

  const filterOptions = useMemo(() => buildFilterOptions(dataset), [dataset])
  const filteredSamples = useMemo(
    () => filterCompareSamples(dataset, effectiveFilters),
    [dataset, effectiveFilters],
  )
  const sortedSamples = useMemo(
    () => sortCompareSamples(filteredSamples, sortKey, sortDirection),
    [filteredSamples, sortDirection, sortKey],
  )
  const kpiCards = useMemo(() => summarizeCompareKpis(sortedSamples), [sortedSamples])
  const activeSelectedSampleId = sortedSamples.some((sample) => sample.id === selectedSampleId)
    ? selectedSampleId
    : sortedSamples[0]?.id ?? ''
  const selectedSample = sortedSamples.find((sample) => sample.id === activeSelectedSampleId)

  function handleDatasetChange(datasetId: string) {
    startTransition(() => {
      const nextDataset = getCompareDataset(datasetId)
      setActiveDatasetId(datasetId)
      setFilters(buildDefaultFilters(nextDataset))
      setSelectedSampleId(nextDataset.samples[0]?.id ?? '')
      setActiveTab('explanation')
      setSortKey('bundleId')
      setSortDirection('asc')
      setIsDetailOpen(false)
    })
  }

  function handleFilterChange(patch: Partial<CompareDatasetFilters>) {
    startTransition(() => {
      setFilters((current) => ({
        ...current,
        ...patch,
      }))
    })
  }

  function handleSortChange(nextSortKey: string) {
    startTransition(() => {
      if (nextSortKey === sortKey) {
        setSortDirection((current) => (current === 'asc' ? 'desc' : 'asc'))
        return
      }
      setSortKey(nextSortKey)
      setSortDirection('asc')
    })
  }

  function handleSampleInspect(sampleId: string) {
    startTransition(() => {
      setSelectedSampleId(sampleId)
      setIsDetailOpen(true)
    })
  }

  return (
    <section className="page compare-workbench">
      <CompareWorkbenchHeader
        locale={locale}
        datasets={compareWorkbenchDatasets}
        activeDatasetId={activeDatasetId}
        filters={filters}
        replayOptions={filterOptions.replays}
        providerOptions={filterOptions.providers}
        severityOptions={filterOptions.severities}
        ruleOptions={filterOptions.rules}
        serviceOptions={filterOptions.services}
        statusOptions={filterOptions.statuses}
        onDatasetChange={handleDatasetChange}
        onFilterChange={handleFilterChange}
        onExportJson={() => exportCompareSamples(sortedSamples, dataset.label, 'json')}
        onExportCsv={() => exportCompareSamples(sortedSamples, dataset.label, 'csv')}
      />

      <CompareKpiStrip cards={kpiCards} locale={locale} />

      <CompareAnalysisTabs
        locale={locale}
        activeTab={activeTab}
        samples={sortedSamples}
        selectedSampleId={activeSelectedSampleId}
        onTabChange={(tabId) => startTransition(() => setActiveTab(tabId))}
        onInspectSample={handleSampleInspect}
      />

      <CompareSampleMatrix
        locale={locale}
        samples={sortedSamples}
        selectedSampleId={activeSelectedSampleId}
        onSampleFocus={(sampleId) => setSelectedSampleId(sampleId)}
        onInspectSample={handleSampleInspect}
      />

      <section className="section compare-shell-section compare-ledger-shell">
        <details className="compare-ledger-details">
          <summary className="compare-ledger-summary">
            <div>
              <h3 className="compare-section-title">
                {locale === 'zh' ? '审计账本' : 'Audit Ledger'}
              </h3>
              <p className="compare-section-copy compare-section-copy-tight">
                {locale === 'zh'
                  ? '完整可排序行账本保留在紧凑矩阵之后。'
                  : 'Full sortable row ledger kept behind the compact matrix.'}
              </p>
            </div>
            <span className="compare-inline-note">
              {locale === 'zh' ? `${sortedSamples.length} 行` : `${sortedSamples.length} rows`}
            </span>
          </summary>

          <CompareSampleTable
            locale={locale}
            samples={sortedSamples}
            selectedSampleId={activeSelectedSampleId}
            sortKey={sortKey}
            sortDirection={sortDirection}
            onRowSelect={handleSampleInspect}
            onSortChange={handleSortChange}
          />
        </details>
      </section>

      <CompareDetailPanel
        locale={locale}
        isOpen={isDetailOpen}
        sample={selectedSample}
        onClose={() => setIsDetailOpen(false)}
      />
    </section>
  )
}
