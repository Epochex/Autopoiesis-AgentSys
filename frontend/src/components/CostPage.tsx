import './cost.css'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import { display } from './display-lexicon'

/* ── 模型花销 · what the system spent on model calls, and which feature spent it
 * ────────────────────────────────────────────────────────────────────────────
 *
 * Token counts come back from the provider and are exact. The money column is
 * multiplied out from a local rate table, so it is an estimate — the page
 * prints the ledger's own caveat (`rates_note`) verbatim rather than
 * paraphrasing it into something that sounds more certain than it is.
 *
 * A call the provider reported no usage for is filed as a zero-token row. That
 * zero means "unknown", not "free", so those rows are marked instead of being
 * quietly averaged in. */

interface CostBucket {
  calls: number
  tokens: number
  cost_cny: number
}

interface CostCall {
  at: string
  model: string
  purpose: string
  session_id: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_cny: number
  usage_reported: boolean
  failed?: boolean
  error?: string
  prompt_cache_hit_tokens?: number | null
  prompt_cache_miss_tokens?: number | null
}

interface LargestCall {
  at: string
  model: string
  purpose: string
  total_tokens: number
  cost_cny: number
}

interface CostResp {
  ok: boolean
  window_hours: number
  calls: number
  total_tokens: number
  total_cost_cny: number
  average_tokens_per_call: number
  /** already sorted, dearest first */
  by_purpose: Record<string, CostBucket>
  by_model: Record<string, CostBucket>
  /** key is a UTC ISO hour prefix, e.g. "2026-08-22T01" */
  by_hour: Record<string, number>
  largest_call: LargestCall | null
  recent: CostCall[]
  rates_note: string
  failed_calls?: number
  failures_by_purpose?: Record<string, number>
  budget?: {
    window_hours: number
    classes: Record<'background' | 'interactive', { spent_cny: number; cap_cny: number; remaining_cny: number; exhausted: boolean }>
    interactive_purposes: string[]
  }
  provider_balance?: { available: boolean; currency: string; total: number; topped_up: number; granted: number; checked_at: string } | null
  provider_cache?: {
    hit_tokens: number | null
    miss_tokens: number | null
    hit_reported_calls: number
    miss_reported_calls: number
    reported_calls: number
    total_calls: number
  }
}

interface CacheNamespace {
  entries: number
  bytes: number
  newest_age_sec: number | null
}

interface CacheResp {
  ok: boolean
  calls_enabled: boolean
  prewarm_enabled: boolean
  entries: number
  dir: string
  namespaces: Record<string, CacheNamespace>
}

type St = { s: 'load' } | { s: 'err'; m: string } | { s: 'ok'; d: CostResp }
type CacheSt = { s: 'load' } | { s: 'err' } | { s: 'ok'; d: CacheResp }

interface Win {
  hours: number
  zh: string
  en: string
}

const WINDOWS: readonly Win[] = [
  { hours: 1, zh: '1 小时', en: '1H' },
  { hours: 24, zh: '24 小时', en: '24H' },
  { hours: 168, zh: '7 天', en: '7D' },
  { hours: 720, zh: '30 天', en: '30D' },
]

const REFRESH_MS = 60_000

/** The ledger files each call under the response-schema name that asked for it,
 * so these keys are the feature register. An unknown key prints raw rather than
 * being guessed at. */
const PURPOSE: Record<string, [string, string]> = {
  rca_analysis: ['查故障原因', 'ROOT-CAUSE ANALYSIS'],
  rca_followup: ['追问', 'FOLLOW-UP QUESTION'],
  rca_eval: ['结果打分', 'RESULT SCORING'],
  network_rca_diagnosis: ['网络故障判断', 'NETWORK DIAGNOSIS'],
  threat_assessment: ['威胁判断', 'THREAT ASSESSMENT'],
  wan_threat: ['外网威胁判断', 'WAN THREAT'],
  internal_host: ['内网主机分析', 'INTERNAL HOST'],
  device_graph_analysis: ['设备关系分析', 'DEVICE GRAPH'],
  mesh_model: ['网络关系建模', 'MESH MODEL'],
  posture: ['安全状态判断', 'SECURITY POSTURE'],
  JudgeResponse_v1: ['结果评审', 'JUDGE'],
  diag: ['测试调用', 'TEST CALL'],
  production_wan_threat: ['外网来源深度研判', 'WAN SOURCE ASSESSMENT'],
  production_analysis: ['内网对象深度研判', 'INTERNAL ASSESSMENT'],
  display_translation: ['英文界面翻译', 'ENGLISH UI TRANSLATION'],
  'evidence-analysis-v1': ['证据分析', 'EVIDENCE ANALYSIS'],
  wan: ['外网来源深度研判', 'WAN SOURCE ASSESSMENT'],
}

const purposeLabel = (key: string, zh: boolean) => {
  const hit = PURPOSE[key]
  return hit ? hit[zh ? 0 : 1] : key
}

const T = (zh: boolean) => ({
  code: zh ? '本地记账 · 只读' : 'LOCAL LEDGER · READ-ONLY',
  thesis: zh
    ? '系统每调一次模型就记一笔,记在发起它的那个功能名下,所以能看出钱是哪个功能花的。token 数是接口返回的,准;钱是拿本地费率表乘出来的,只是估。'
    : 'Every model call is filed under the feature that asked for it, so spend traces back to a feature instead of just a date. Token counts come from the API and are exact; the money figure is multiplied out from a local rate table and is an estimate.',
  windowK: zh ? '看哪段时间' : 'WINDOW',
  loading: zh ? '读取中…' : 'READING…',
  offline: zh ? '读不到花销数据' : 'SPEND DATA UNAVAILABLE',
  empty: zh ? '这段时间没有调用' : 'NO CALLS IN THIS WINDOW',
  lastAt: zh ? '上次刷新' : 'LAST REFRESH',
  auto: zh ? '每 60 秒自动刷新' : 'AUTO EVERY 60S',
  refresh: zh ? '立即刷新' : 'REFRESH NOW',
  refreshing: zh ? '刷新中…' : 'REFRESHING…',
  totalCost: (h: number) => {
    if (!zh) return 'TOTAL SPEND (CNY)'
    if (h <= 1) return '这一个小时花了多少 (元)'
    if (h <= 24) return '这一天花了多少 (元)'
    return `这 ${Math.round(h / 24)} 天花了多少 (元)`
  },
  calls: zh ? '调用次数' : 'CALLS',
  callsUnit: zh ? '次调用' : 'CALLS',
  tokens: zh ? 'TOKEN 总数' : 'TOTAL TOKENS',
  avg: zh ? '平均每次 TOKEN' : 'AVG TOKENS / CALL',
  chart: zh ? '每小时花了多少' : 'SPEND BY HOUR',
  chartDay: zh ? '每天花了多少' : 'SPEND BY DAY',
  chartEmpty: zh ? '这段时间没有花销' : 'NOTHING SPENT IN THIS WINDOW',
  barsSum: zh ? '图上这些格加起来' : 'BARS ADD UP TO',
  cellsHour: (n: number) => (zh ? `每格 1 小时 · 共 ${n} 格` : `1 HOUR PER BAR · ${n} BARS`),
  cellsDay: (n: number) => (zh ? `每格 1 天 · 共 ${n} 格` : `1 DAY PER BAR · ${n} BARS`),
  topHours: zh ? '最花钱的几个小时' : 'DEAREST HOURS',
  topDays: zh ? '最花钱的几天' : 'DEAREST DAYS',
  spent: zh ? '花在哪儿了' : 'WHERE THE MONEY WENT',
  spentK: zh ? '按功能分 · 贵的在前' : 'BY FEATURE · DEAREST FIRST',
  feature: zh ? '功能' : 'FEATURE',
  cost: zh ? '花销 (元)' : 'COST (CNY)',
  shareK: zh ? '占这段时间的比例' : 'SHARE OF WINDOW',
  models: zh ? '用的哪个模型' : 'BY MODEL',
  biggest: zh ? '最贵的一次' : 'DEAREST SINGLE CALL',
  biggestNone: zh ? '这段时间没有记到调用' : 'NO CALL RECORDED IN THIS WINDOW',
  when: zh ? '时间' : 'TIME',
  model: zh ? '模型' : 'MODEL',
  recent: zh ? '最近几次调用' : 'RECENT CALLS',
  inTok: zh ? '输入 TOKEN' : 'PROMPT',
  outTok: zh ? '输出 TOKEN' : 'COMPLETION',
  noUsage: zh ? '没上报' : 'NOT REPORTED',
  noUsageNote: zh
    ? '标了「没上报」的那几行,接口没告诉我们用了多少 token,所以这里的钱写成 0 —— 不是没花钱,是不知道花了多少,这几行的金额不能信。上面的总数也少算了这一部分。'
    : 'Rows marked NOT REPORTED came back with no usage object, so their cost prints as zero. That is not free, it is unknown — those amounts are not trustworthy, and the totals above are short by that much.',
  switches: zh ? '开关和缓存' : 'SWITCHES & CACHE',
  callsSw: zh ? '模型调用' : 'MODEL CALLS',
  prewarmSw: zh ? '开机预热' : 'PREWARM',
  on: zh ? '开着' : 'ON',
  off: zh ? '关着' : 'OFF',
  cached: zh ? '缓存条数' : 'CACHED ENTRIES',
  cacheDir: zh ? '存在哪' : 'DIRECTORY',
  providerCache: zh ? '供应商输入缓存' : 'PROVIDER INPUT CACHE',
  cacheHit: zh ? '命中输入 token' : 'CACHED INPUT TOKENS',
  cacheMiss: zh ? '未命中输入 token' : 'UNCACHED INPUT TOKENS',
  cacheUnknown: zh ? '未报告' : 'NOT REPORTED',
  cacheCoverage: zh ? '报告缓存用量的调用' : 'CALLS REPORTING CACHE USAGE',
  providerCacheNote: zh
    ? '供应商输入缓存复用请求中的输入 token，调用仍会生成回答。应用结果缓存直接复用已有分析，可免去一次模型调用。以下统计只汇总接口实际报告的值；未报告的字段显示为未报告。费用继续按本地费率估算。'
    : 'Provider input caching reuses input tokens during a model call. Application result caching reuses a completed analysis and avoids a model call. Counters below include only values reported by the API; absent fields remain unreported. Cost continues to use the local rate estimate.',
  cacheWhy: zh
    ? '缓存里存着的这些回答,重新部署以后可以直接拿来用,这部分就不用再花钱问模型第二遍。'
    : 'Answers already in the cache are reused after a redeploy, so that much never has to be paid for a second time.',
  cacheNone: zh ? '现在一条都没缓存' : 'NOTHING CACHED YET',
  cacheOff: zh ? '读不到缓存状态' : 'CACHE STATUS UNAVAILABLE',
  noteK: zh ? '说明' : 'NOTE',
  ledger: zh ? '账本' : 'LEDGER',
  windowSpend: zh ? '本窗口花费' : 'SPENT IN WINDOW',
  budgetK: zh ? '24 小时预算' : '24H BUDGET',
  background: zh ? '后台自动' : 'UNATTENDED',
  interactive: zh ? '人在等的' : 'INTERACTIVE',
  budgetNote: zh ? '后台调用（自动调查、批量研判）到线即停，明天自动恢复；人在等的研判另有一档。' : 'Unattended calls stop at the line and resume the next day; interactive assessments have their own line.',
  balance: zh ? '服务商余额' : 'PROVIDER BALANCE',
  balanceOff: zh ? '余额读不到' : 'BALANCE UNAVAILABLE',
  balanceUnavailable: zh ? '账户不可用' : 'ACCOUNT UNAVAILABLE',
  failed: zh ? '被拒绝的调用' : 'REFUSED CALLS',
  failedNote: zh ? '被拒绝的调用不花钱，但说明有人在等一个没来的答案。' : 'A refused call costs nothing, but someone was waiting for an answer that never came.',
  klass: zh ? '类型' : 'CLASS',
  cacheLine: zh ? '结果缓存' : 'RESULT CACHE',
  entries: zh ? '条' : 'ENTRIES',
  hitFree: zh ? '命中免费' : 'HITS ARE FREE',
  callsOff: zh ? '模型调用已关闭' : 'MODEL CALLS OFF',
  status: zh ? '状态' : 'STATUS',
  refused: zh ? '被拒' : 'REFUSED',
  ok: zh ? '成功' : 'OK',
})

const pad = (n: number) => String(n).padStart(2, '0')

const num = (n: number) => n.toLocaleString('en-US')

const money = (n: number, digits: number) => n.toFixed(digits)

const clock = (ms: number) => {
  const t = new Date(ms)
  return `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`
}

const stamp = (iso: string) => {
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return iso
  return `${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`
}



interface Bar {
  key: string
  label: string
  cost: number
}

/** Lay the buckets on a contiguous axis ending at the current bucket, so an
 * hour that cost nothing reads as a gap instead of being closed up — a chart
 * that drops its empty hours makes a quiet stretch look busy. One bucket more
 * than the window, because the window starts mid-bucket and the ledger files
 * that leading slice under the whole hour it fell in: without the extra bar,
 * spend that the headline total counts would have nowhere to be drawn. Lookup
 * keys are UTC because that is what the ledger writes; hourly labels are local
 * time of the same instant, and daily labels come off the UTC key so a bar and
 * its label can never name different days. */
const buildBars = (byHour: Record<string, number>, windowHours: number): { bars: Bar[]; daily: boolean } => {
  const daily = windowHours > 168
  const stepMs = daily ? 86_400_000 : 3_600_000
  const count = (daily ? Math.ceil(windowHours / 24) : windowHours) + 1

  const sums = new Map<string, number>()
  for (const [key, value] of Object.entries(byHour)) {
    const bucket = daily ? key.slice(0, 10) : key.slice(0, 13)
    sums.set(bucket, (sums.get(bucket) ?? 0) + value)
  }

  const end = Math.floor(Date.now() / stepMs) * stepMs
  const bars: Bar[] = []
  for (let i = count - 1; i >= 0; i -= 1) {
    const at = new Date(end - i * stepMs)
    const iso = at.toISOString()
    const key = daily ? iso.slice(0, 10) : iso.slice(0, 13)
    bars.push({
      key,
      label: daily ? key.slice(5) : `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:00`,
      cost: sums.get(key) ?? 0,
    })
  }
  return { bars, daily }
}

const readJson = async <T,>(url: string, signal: AbortSignal): Promise<T> => {
  const r = await fetch(url, { headers: { Accept: 'application/json' }, signal })
  if (!r.ok) throw new Error(`gateway ${r.status}`)
  return (await r.json()) as T
}

export function CostPage({ lang }: { lang: Lang }) {
  const zh = lang === 'zh'
  const tx = T(zh)
  const [hours, setHours] = useState<number>(24)
  const [st, setSt] = useState<St>({ s: 'load' })
  const [cacheSt, setCacheSt] = useState<CacheSt>({ s: 'load' })
  const [refreshedAt, setRefreshedAt] = useState<number | null>(null)
  const [pending, setPending] = useState(false)
  // One request in flight at a time. The 60s timer skips its turn rather than
  // queueing behind a slow gateway; a window change or a manual press instead
  // supersedes what is running, so the newest answer is always the one shown.
  const inflight = useRef<AbortController | null>(null)

  const load = useCallback(async (h: number, mode: 'timer' | 'now') => {
    const running = inflight.current
    if (running) {
      if (mode === 'timer') return
      running.abort()
    }
    const ctl = new AbortController()
    inflight.current = ctl
    setPending(true)
    try {
      const [cost, cache] = await Promise.allSettled([
        readJson<CostResp>(`/api/rca/cost?hours=${h}`, ctl.signal),
        readJson<CacheResp>('/api/rca/cost/cache', ctl.signal),
      ])
      if (ctl.signal.aborted) return
      if (cost.status === 'fulfilled' && cost.value.ok) {
        setSt({ s: 'ok', d: cost.value })
        setRefreshedAt(Date.now())
      } else {
        const why = cost.status === 'rejected' && cost.reason instanceof Error ? cost.reason.message : 'NO DATA'
        setSt({ s: 'err', m: why })
      }
      setCacheSt(cache.status === 'fulfilled' && cache.value.ok ? { s: 'ok', d: cache.value } : { s: 'err' })
    } finally {
      if (inflight.current === ctl) {
        inflight.current = null
        setPending(false)
      }
    }
  }, [])

  useEffect(() => {
    setSt({ s: 'load' })
    void load(hours, 'now')
    const id = window.setInterval(() => { void load(hours, 'timer') }, REFRESH_MS)
    return () => {
      window.clearInterval(id)
      inflight.current?.abort()
      inflight.current = null
    }
  }, [hours, load])

  const refresh = useCallback(() => { void load(hours, 'now') }, [hours, load])

  const d = st.s === 'ok' ? st.d : null
  const chart = useMemo(() => (d ? buildBars(d.by_hour, d.window_hours) : { bars: [] as Bar[], daily: false }), [d])
  const peakCost = useMemo(() => chart.bars.reduce((top, b) => (b.cost > top ? b.cost : top), 0), [chart])
  const barsSum = useMemo(() => chart.bars.reduce((sum, b) => sum + b.cost, 0), [chart])
  const topBars = useMemo(() => chart.bars.filter((b) => b.cost > 0).sort((a, b) => b.cost - a.cost).slice(0, 3), [chart])
  const purposes = useMemo(() => Object.entries(d?.by_purpose ?? {}), [d])
  const interactive = useMemo(() => new Set(d?.budget?.interactive_purposes ?? []), [d])
  const failures = useMemo(() => d?.failures_by_purpose ?? {}, [d])
  const failedPurposesOnly = useMemo(
    () => Object.keys(failures).filter((key) => !(d?.by_purpose ?? {})[key]),
    [failures, d],
  )
  const unreported = useMemo(() => (d?.recent ?? []).filter((r) => !r.usage_reported && !r.failed).length, [d])
  const cache = cacheSt.s === 'ok' ? cacheSt.d : null
  const budget = d?.budget ?? null
  const balance = d?.provider_balance ?? null
  const hasRows = Boolean(d && (d.calls > 0 || (d.failed_calls ?? 0) > 0))

  const gauge = (spent: number, cap: number) => `${Math.min(100, cap > 0 ? (spent / cap) * 100 : 0).toFixed(1)}%`

  return (
    <div className="cx">
      <header className="cx-head">
        <div className="cx-head-l">
          <div className="cx-head-code"><span>{tx.code}</span></div>
          <h1 className="cx-head-title">{zh ? <>模型<mark>花销</mark></> : <>MODEL <mark>SPEND</mark></>}</h1>
          <p className="cx-thesis">{tx.thesis}</p>
        </div>
        <div className="cx-ctl">
          <div className="cx-win" role="group" aria-label={tx.windowK}>
            <span className="cx-win-k">{tx.windowK}</span>
            {WINDOWS.map((w) => (
              <button key={w.hours} type="button" className={`cx-win-b${hours === w.hours ? ' is-on' : ''}`}
                aria-pressed={hours === w.hours} onClick={() => setHours(w.hours)}>{zh ? w.zh : w.en}</button>
            ))}
          </div>
          <div className="cx-fresh">
            <span className="cx-fresh-k">{tx.lastAt}</span>
            <b>{refreshedAt === null ? '—' : clock(refreshedAt)}</b>
            <span className="cx-fresh-a">{tx.auto}</span>
            <button type="button" className="cx-fresh-b" onClick={refresh} disabled={pending}>{pending ? tx.refreshing : tx.refresh}</button>
          </div>
        </div>
      </header>

      {/* ── the ledger line: what this window cost, where the day's lines are,
             and what the provider says is left. One band of rules and type. */}
      <section className="cx-band" aria-label={tx.ledger}>
        <div className="cx-band-c is-spend">
          <span className="cx-k">{tx.totalCost(hours)}</span>
          <b className="cx-money n"><i>¥</i>{d ? money(d.total_cost_cny, 4) : '—'}</b>
          <span className="cx-sub n">{d ? `${num(d.calls)} ${tx.callsUnit} · ${num(d.total_tokens)} tokens` : tx.loading}{d && (d.failed_calls ?? 0) > 0 ? <em className="is-hi"> · {num(d.failed_calls ?? 0)} {tx.refused}</em> : null}</span>
        </div>
        <div className="cx-band-c is-budget">
          <span className="cx-k">{tx.budgetK}</span>
          {budget ? (['background', 'interactive'] as const).map((key) => {
            const line = budget.classes[key]
            return (
              <div className={`cx-gauge${line.exhausted ? ' is-out' : ''}`} key={key}>
                <span className="cx-gauge-k">{key === 'background' ? tx.background : tx.interactive}</span>
                <span className="cx-gauge-t" aria-hidden="true"><i style={{ width: gauge(line.spent_cny, line.cap_cny) }} /></span>
                <span className="cx-gauge-v n"><b>¥{money(line.spent_cny, 2)}</b> / ¥{money(line.cap_cny, 2)}</span>
              </div>
            )
          }) : <span className="cx-sub">{tx.loading}</span>}
          <span className="cx-sub">{tx.budgetNote}</span>
        </div>
        <div className={`cx-band-c is-balance${balance && !balance.available ? ' is-out' : ''}`}>
          <span className="cx-k">{tx.balance}</span>
          <b className="cx-money is-small n">{balance ? <><i>¥</i>{money(balance.total, 2)}</> : '—'}</b>
          <span className="cx-sub">{balance ? (balance.available ? `DeepSeek · ${balance.checked_at.slice(11, 16)} UTC` : tx.balanceUnavailable) : tx.balanceOff}</span>
        </div>
        <div className="cx-band-c is-cache">
          <span className="cx-k">{tx.cacheLine}</span>
          <b className="cx-money is-small n">{cache ? num(cache.entries) : '—'}<i className="is-after"> {tx.entries}</i></b>
          <span className="cx-sub">{cache ? (cache.calls_enabled ? tx.hitFree : tx.callsOff) : tx.cacheOff}{cache ? <code> {cache.dir}</code> : null}</span>
        </div>
      </section>

      {st.s === 'load' ? <div className="cx-state">{tx.loading}</div>
        : st.s === 'err' ? <div className="cx-state is-err">{tx.offline} · {st.m}</div>
        : !d || !hasRows ? (
          <>
            <div className="cx-state">{tx.empty}</div>
            <div className="cx-note"><span className="cx-note-k">{tx.noteK}</span><p>{display(d?.rates_note ?? '', lang)}</p></div>
          </>
        ) : (
          <>
            <section className="cx-sec">
              <div className="cx-sec-k">{chart.daily ? tx.chartDay : tx.chart}<em>{chart.daily ? tx.cellsDay(chart.bars.length) : tx.cellsHour(chart.bars.length)} · {tx.barsSum} ¥{money(barsSum, 4)}</em></div>
              {peakCost <= 0 ? <div className="cx-state is-inline">{tx.chartEmpty}</div> : (
                <div className="cx-chart">
                  <div className="cx-plot">
                    <div className="cx-plot-ax" aria-hidden="true"><span>¥{money(peakCost, 3)}</span><span>¥{money(peakCost / 2, 3)}</span><span>¥0</span></div>
                    <div className="cx-plot-in">
                      <div className="cx-plot-mid" aria-hidden="true" />
                      <ul className="cx-bars">
                        {chart.bars.map((b) => {
                          const text = `${b.label} · ¥${money(b.cost, 4)}`
                          return <li key={b.key} className={`cx-bar${b.cost > 0 && b.cost === peakCost ? ' is-peak' : ''}`} title={text}>
                            <span className="cx-bar-f" aria-hidden="true" style={{ height: b.cost > 0 ? `${Math.max((b.cost / peakCost) * 100, 2)}%` : '0' }} />
                            <span className="cx-sr">{text}</span>
                          </li>
                        })}
                      </ul>
                    </div>
                  </div>
                  <div className="cx-chart-f"><span>{chart.bars[0]?.label ?? '—'}</span><span>{chart.bars[chart.bars.length - 1]?.label ?? '—'}</span></div>
                  <p className="cx-chart-top"><span className="cx-chart-k">{chart.daily ? tx.topDays : tx.topHours}</span>{topBars.map((b) => <span className="cx-chart-t" key={b.key}><b>{b.label}</b> ¥{money(b.cost, 4)}</span>)}</p>
                </div>
              )}
            </section>

            <section className="cx-sec">
              <div className="cx-sec-k">{tx.spent}<em>{tx.spentK}</em></div>
              <div className="cx-scroll">
                <table className="cx-t cx-t-spend">
                  <thead><tr><th>{tx.feature}</th><th>{tx.klass}</th><th className="n">{tx.calls}</th><th className="n">{tx.failed}</th><th className="n">TOKENS</th><th className="n">{tx.cost}</th><th>{tx.shareK}</th></tr></thead>
                  <tbody>
                    {purposes.map(([key, b], i) => {
                      const share = d.total_cost_cny > 0 ? (b.cost_cny / d.total_cost_cny) * 100 : 0
                      const klass = interactive.has(key) ? 'interactive' : 'background'
                      return <tr key={key} className={i === 0 ? 'is-top' : ''}>
                        <th>{purposeLabel(key, zh)}<code>{key}</code></th>
                        <td><span className={`cx-class is-${klass}`}>{klass === 'interactive' ? tx.interactive : tx.background}</span></td>
                        <td className="n">{num(b.calls)}</td>
                        <td className={`n${failures[key] ? ' is-hi' : ''}`}>{failures[key] ? num(failures[key]) : '—'}</td>
                        <td className="n">{num(b.tokens)}</td>
                        <td className="n strong">¥{money(b.cost_cny, 4)}</td>
                        <td className="cx-share-c"><span className="cx-share" aria-hidden="true"><i style={{ width: `${share}%` }} /></span><em>{share.toFixed(1)}%</em></td>
                      </tr>
                    })}
                    {failedPurposesOnly.map((key) => (
                      <tr key={`f-${key}`} className="is-refused">
                        <th>{purposeLabel(key, zh)}<code>{key}</code></th>
                        <td><span className={`cx-class is-${interactive.has(key) ? 'interactive' : 'background'}`}>{interactive.has(key) ? tx.interactive : tx.background}</span></td>
                        <td className="n">0</td><td className="n is-hi">{num(failures[key])}</td><td className="n">0</td><td className="n">¥0.0000</td><td className="cx-share-c"><em>—</em></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {(d.failed_calls ?? 0) > 0 ? <p className="cx-line is-hi">{tx.failedNote}</p> : null}
              <p className="cx-line">{Object.entries(d.by_model).map(([key, b]) => <span key={key}><b>{key}</b> {num(b.calls)} {tx.callsUnit} · {num(b.tokens)} tokens · ¥{money(b.cost_cny, 4)}</span>)}{d.largest_call ? <span><b>{tx.biggest}</b> ¥{money(d.largest_call.cost_cny, 4)} · {num(d.largest_call.total_tokens)} tokens · {purposeLabel(d.largest_call.purpose, zh)} · {stamp(d.largest_call.at)}</span> : null}</p>
            </section>

            <section className="cx-sec">
              <div className="cx-sec-k">{tx.recent}<em>{num(d.recent.length)}</em></div>
              <div className="cx-scroll">
                <table className="cx-t cx-t-recent">
                  <thead><tr><th>{tx.when}</th><th>{tx.feature}</th><th>{tx.model}</th><th>{tx.status}</th><th className="n">{tx.inTok}</th><th className="n">{tx.outTok}</th><th className="n">{tx.cost}</th></tr></thead>
                  <tbody>
                    {d.recent.map((r, i) => (
                      <tr key={`${r.at}-${i}`} className={r.failed ? 'is-refused' : !r.usage_reported ? 'is-nousage' : ''}>
                        <td className="n">{stamp(r.at)}</td>
                        <th>{purposeLabel(r.purpose, zh)}{r.session_id ? <code>{r.session_id}</code> : null}</th>
                        <td>{r.model}</td>
                        <td>{r.failed ? <span className="cx-flag">{tx.refused} · {(r.error ?? '').slice(0, 40)}</span> : !r.usage_reported ? <span className="cx-flag">{tx.noUsage}</span> : tx.ok}</td>
                        <td className="n">{r.failed ? '—' : num(r.prompt_tokens)}</td>
                        <td className="n">{r.failed ? '—' : num(r.completion_tokens)}</td>
                        <td className="n strong">{r.failed ? '—' : `¥${money(r.cost_cny, 4)}`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {unreported > 0 ? <div className="cx-warn"><span className="cx-warn-k">{tx.noUsage}</span><p>{tx.noUsageNote}</p></div> : null}
            </section>

            <div className="cx-note"><span className="cx-note-k">{tx.noteK}</span><p>{display(d.rates_note, lang)}</p></div>
          </>
        )}
    </div>
  )
}
