/* ── 基准态势 · 拓扑图 — capability → benchmark → real metric, as a node graph ───
 * Not text: a node-link topology (like the live 态势) mapping the system's 4
 * capabilities to the benchmarks that cover them, each benchmark node carrying a
 * REAL headline number + honest status (真跑 / 公开参照 / 定义待跑). Hover a node
 * to light its subtree. Numbers come from the benchmark + replay payloads. */
import { useState } from 'react'
import type { BenchmarkResp } from './benchmark-data'

type Metric = { real: boolean; status: 'live' | 'ref' | 'defs'; big: string; sub: string }
type BNode = { id: string; label: string; metric: Metric }
type CNode = { id: string; label: string; covered: boolean; benches: string[] }

export function BenchTopology({ bench, replay, zh }: { bench: BenchmarkResp; replay: { heal: number; mem: number } | null; zh: boolean }) {
  const [sel, setSel] = useState<string | null>(null)
  const lme = bench.longmemeval
  const tiered = lme.systems.find((s) => s.self)
  const best = Math.max(...lme.systems.map((s) => s.recall['5']))
  const sre = bench.itbench.groups.find((g) => g.id === 'sre')
  const ciso = bench.itbench.groups.find((g) => g.id === 'ciso')
  const healPct = replay ? `${Math.round(replay.heal * 100)}%` : '—'
  const memG = replay ? `+${replay.mem}` : '—'

  const L = {
    root: 'AUTOPOIESIS',
    caps: {
      rca: zh ? '内网根因分析' : 'NETWORK RCA', sec: zh ? '自我渗透 / 安全' : 'SELF-PENTEST',
      mem: zh ? '长期记忆 / 检索' : 'MEMORY', evo: zh ? '自演化 / 技能归纳' : 'SELF-EVOLUTION',
    },
    live: zh ? '真跑' : 'LIVE', ref: zh ? '公开参照' : 'PUBLISHED', defs: zh ? '定义 · 待跑' : 'DEFS · PENDING',
    heal: zh ? '自愈' : 'heal', memL: zh ? '记忆' : 'mem', scen: zh ? '场景' : 'scen',
  }

  const caps: CNode[] = [
    { id: 'rca', label: L.caps.rca, covered: true, benches: ['replay', 'sre'] },
    { id: 'sec', label: L.caps.sec, covered: true, benches: ['ciso'] },
    { id: 'mem', label: L.caps.mem, covered: true, benches: ['lme'] },
    { id: 'evo', label: L.caps.evo, covered: false, benches: ['replay'] },
  ]
  const benches: BNode[] = [
    { id: 'replay', label: zh ? '网络RCA 自愈回放' : 'NET-RCA SELF-HEAL', metric: { real: true, status: 'live', big: `${L.heal} ${healPct}`, sub: `${L.memL} ${memG} · Redpanda` } },
    { id: 'sre', label: 'ITBench · SRE', metric: { real: false, status: 'ref', big: `SOTA ${sre?.sota_pct ?? '—'}%`, sub: zh ? '公开基线' : 'baseline' } },
    { id: 'ciso', label: 'ITBench · CISO', metric: { real: false, status: 'defs', big: `SOTA ${ciso?.sota_pct ?? '—'}%`, sub: `4 ${L.scen} · ${zh ? '真实定义' : 'real defs'}` } },
    { id: 'lme', label: 'LongMemEval-500', metric: { real: true, status: 'live', big: `${(best * 100).toFixed(0)}% · ${((tiered?.recall['5'] ?? 0) * 100).toFixed(1)}`, sub: `recall@5 · ${zh ? '本仓库' : 'ours'}/best` } },
  ]

  // geometry
  const VW = 1440, VH = 430
  const rootX = 60, rootY = VH / 2
  const capX = 360, benchX = 720, metaX = 1120
  const capY = (i: number) => 60 + i * ((VH - 120) / (caps.length - 1))
  const benchY = (i: number) => 50 + i * ((VH - 100) / (benches.length - 1))
  const cy = new Map(caps.map((c, i) => [c.id, capY(i)]))
  const by = new Map(benches.map((b, i) => [b.id, benchY(i)]))

  const edgesCap = caps.map((c) => ({ id: c.id, y: cy.get(c.id)! }))
  const edgesBench: { cap: string; bench: string; cy: number; by: number }[] = []
  caps.forEach((c) => c.benches.forEach((b) => edgesBench.push({ cap: c.id, bench: b, cy: cy.get(c.id)!, by: by.get(b)! })))

  const lit = (kind: 'cap' | 'bench', id: string) => {
    if (!sel) return true
    if (sel === id) return true
    if (kind === 'bench') return caps.some((c) => c.id === sel && c.benches.includes(id))
    if (kind === 'cap') return benches.some((b) => b.id === sel && caps.find((c) => c.id === sel)) || caps.find((c) => c.id === id)?.benches.includes(sel) || sel === id
    return true
  }
  const edgeLit = (capId: string, benchId: string) => !sel || sel === capId || sel === benchId
  const curve = (x1: number, y1: number, x2: number, y2: number) => `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}`

  return (
    <svg className="bt2" viewBox={`0 0 ${VW} ${VH}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="benchmark topology">
      {/* edges root→cap */}
      {edgesCap.map((e) => <path key={`rc${e.id}`} className={`bt2-edge${!sel || sel === e.id || caps.find((c) => c.id === e.id)?.benches.includes(sel || '') ? ' on' : ''}`} d={curve(rootX + 8, rootY, capX - 96, e.y)} />)}
      {/* edges cap→bench */}
      {edgesBench.map((e, i) => <path key={`cb${i}`} className={`bt2-edge${edgeLit(e.cap, e.bench) ? ' on' : ''}`} d={curve(capX + 96, e.cy, benchX - 104, e.by)} />)}
      {/* edges bench→metric */}
      {benches.map((b) => <path key={`bm${b.id}`} className={`bt2-edge${!sel || sel === b.id || caps.some((c) => c.id === sel && c.benches.includes(b.id)) ? ' on' : ''}`} d={curve(benchX + 104, by.get(b.id)!, metaX - 8, by.get(b.id)!)} />)}

      {/* root */}
      <g className="bt2-root">
        <rect x={rootX - 8} y={rootY - 26} width={128} height={52} rx={0} />
        <text x={rootX + 56} y={rootY - 4} textAnchor="middle" className="bt2-root-t">AUTOPOIESIS</text>
        <text x={rootX + 56} y={rootY + 13} textAnchor="middle" className="bt2-root-s">{zh ? '自演化内核' : 'self-evolving kernel'}</text>
      </g>

      {/* capability nodes */}
      {caps.map((c) => {
        const y = cy.get(c.id)!, on = lit('cap', c.id)
        return (
          <g key={c.id} className={`bt2-cap${sel === c.id ? ' sel' : ''}${on ? '' : ' dim'}`} onMouseEnter={() => setSel(c.id)} onMouseLeave={() => setSel(null)} style={{ cursor: 'pointer' }}>
            <rect x={capX - 96} y={y - 17} width={192} height={34} />
            <text x={capX} y={y + 4} textAnchor="middle" className="bt2-cap-t">{c.label}</text>
            <circle className={`bt2-dot ${c.covered ? 'ok' : 'gap'}`} cx={capX - 86} cy={y} r={4} />
          </g>
        )
      })}

      {/* benchmark nodes + metric leaves */}
      {benches.map((b) => {
        const y = by.get(b.id)!, on = lit('bench', b.id), m = b.metric
        return (
          <g key={b.id} className={`bt2-bench s-${m.status}${sel === b.id ? ' sel' : ''}${on ? '' : ' dim'}`} onMouseEnter={() => setSel(b.id)} onMouseLeave={() => setSel(null)} style={{ cursor: 'pointer' }}>
            <rect x={benchX - 104} y={y - 20} width={208} height={40} />
            <text x={benchX} y={y - 3} textAnchor="middle" className="bt2-bench-t">{b.label}</text>
            <text x={benchX} y={y + 12} textAnchor="middle" className="bt2-bench-st">{m.status === 'live' ? L.live : m.status === 'ref' ? L.ref : L.defs}</text>
            {/* metric leaf */}
            <g className="bt2-meta">
              <text x={metaX} y={y - 2} className="bt2-meta-big">{m.big}</text>
              <text x={metaX} y={y + 13} className="bt2-meta-sub">{m.sub}</text>
            </g>
          </g>
        )
      })}
    </svg>
  )
}
