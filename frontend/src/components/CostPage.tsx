import './cost.css'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Lang } from '../i18n'

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
  grounded_rca_benchmark_v1: ['跑基准测试', 'BENCHMARK RUN'],
  diag: ['测试调用', 'TEST CALL'],
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
  cacheWhy: zh
    ? '缓存里存着的这些回答,重新部署以后可以直接拿来用,这部分就不用再花钱问模型第二遍。'
    : 'Answers already in the cache are reused after a redeploy, so that much never has to be paid for a second time.',
  cacheNone: zh ? '现在一条都没缓存' : 'NOTHING CACHED YET',
  cacheOff: zh ? '读不到缓存状态' : 'CACHE STATUS UNAVAILABLE',
  noteK: zh ? '说明' : 'NOTE',
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

const bytes = (n: number) => {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

const ago = (sec: number | null, zh: boolean) => {
  if (sec === null) return '—'
  if (sec < 60) return zh ? `${Math.round(sec)} 秒前` : `${Math.round(sec)}s ago`
  if (sec < 3600) return zh ? `${Math.round(sec / 60)} 分钟前` : `${Math.round(sec / 60)}m ago`
  return zh ? `${Math.round(sec / 3600)} 小时前` : `${Math.round(sec / 3600)}h ago`
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
      // A superseded request leaves the flags to whoever replaced it.
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

  const chart = useMemo(
    () => (d ? buildBars(d.by_hour, d.window_hours) : { bars: [] as Bar[], daily: false }),
    [d],
  )
  const peakCost = useMemo(
    () => chart.bars.reduce((top, b) => (b.cost > top ? b.cost : top), 0),
    [chart],
  )
  // Printed on the chart so the strip can be checked against the headline
  // total: if the axis ever failed to cover the whole window, the two figures
  // would stop agreeing.
  const barsSum = useMemo(() => chart.bars.reduce((sum, b) => sum + b.cost, 0), [chart])
  // Named in text as well as drawn, so no figure on this page exists only as a
  // bar height.
  const topBars = useMemo(
    () => chart.bars.filter((b) => b.cost > 0).sort((a, b) => b.cost - a.cost).slice(0, 3),
    [chart],
  )

  const purposes = useMemo(() => Object.entries(d?.by_purpose ?? {}), [d])
  const models = useMemo(() => Object.entries(d?.by_model ?? {}), [d])
  const unreported = useMemo(() => (d?.recent ?? []).filter((r) => !r.usage_reported).length, [d])
  const namespaces = useMemo(
    () => (cacheSt.s === 'ok' ? Object.entries(cacheSt.d.namespaces) : []),
    [cacheSt],
  )

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
              <button
                key={w.hours}
                type="button"
                className={`cx-win-b${w.hours === hours ? ' is-on' : ''}`}
                aria-pressed={w.hours === hours}
                onClick={() => setHours(w.hours)}
              >
                {zh ? w.zh : w.en}
              </button>
            ))}
          </div>
          <div className="cx-fresh">
            <span className="cx-fresh-k">{tx.lastAt}</span>
            <b>{refreshedAt === null ? '—' : clock(refreshedAt)}</b>
            <span className="cx-fresh-a">{tx.auto}</span>
            <button type="button" className="cx-fresh-b" onClick={refresh} disabled={pending}>
              {pending ? tx.refreshing : tx.refresh}
            </button>
          </div>
        </div>
      </header>

      {/* Independent of the spend window: it says whether calls are even
          switched on, which is the first thing a zero total needs explained. */}
      <section className="cx-cache">
        <div className="cx-cache-k">{tx.switches}</div>
        {cacheSt.s !== 'ok' ? (
          <div className="cx-cache-r">
            <span className="cx-state is-inline">{cacheSt.s === 'load' ? tx.loading : tx.cacheOff}</span>
          </div>
        ) : (
          <>
            <div className="cx-cache-r">
              <span className={`cx-sw ${cacheSt.d.calls_enabled ? 'is-on' : 'is-off'}`}>
                <em>{tx.callsSw}</em><b>{cacheSt.d.calls_enabled ? tx.on : tx.off}</b>
              </span>
              <span className={`cx-sw ${cacheSt.d.prewarm_enabled ? 'is-on' : 'is-off'}`}>
                <em>{tx.prewarmSw}</em><b>{cacheSt.d.prewarm_enabled ? tx.on : tx.off}</b>
              </span>
              <span className="cx-sw">
                <em>{tx.cached}</em><b className="n">{num(cacheSt.d.entries)}</b>
              </span>
              <span className="cx-cache-dir"><em>{tx.cacheDir}</em><code>{cacheSt.d.dir}</code></span>
            </div>
            <div className="cx-ns">
              {namespaces.length === 0 ? (
                <span className="cx-ns-none">{tx.cacheNone}</span>
              ) : (
                namespaces.map(([name, ns]) => (
                  <span className="cx-ns-i" key={name}>
                    <b>{name}</b>
                    <i>{num(ns.entries)}</i>
                    <i>{bytes(ns.bytes)}</i>
                    <i>{ago(ns.newest_age_sec, zh)}</i>
                  </span>
                ))
              )}
            </div>
            <p className="cx-cache-p">{tx.cacheWhy}</p>
          </>
        )}
      </section>

      {st.s === 'load' ? <div className="cx-state">{tx.loading}</div>
        : st.s === 'err' ? <div className="cx-state is-err">{tx.offline} · {st.m}</div>
        : !d ? null
        : d.calls === 0 ? (
          <>
            <div className="cx-state">{tx.empty}</div>
            <div className="cx-note"><span className="cx-note-k">{tx.noteK}</span><p>{d.rates_note}</p></div>
          </>
        ) : (
          <>
            <section className="cx-figs">
              <div className="cx-fig is-money">
                <b><i>¥</i>{money(d.total_cost_cny, d.total_cost_cny >= 10 ? 2 : 4)}</b>
                <span>{tx.totalCost(d.window_hours)}</span>
              </div>
              <div className="cx-fig"><b>{num(d.calls)}</b><span>{tx.calls}</span></div>
              <div className="cx-fig"><b>{num(d.total_tokens)}</b><span>{tx.tokens}</span></div>
              <div className="cx-fig"><b>{num(d.average_tokens_per_call)}</b><span>{tx.avg}</span></div>
            </section>

            <section className="cx-sec">
              <div className="cx-sec-k">{tx.spent} <em>{tx.spentK}</em></div>
              <div className="cx-scroll">
                <table className="cx-t cx-t-spend">
                  <thead>
                    <tr>
                      <th scope="col">{tx.feature}</th>
                      <th scope="col" className="n">{tx.calls}</th>
                      <th scope="col" className="n">{tx.tokens}</th>
                      <th scope="col" className="n">{tx.cost}</th>
                      <th scope="col">{tx.shareK}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {purposes.map(([key, b], i) => {
                      const share = d.total_cost_cny > 0 ? b.cost_cny / d.total_cost_cny : 0
                      return (
                        <tr key={key} className={i === 0 ? 'is-top' : undefined}>
                          <th scope="row">
                            {purposeLabel(key, zh)}
                            <code>{key}</code>
                          </th>
                          <td className="n">{num(b.calls)}</td>
                          <td className="n">{num(b.tokens)}</td>
                          <td className="n strong">{money(b.cost_cny, 4)}</td>
                          <td className="cx-share-c">
                            <span className="cx-share" aria-hidden="true">
                              <i style={{ width: `${share > 0 ? Math.max(share * 100, 1) : 0}%` }} />
                            </span>
                            <em>{(share * 100).toFixed(1)}%</em>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="cx-sec">
              <div className="cx-sec-k">{tx.models}</div>
              <div className="cx-models">
                {models.map(([key, b]) => (
                  <div className="cx-model" key={key}>
                    <b>{key}</b>
                    <span>{num(b.calls)} {tx.callsUnit}</span>
                    <span>{num(b.tokens)} tokens</span>
                    <span className="cx-model-c">¥{money(b.cost_cny, 4)}</span>
                  </div>
                ))}
              </div>
            </section>

            <section className="cx-sec">
              <div className="cx-sec-k">{chart.daily ? tx.chartDay : tx.chart}</div>
              <div className="cx-chart">
                {peakCost <= 0 ? (
                  <div className="cx-state is-inline">{tx.chartEmpty}</div>
                ) : (
                  <>
                    <div className="cx-chart-h">
                      <span className="cx-chart-k">{tx.barsSum}</span>
                      <b>¥{money(barsSum, 4)}</b>
                      <span className="cx-chart-n">
                        {chart.daily ? tx.cellsDay(chart.bars.length) : tx.cellsHour(chart.bars.length)}
                      </span>
                    </div>
                    <div className="cx-plot">
                      <div className="cx-plot-ax" aria-hidden="true">
                        <span>¥{money(peakCost, 4)}</span>
                        <span>¥{money(peakCost / 2, 4)}</span>
                        <span>¥0</span>
                      </div>
                      <div className="cx-plot-in">
                        <div className="cx-plot-mid" aria-hidden="true" />
                        <ul className="cx-bars">
                          {chart.bars.map((b) => {
                            const text = `${b.label} · ¥${money(b.cost, 4)}`
                            return (
                              <li
                                key={b.key}
                                className={`cx-bar${b.cost > 0 && b.cost === peakCost ? ' is-peak' : ''}`}
                                title={text}
                              >
                                <span
                                  className="cx-bar-f"
                                  aria-hidden="true"
                                  style={{ height: b.cost > 0 ? `${Math.max((b.cost / peakCost) * 100, 2)}%` : '0' }}
                                />
                                <span className="cx-sr">{text}</span>
                              </li>
                            )
                          })}
                        </ul>
                      </div>
                    </div>
                    <div className="cx-chart-f">
                      <span>{chart.bars[0]?.label ?? '—'}</span>
                      <span>{chart.bars[chart.bars.length - 1]?.label ?? '—'}</span>
                    </div>
                    <p className="cx-chart-top">
                      <span className="cx-chart-k">{chart.daily ? tx.topDays : tx.topHours}</span>
                      {topBars.map((b) => (
                        <span className="cx-chart-t" key={b.key}><b>{b.label}</b> ¥{money(b.cost, 4)}</span>
                      ))}
                    </p>
                  </>
                )}
              </div>
            </section>

            <section className="cx-sec">
              <div className="cx-sec-k">{tx.biggest}</div>
              {d.largest_call === null ? (
                <div className="cx-state is-inline">{tx.biggestNone}</div>
              ) : (
                <div className="cx-big">
                  <div className="cx-big-c">
                    <b><i>¥</i>{money(d.largest_call.cost_cny, 4)}</b>
                    <span>{num(d.largest_call.total_tokens)} tokens</span>
                  </div>
                  <dl className="cx-big-d">
                    <div><dt>{tx.when}</dt><dd>{stamp(d.largest_call.at)}</dd></div>
                    <div>
                      <dt>{tx.feature}</dt>
                      <dd>{purposeLabel(d.largest_call.purpose, zh)} <code>{d.largest_call.purpose}</code></dd>
                    </div>
                    <div><dt>{tx.model}</dt><dd>{d.largest_call.model}</dd></div>
                  </dl>
                </div>
              )}
            </section>

            <section className="cx-sec">
              <div className="cx-sec-k">{tx.recent}</div>
              <div className="cx-scroll">
                <table className="cx-t cx-t-recent">
                  <thead>
                    <tr>
                      <th scope="col">{tx.when}</th>
                      <th scope="col">{tx.feature}</th>
                      <th scope="col">{tx.model}</th>
                      <th scope="col" className="n">{tx.inTok}</th>
                      <th scope="col" className="n">{tx.outTok}</th>
                      <th scope="col" className="n">{tx.cost}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.recent.map((r, i) => (
                      <tr key={`${r.at}-${i}`} className={r.usage_reported ? undefined : 'is-nousage'}>
                        <td>{stamp(r.at)}</td>
                        <td>{purposeLabel(r.purpose, zh)}</td>
                        <td>{r.model}</td>
                        <td className="n">{num(r.prompt_tokens)}</td>
                        <td className="n">{num(r.completion_tokens)}</td>
                        <td className="n strong">
                          {money(r.cost_cny, 4)}
                          {r.usage_reported ? null : <em className="cx-flag">{tx.noUsage}</em>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {unreported > 0 ? (
                <div className="cx-warn"><span className="cx-warn-k">{tx.noUsage}</span><p>{tx.noUsageNote}</p></div>
              ) : null}
            </section>

            {/* The ledger's own caveat, printed as written. */}
            <div className="cx-note">
              <span className="cx-note-k">{tx.noteK}</span>
              <p>{d.rates_note}</p>
            </div>
          </>
        )}
    </div>
  )
}
