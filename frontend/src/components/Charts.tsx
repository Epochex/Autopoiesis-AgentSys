import ReactECharts from 'echarts-for-react'
import type { Baseline, DataStats } from '../types'
import { makeT, type Lang } from '../i18n'

const AXIS = '#8b93a3'
const GRID = '#262b35'

export function AblationChart({ baselines, lang }: { baselines: Baseline[]; lang: Lang }) {
  const t = makeT(lang)
  const option = {
    grid: { left: 44, right: 16, top: 24, bottom: 48 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: baselines.map((b) => b.name),
      axisLabel: { color: AXIS, fontSize: 10, interval: 0, rotate: 16 },
      axisLine: { lineStyle: { color: GRID } },
    },
    yAxis: {
      type: 'value',
      max: 100,
      name: '%',
      axisLabel: { color: AXIS, fontSize: 10 },
      splitLine: { lineStyle: { color: GRID } },
    },
    series: [
      {
        type: 'bar',
        data: baselines.map((b) => ({
          value: Math.round(b.rootCauseAccuracy * 100),
          itemStyle: { color: b.rootCauseAccuracy < 1 ? '#ff6b6b' : '#3ad29f', borderRadius: [4, 4, 0, 0] },
        })),
        barWidth: '46%',
        label: { show: true, position: 'top', color: '#e6e9ef', fontSize: 11, formatter: '{c}%' },
      },
    ],
  }
  return (
    <div className="chart-card">
      <div className="chart-title">{t('ablation')} · {t('accuracy')}</div>
      <ReactECharts option={option} style={{ height: 220 }} notMerge lazyUpdate />
      <p className="chart-note">{t('ablationNote')}</p>
    </div>
  )
}

export function DenyPortChart({ stats, lang }: { stats: DataStats; lang: Lang }) {
  const t = makeT(lang)
  const ports = stats.topDenyPorts
  const option = {
    grid: { left: 60, right: 20, top: 24, bottom: 24 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'value', axisLabel: { color: AXIS, fontSize: 10 }, splitLine: { lineStyle: { color: GRID } } },
    yAxis: {
      type: 'category',
      data: ports.map((p) => `:${p[0]}`).reverse(),
      axisLabel: { color: AXIS, fontSize: 11 },
      axisLine: { lineStyle: { color: GRID } },
    },
    series: [
      {
        type: 'bar',
        data: ports.map((p) => p[1]).reverse(),
        itemStyle: { color: '#5db4ff', borderRadius: [0, 4, 4, 0] },
        barWidth: '56%',
      },
    ],
  }
  return (
    <div className="chart-card">
      <div className="chart-title">{t('denyByPort')}</div>
      <ReactECharts option={option} style={{ height: 220 }} notMerge lazyUpdate />
    </div>
  )
}
