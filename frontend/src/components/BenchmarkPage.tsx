import './benchmark.css'
import { useEffect, useState } from 'react'
import type { Lang } from '../i18n'
import type { BenchmarkResp, LmeSystem } from './benchmark-data'
import { ABILITY_LABEL, BENCHMARK_FIXTURE } from './benchmark-data'
import { BenchTopology } from './bench-topology'
import { MemoryConstellation } from './MemoryConstellation'

/* ── SCENARIO 2 · 基准 / BENCHMARK ─────────────────────────────────────────────
 * GET /api/rca/benchmark. LongMemEval-500 = REAL local results (this repo's
 * tiered store vs Mem0 / Reflexion / flat-vector / BM25). ITBench = published
 * reference catalog + baselines (local k8s run pending, labelled honestly).
 * Interactive: recall@k toggle, hover a system for its per-ability breakdown. */

type St = { s: 'load' } | { s: 'ok'; d: BenchmarkResp; fixture: boolean }
type K = '1' | '3' | '5' | '10'

const T = (zh: boolean) => ({
  kicker: zh ? '基准场景 · 覆盖全系统能力' : 'BENCHMARK · WHOLE-SYSTEM COVERAGE',
  loading: zh ? '拉取基准…' : 'FETCHING…',
  fixture: zh ? '内置真实结果 · 网关离线' : 'BUILT-IN REAL RESULTS · GATEWAY OFFLINE',
  cover: zh ? '覆盖图 · 能力 → 基准' : 'COVERAGE · CAPABILITY → BENCHMARK',
  covered: zh ? '已覆盖' : 'COVERED', gap: zh ? '无公开基准' : 'NO PUBLIC BENCHMARK',
  lme: zh ? 'LongMemEval-500 · 记忆检索' : 'LongMemEval-500 · MEMORY RETRIEVAL',
  live: zh ? '真实运行' : 'LIVE', ref: zh ? '公开基线' : 'PUBLISHED REF',
  atk: zh ? '召回@' : 'recall@', mine: zh ? '本仓库' : 'THIS REPO',
  ability: zh ? '分能力 recall@5 · 悬停系统查看' : 'per-ability recall@5 · hover a system',
  itb: zh ? 'ITBench · RCA + 安全运营' : 'ITBench · RCA + SECURITY OPS',
  scen: zh ? '场景' : 'scenarios', sota: zh ? 'SOTA 解决率' : 'SOTA solve', maps: zh ? '对应' : 'maps to',
  paper: 'arXiv', repo: 'GitHub', board: zh ? '榜单' : 'leaderboard',
  hint: zh ? '记忆维度真跑真数 · 安全/RCA 维度接 ITBench(k3s 待跑)· 数字照实' : 'memory dim = real local run · RCA/security via ITBench (k3s pending) · numbers as-is',
})

export function BenchmarkPage({ lang, embed = false }: { lang: Lang; embed?: boolean }) {
  const zh = lang === 'zh'
  const tx = T(zh)
  const [st, setSt] = useState<St>({ s: 'load' })
  const [k, setK] = useState<K>('5')
  const [sel, setSel] = useState<string | null>(null)
  const [rep, setRep] = useState<{ heal: number; mem: number } | null>(null)

  useEffect(() => {
    let alive = true
    setSt({ s: 'load' })
    fetch('/api/rca/benchmark?lang=' + lang, { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('http'))))
      .then((d: BenchmarkResp) => { if (alive) setSt(d && d.ok ? { s: 'ok', d, fixture: false } : { s: 'ok', d: BENCHMARK_FIXTURE, fixture: true }) })
      .catch(() => { if (alive) setSt({ s: 'ok', d: BENCHMARK_FIXTURE, fixture: true }) })
    // best-effort: real self-heal numbers for the topology (network-RCA replay)
    fetch('/api/rca/replay?lang=' + lang + '&passes=4', { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => { if (alive && j && j.ok) setRep({ heal: j.self_heal?.heal_rate ?? j.summary?.accuracy_warm ?? 0, mem: j.summary?.memory_grown ?? 0 }) })
      .catch(() => {})
    return () => { alive = false }
  }, [lang])

  if (st.s === 'load') return <div className="bm"><div className="bm-state">{tx.loading}</div></div>
  const d = st.d
  const lme = d.longmemeval
  const systems = [...lme.systems].sort((a, b) => b.recall[k] - a.recall[k])
  const maxR = Math.max(...systems.map((s) => s.recall[k]), 1e-9)
  const active: LmeSystem = lme.systems.find((s) => s.id === sel) ?? lme.systems.find((s) => s.self) ?? lme.systems[0]
  const pct = (v: number) => (v * 100).toFixed(1)

  return (
    <div className="bm">
      {!embed ? (
        <header className="bm-head">
          <div className="bm-head-code">
            {st.fixture ? <span className="bm-sample">{tx.fixture}</span> : null}<span>{tx.kicker}</span>
          </div>
          <h1 className="bm-title">{zh ? <>基<mark>准</mark></> : <>BENCH<mark>MARK</mark></>}</h1>
        </header>
      ) : null}

      {/* full expanded memory graph — shown standalone; under BenchConsole the live
          TopologyCanvas already renders this constellation, so skip it when embedded */}
      {!embed ? (
        <section className="bm-sec">
          <div className="bm-sec-k">{zh ? '基准态势 · 记忆星座图(自愈跑出的真实知识图,全展开)' : 'BENCHMARK POSTURE · MEMORY CONSTELLATION (real evolved graph)'}<span className="bm-badge live">{zh ? '真跑' : 'LIVE'}</span></div>
          <MemoryConstellation lang={lang} />
        </section>
      ) : null}

      {/* capability → benchmark → real metric map */}
      <section className="bm-sec">
        <div className="bm-sec-k">{zh ? '能力 → 基准 → 真实指标 映射' : 'CAPABILITY → BENCHMARK → METRIC MAP'}</div>
        <div className="bm-topowrap"><BenchTopology bench={d} replay={rep} zh={zh} /></div>
      </section>

      {/* coverage map */}
      <section className="bm-sec">
        <div className="bm-sec-k">{tx.cover}</div>
        <div className="bm-cover">
          {d.coverage.map((c, i) => (
            <div key={i} className={`bm-cov${c.covered ? '' : ' gap'}`}>
              <span className="bm-cov-cap">{zh ? c.cap.zh : c.cap.en}</span>
              <span className="bm-cov-arrow">→</span>
              <span className="bm-cov-b">{typeof c.benchmark === 'string' ? c.benchmark : (zh ? c.benchmark.zh : c.benchmark.en)}</span>
              <span className={`bm-cov-flag${c.covered ? ' ok' : ''}`}>{c.covered ? tx.covered : tx.gap}</span>
            </div>
          ))}
        </div>
      </section>

      {/* LongMemEval — real */}
      <section className="bm-sec">
        <div className="bm-sec-k">{tx.lme}<span className="bm-badge live">{tx.live} · n={lme.n}</span></div>
        <div className="bm-ktabs">
          <span className="bm-ktabs-l">{tx.atk}</span>
          {lme.ks.map((kk) => <button key={kk} className={`bm-ktab${String(kk) === k ? ' on' : ''}`} onClick={() => setK(String(kk) as K)}>{kk}</button>)}
        </div>
        <div className="bm-sys" onMouseLeave={() => setSel(null)}>
          {systems.map((s) => (
            <div key={s.id} className={`bm-row${s.self ? ' self' : ''}${sel === s.id ? ' on' : ''}`} onMouseEnter={() => setSel(s.id)}>
              <span className="bm-row-nm">{s.label}{s.self ? <i className="bm-mine">{tx.mine}</i> : null}</span>
              <span className="bm-row-track"><span className="bm-row-bar" style={{ width: `${(s.recall[k] / maxR) * 100}%` }} /></span>
              <span className="bm-row-v">{pct(s.recall[k])}<i>%</i></span>
            </div>
          ))}
        </div>
        <p className="bm-note">{zh ? lme.note.zh : lme.note.en}</p>
        <div className="bm-ability">
          <div className="bm-ability-k">{tx.ability} · <b>{active.label}</b></div>
          <div className="bm-ability-grid">
            {lme.abilities.map((ab) => (
              <div key={ab} className="bm-ab">
                <span className="bm-ab-l">{zh ? ABILITY_LABEL[ab]?.zh ?? ab : ABILITY_LABEL[ab]?.en ?? ab}</span>
                <span className="bm-ab-track"><span className="bm-ab-bar" style={{ width: `${(active.by_ability5[ab] ?? 0) * 100}%` }} /></span>
                <span className="bm-ab-v">{pct(active.by_ability5[ab] ?? 0)}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ITBench — published reference */}
      <section className="bm-sec">
        <div className="bm-sec-k">{tx.itb}<span className="bm-badge ref">{zh ? d.itbench.status.zh : d.itbench.status.en}</span></div>
        <div className="bm-itb-links">
          <b>{d.itbench.total_scenarios}</b> {tx.scen} ·
          <a href={d.itbench.paper_url} target="_blank" rel="noreferrer">{tx.paper}</a>
          <a href={d.itbench.repo_url} target="_blank" rel="noreferrer">{tx.repo}</a>
          <a href={d.itbench.leaderboard_url} target="_blank" rel="noreferrer">{tx.board}</a>
        </div>
        <div className="bm-groups">
          {d.itbench.groups.map((g) => (
            <div key={g.id} className="bm-group">
              <span className="bm-group-l">{zh ? g.label.zh : g.label.en}</span>
              <span className="bm-group-maps">{tx.maps} {zh ? g.maps_to.zh : g.maps_to.en}</span>
              <span className="bm-group-track"><span className="bm-group-bar" style={{ width: `${g.sota_pct}%` }} /></span>
              <span className="bm-group-v">{tx.sota} {g.sota_pct}<i>%</i></span>
            </div>
          ))}
        </div>
        <p className="bm-note">{zh ? d.itbench.note.zh : d.itbench.note.en}</p>
      </section>

      <div className="bm-hint">{tx.hint}</div>
    </div>
  )
}
