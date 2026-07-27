import './memory-constellation.css'
import { useEffect, useMemo, useState } from 'react'
import type { Lang } from '../i18n'

/* ── 基准态势 · 记忆星座图 — the REAL evolved memory graph, fully expanded ────────
 * GET /api/rca/memory-graph → nodes (episodic / semantic / procedural records +
 * insight hubs) + real links/relations. Laid out as tier clusters with edges,
 * like the live device-relation constellation. Hover a node → its neighbourhood
 * lights + the real record text shows. Every node/edge is a real memory record. */

type GNode = { id: string; tier: string; label: string; text: string; strength: number; importance: number; tags: string[]; assets: string[] }
type GEdge = { src: string; dst: string; type: string }
type Graph = { ok: boolean; nodes: GNode[]; edges: GEdge[]; stats: { records: number; edges: number; insights: number; by_tier: Record<string, number> } }
type St = { s: 'load' } | { s: 'err' } | { s: 'ok'; g: Graph }

const TIER: Record<string, { zh: string; en: string; cls: string }> = {
  episodic: { zh: '情节记忆', en: 'EPISODIC', cls: 'epi' },
  semantic: { zh: '语义记忆', en: 'SEMANTIC', cls: 'sem' },
  procedural: { zh: '程序记忆', en: 'PROCEDURAL', cls: 'proc' },
  insight: { zh: '洞见', en: 'INSIGHT', cls: 'ins' },
}
const VW = 1440, VH = 600
const CLUSTER: Record<string, { x: number; y: number; r: number }> = {
  episodic: { x: 340, y: 300, r: 150 },
  semantic: { x: 770, y: 210, r: 165 },
  procedural: { x: 1150, y: 300, r: 150 },
  insight: { x: 760, y: 470, r: 60 },
}

export function MemoryConstellation({ lang }: { lang: Lang }) {
  const zh = lang === 'zh'
  const [st, setSt] = useState<St>({ s: 'load' })
  const [sel, setSel] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setSt({ s: 'load' })
    fetch('/api/rca/memory-graph?passes=4', { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error())))
      .then((g: Graph) => { if (alive) setSt(g && g.ok ? { s: 'ok', g } : { s: 'err' }) })
      .catch(() => { if (alive) setSt({ s: 'err' }) })
    return () => { alive = false }
  }, [])

  const pos = useMemo(() => {
    if (st.s !== 'ok') return new Map<string, { x: number; y: number }>()
    const byTier = new Map<string, GNode[]>()
    for (const n of st.g.nodes) { const a = byTier.get(n.tier) ?? []; a.push(n); byTier.set(n.tier, a) }
    const m = new Map<string, { x: number; y: number }>()
    for (const [tier, arr] of byTier) {
      const c = CLUSTER[tier] ?? { x: VW / 2, y: VH / 2, r: 120 }
      arr.forEach((n, i) => {
        // sunflower packing inside the cluster circle (deterministic, organic)
        const ang = i * 2.399963
        const rr = c.r * Math.sqrt((i + 0.6) / Math.max(1, arr.length))
        m.set(n.id, { x: c.x + Math.cos(ang) * rr, y: c.y + Math.sin(ang) * rr })
      })
    }
    return m
  }, [st])

  if (st.s === 'load') return <div className="mc"><div className="mc-state">{zh ? '运行真实自演化流,构建记忆图…' : 'BUILDING MEMORY GRAPH…'}</div></div>
  if (st.s === 'err') return <div className="mc"><div className="mc-state">{zh ? '记忆图端点不可达' : 'MEMORY-GRAPH ENDPOINT UNREACHABLE'}</div></div>
  const g = st.g
  const nodeById = new Map(g.nodes.map((n) => [n.id, n]))
  const neigh = new Set<string>()
  if (sel) { neigh.add(sel); for (const e of g.edges) { if (e.src === sel) neigh.add(e.dst); if (e.dst === sel) neigh.add(e.src) } }
  const selNode = sel ? nodeById.get(sel) : null
  const maxStr = Math.max(0.5, ...g.nodes.map((n) => n.strength))
  const rOf = (n: GNode) => 6 + (n.strength / maxStr) * 12

  return (
    <div className="mc">
      <div className="mc-legend">
        {Object.entries(TIER).map(([k, v]) => g.stats.by_tier?.[k] || k === 'insight' ? (
          <span key={k} className={`mc-leg ${v.cls}`}><i /> {zh ? v.zh : v.en}{g.stats.by_tier?.[k] ? ` ×${g.stats.by_tier[k]}` : ''}</span>
        ) : null)}
        <span className="mc-leg-stat">{g.stats.records} {zh ? '记忆' : 'records'} · {g.stats.edges} {zh ? '关系边' : 'links'} · {g.stats.insights} {zh ? '洞见' : 'insights'}</span>
      </div>

      <div className="mc-stage">
        <svg className="mc-svg" viewBox={`0 0 ${VW} ${VH}`} preserveAspectRatio="xMidYMid meet" onMouseLeave={() => setSel(null)}>
          {/* cluster halos + labels */}
          {Object.entries(CLUSTER).map(([tier, c]) => (g.stats.by_tier?.[tier] || tier === 'insight') ? (
            <g key={tier} className={`mc-cluster ${TIER[tier]?.cls}`}>
              <ellipse cx={c.x} cy={c.y} rx={c.r + 26} ry={c.r + 18} />
              <text x={c.x} y={c.y - c.r - 24} textAnchor="middle" className="mc-cluster-l">{zh ? TIER[tier]?.zh : TIER[tier]?.en}</text>
            </g>
          ) : null)}

          {/* edges */}
          <g className="mc-edges">
            {g.edges.map((e, i) => {
              const a = pos.get(e.src), b = pos.get(e.dst)
              if (!a || !b) return null
              const on = !sel || e.src === sel || e.dst === sel
              return <line key={i} className={`mc-edge${e.type === 'link' ? ' link' : ''}${on ? ' on' : ' mute'}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} />
            })}
          </g>

          {/* nodes */}
          <g className="mc-nodes">
            {g.nodes.map((n) => {
              const p = pos.get(n.id); if (!p) return null
              const dim = sel && !neigh.has(n.id)
              return (
                <g key={n.id} className={`mc-node ${TIER[n.tier]?.cls ?? 'sem'}${sel === n.id ? ' sel' : ''}${dim ? ' dim' : ''}`}
                  transform={`translate(${p.x} ${p.y})`} onMouseEnter={() => setSel(n.id)} style={{ cursor: 'pointer' }}>
                  <circle r={rOf(n)} />
                  {rOf(n) >= 13 || sel === n.id ? <text className="mc-node-l" y={-rOf(n) - 4} textAnchor="middle">{n.label}</text> : null}
                </g>
              )
            })}
          </g>
        </svg>

        {/* detail readout — real record content on hover */}
        <aside className={`mc-detail${selNode ? ' on' : ''}`}>
          {selNode ? (
            <>
              <div className={`mc-detail-tier ${TIER[selNode.tier]?.cls}`}>{zh ? TIER[selNode.tier]?.zh : TIER[selNode.tier]?.en}</div>
              <div className="mc-detail-id">{selNode.label}</div>
              <p className="mc-detail-text">{selNode.text || selNode.id}</p>
              {selNode.tags.length ? <div className="mc-detail-tags">{selNode.tags.map((t) => <code key={t}>{t}</code>)}</div> : null}
              {selNode.assets.length ? <div className="mc-detail-assets">{zh ? '资产' : 'assets'}: {selNode.assets.join(' · ')}</div> : null}
              <div className="mc-detail-str">{zh ? '强度' : 'strength'} {selNode.strength.toFixed(2)} · {zh ? '重要度' : 'importance'} {selNode.importance.toFixed(2)}</div>
            </>
          ) : <div className="mc-detail-hint">{zh ? '悬停任一记忆节点,查看真实内容与它的关系边' : 'Hover a memory node for its real content + links'}</div>}
        </aside>
      </div>
    </div>
  )
}
