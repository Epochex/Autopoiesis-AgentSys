import { useEffect, useState } from 'react'
import type { Lang } from '../i18n'
import type { DataStats, SubnetGraph, Topology } from '../types'
import { TopologyCanvas } from './TopologyCanvas'
import { BenchmarkPage } from './BenchmarkPage'

/* ── SCENARIO 2 · 态势-bench — REUSES TopologyCanvas, verbatim ─────────────────
 * GET /api/rca/bench-snapshot returns the offline replay's evolved memory graph
 * mapped into the same Topology + SubnetGraph shapes the network console uses.
 * We render the
 * identical TopologyCanvas in its drilled state (drillSub === graph.cidr), so the
 * benchmark posture is the exact same constellation UI/UX as the internal-network
 * 态势 with a swapped data source. Read-only: RCA callbacks are no-ops. The
 * LongMemEval / ITBench detail rides below, embedded. */

type Snap = { ok: boolean; topology: Topology; stats: DataStats; graph: SubnetGraph; cidr: string }
type St = { s: 'load' } | { s: 'err' } | { s: 'ok'; d: Snap }

export function BenchConsole({ lang }: { lang: Lang }) {
  const zh = lang === 'zh'
  const [st, setSt] = useState<St>({ s: 'load' })
  const [hoverDev, setHoverDev] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetch('/api/rca/bench-snapshot', { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error())))
      .then((d: Snap) => { if (alive) setSt(d && d.ok ? { s: 'ok', d } : { s: 'err' }) })
      .catch(() => { if (alive) setSt({ s: 'err' }) })
    return () => { alive = false }
  }, [])

  const noop = () => {}

  return (
    <>
      {st.s === 'ok' ? (
        <section className="canvas-wrap mid">
          <TopologyCanvas
            topo={st.d.topology}
            stats={st.d.stats}
            activeKey=""
            drillSub={st.d.cidr}
            drillDev={null}
            tempo={1}
            marks={{}}
            threat={null}
            lang={lang}
            meshCount={0}
            meshLoading={false}
            hover3D={null}
            hover3DCidr={null}
            topoAlert={null}
            wan={null}
            graph={st.d.graph}
            graphAnalysis={null}
            hoverDev={hoverDev}
            onHoverDev={setHoverDev}
            onGraphAnalyze={noop}
            onCloseGraphAnalysis={noop}
            onWan={noop}
            onCloseWan={noop}
            onHoverSubnet={undefined}
            onOpen3D={noop}
            onCloseThreat={noop}
            onSub={noop}
            onDev={noop}
            onBatch={noop}
            onDiagnose={noop}
            theater={null}
            allGraphs={{}}
            onCloseTheater={noop}
            onOpenTheater={noop}
          />
        </section>
      ) : st.s === 'load' ? (
        <div className="bm"><div className="bm-state">{zh ? '构建记忆拓扑…' : 'BUILDING MEMORY TOPOLOGY…'}</div></div>
      ) : (
        <div className="bm"><div className="bm-state">{zh ? '拓扑端点不可达 · 见下方基准明细' : 'TOPOLOGY ENDPOINT UNREACHABLE · see detail below'}</div></div>
      )}
      <BenchmarkPage lang={lang} />
    </>
  )
}
