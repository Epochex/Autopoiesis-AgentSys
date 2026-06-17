import {
  lazy,
  Suspense,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import './App.css'
import { ErrorBoundary } from './components/ErrorBoundary'
import { LiveFlowConsole } from './components/LiveFlowConsole'
import { PipelineTopologyView } from './components/PipelineTopologyView'
import { controlLoopSnapshot } from './data/controlLoopModel'
import { useControlLoopSnapshot } from './hooks/useControlLoopSnapshot'

type ViewMode = 'console' | 'topology' | 'compare'
type Locale = 'en' | 'zh'

const CompareModeView = lazy(() =>
  import('./components/CompareModeView').then((module) => ({
    default: module.CompareModeView,
  })),
)

function firstDeviceName(snapshot: ReturnType<typeof useControlLoopSnapshot>['snapshot']) {
  const suggestion = snapshot.suggestions[0]
  const deviceName = suggestion?.evidenceBundle.device.device_name
  return typeof deviceName === 'string' && deviceName ? deviceName : null
}

function selectedDeviceLabel(
  snapshot: ReturnType<typeof useControlLoopSnapshot>['snapshot'],
  suggestion: ReturnType<typeof useControlLoopSnapshot>['snapshot']['suggestions'][number],
) {
  const deviceName = suggestion?.evidenceBundle.device.device_name
  if (typeof deviceName === 'string' && deviceName.trim().length > 0) {
    return deviceName
  }

  return firstDeviceName(snapshot) ?? suggestion.context.srcDeviceKey
}

function App() {
  const { snapshot, latestDelta, connectionState, transportIssue } =
    useControlLoopSnapshot()
  const heroStageRef = useRef<HTMLElement | null>(null)
  const suggestionPool =
    snapshot.suggestions.length > 0
      ? snapshot.suggestions
      : controlLoopSnapshot.suggestions
  const defaultSuggestionId =
    snapshot.defaultSuggestionId || suggestionPool[0]?.id || controlLoopSnapshot.defaultSuggestionId
  const [view, setView] = useState<ViewMode>('console')
  const [locale, setLocale] = useState<Locale>('en')
  const [showSurfaceDock, setShowSurfaceDock] = useState(true)
  const [preferredSuggestionId, setPreferredSuggestionId] =
    useState(defaultSuggestionId)
  const activeSuggestionId = suggestionPool.some(
    (suggestion) => suggestion.id === preferredSuggestionId,
  )
    ? preferredSuggestionId
    : defaultSuggestionId

  const selectedSuggestion = useMemo(
    () =>
      suggestionPool.find(
        (suggestion) => suggestion.id === activeSuggestionId,
      ) ?? suggestionPool[0],
    [activeSuggestionId, suggestionPool],
  )
  const copy = useMemo(
    () =>
      locale === 'zh'
        ? {
            kicker: 'ContextHelix / 企业上下文控制',
            titleConsole: '上下文控制台',
            titleTopology: '系统链路图',
            titleCompare: '策略对比',
            navConsole: '首页总览',
            navTopology: '链路地图',
            navCompare: '对比模式',
            labels: {
              service: '当前服务',
              device: '当前设备',
              judgment: '当前判断',
              stream: '数据流状态',
              latestSuggestion: '最新建议',
              nextAction: '下一步动作',
              branch: '分支',
              validation: '验证状态',
              latestAlert: '最新告警',
              closure: '闭环状态',
            },
          }
        : {
            kicker: 'ContextHelix / Enterprise Context Control',
            titleConsole: 'Context Control Console',
            titleTopology: 'System Flow Map',
            titleCompare: 'Compare Mode',
            navConsole: 'Overview',
            navTopology: 'Flow Map',
            navCompare: 'Compare',
            labels: {
              service: 'active service',
              device: 'active device',
              judgment: 'current judgment',
              stream: 'stream state',
              latestSuggestion: 'latest suggestion',
              nextAction: 'next action',
              branch: 'branch',
              validation: 'validation',
              latestAlert: 'latest alert',
              closure: 'closure',
            },
          },
    [locale],
  )
  const dockSummary = useMemo(
    () =>
      locale === 'zh'
        ? `${selectedSuggestion.context.service} · ${selectedDeviceLabel(snapshot, selectedSuggestion)} · ${selectedSuggestion.confidenceLabel}`
        : `${selectedSuggestion.context.service} · ${selectedDeviceLabel(snapshot, selectedSuggestion)} · ${selectedSuggestion.confidenceLabel}`,
    [locale, selectedSuggestion, snapshot],
  )
  const primaryTitle =
    view === 'console'
      ? copy.titleConsole
      : view === 'topology'
        ? copy.titleTopology
        : copy.titleCompare
  const dockAutoHide = true

  useEffect(() => {
    if (!dockAutoHide) {
      return
    }
    let previousY = window.scrollY

    const handleScroll = () => {
      const currentY = window.scrollY
      const downDelta = currentY - previousY
      const upDelta = previousY - currentY

      if (currentY <= 16) {
        setShowSurfaceDock(true)
      } else if (downDelta > 6 && currentY > 56) {
        setShowSurfaceDock(false)
      } else if (upDelta > 10) {
        setShowSurfaceDock(true)
      }

      previousY = currentY
    }

    window.addEventListener('scroll', handleScroll, { passive: true })
    return () => window.removeEventListener('scroll', handleScroll)
  }, [dockAutoHide])

  if (!selectedSuggestion) {
    return (
      <div className="app-shell">
        <section className="page" style={{ borderRight: 'none' }}>
          <section className="section error-panel">
            <div className="section-header">
              <div>
                <h2 className="section-title">No Suggestion Available</h2>
                <span className="section-subtitle">
                  The frontend has no suggestion slice to bind the current
                  control-loop story to.
                </span>
              </div>
              <span className="section-kicker">empty control-loop selection</span>
            </div>
            <div className="error-panel-body">
              <strong>Check `/api/control-loop/snapshot` and the local fallback model.</strong>
            </div>
          </section>
        </section>
      </div>
    )
  }

  return (
    <div
      className={`app-shell ${dockAutoHide && !showSurfaceDock ? 'dock-collapsed' : ''}`}
    >
      <header
        className={`surface-dock ${dockAutoHide && !showSurfaceDock ? 'is-hidden' : ''}`}
      >
        <div className="surface-dock-brand">
          <span className="surface-dock-kicker">{copy.kicker}</span>
          <div className="surface-dock-title">
            <strong>{primaryTitle}</strong>
            <span
              className={`live-dot state-${connectionState}`}
              aria-hidden="true"
            />
          </div>
          <span className="surface-dock-summary">{dockSummary}</span>
        </div>

        <div className="surface-dock-controls">
          <nav className="view-switch surface-switch" aria-label="Primary views">
            <button
              type="button"
              className={view === 'console' ? 'tab is-active' : 'tab'}
              onClick={() => {
                setView('console')
                setShowSurfaceDock(true)
              }}
            >
              {copy.navConsole}
            </button>
            <button
              type="button"
              className={view === 'topology' ? 'tab is-active' : 'tab'}
              onClick={() => {
                setView('topology')
                setShowSurfaceDock(true)
              }}
            >
              {copy.navTopology}
            </button>
            <button
              type="button"
              className={view === 'compare' ? 'tab is-active' : 'tab'}
              onClick={() => {
                setView('compare')
                setShowSurfaceDock(true)
              }}
            >
              {copy.navCompare}
            </button>
          </nav>

          <div className="locale-switch surface-switch" aria-label="Language switch">
            <button
              type="button"
              className={locale === 'en' ? 'tab is-active' : 'tab'}
              onClick={() => setLocale('en')}
            >
              EN
            </button>
            <button
              type="button"
              className={locale === 'zh' ? 'tab is-active' : 'tab'}
              onClick={() => setLocale('zh')}
            >
              中文
            </button>
          </div>
        </div>
      </header>

      <main
        className={[
          view === 'compare' ? 'workspace workspace-compare' : 'workspace',
          dockAutoHide ? 'workspace-with-dock-offset' : '',
        ]
          .filter(Boolean)
          .join(' ')}
      >
        <ErrorBoundary title="Primary View">
          {view === 'console' ? (
            <LiveFlowConsole
              connectionState={connectionState}
              snapshot={snapshot}
              latestDelta={latestDelta}
              selectedSuggestion={selectedSuggestion}
              onSelectSuggestion={setPreferredSuggestionId}
              transportIssue={transportIssue}
              locale={locale}
              heroStageRef={heroStageRef}
            />
          ) : view === 'topology' ? (
            <PipelineTopologyView snapshot={snapshot} locale={locale} />
          ) : (
            <Suspense
              fallback={
                <section className="page">
                  <section className="section chart-fallback">
                    {locale === 'zh' ? '正在载入对比模式...' : 'loading compare mode...'}
                  </section>
                </section>
              }
            >
              <CompareModeView locale={locale} />
            </Suspense>
          )}
        </ErrorBoundary>
      </main>
    </div>
  )
}

export default App
