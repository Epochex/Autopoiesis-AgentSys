import { useMemo, useState } from 'react'
import type { MeshNode } from '../types'
import type { Lang } from '../i18n'

type Pt = { x: number; y: number }
const W = 1180
const H = 620
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)
const rad = (out: number) => Math.max(5, Math.min(20, 4 + Math.log10(out + 1) * 4))

const ROLE_COLOR: Record<string, string> = {
  'netbios/win': '#ffb347',
  'camera/dvr': '#ff6b5e',
  'qq/im': '#5fe4d1',
  dns: '#9d8bff',
  'upnp/ssdp': '#5fb0ff',
  'iot/probe': '#ff6b5e',
  web: '#5fe4d1',
  voip: '#ffd24d',
  host: '#7c8f8a',
}
const tcolor = (t: string) => (t === 'high' ? '#ff5a6a' : t === 'watch' ? '#ffb347' : '#5fe4d1')

export function MeshView({ nodes, cidr, lang, onClose }: { nodes: MeshNode[]; cidr: string; lang: Lang; onClose: () => void }) {
  const [sel, setSel] = useState<string | null>(null)

  const { pos, edges, roles } = useMemo(() => {
    const byRole = new Map<string, MeshNode[]>()
    for (const n of nodes) (byRole.get(n.role) ?? byRole.set(n.role, []).get(n.role)!).push(n)
    const roleList = [...byRole.keys()]
    const cx = W / 2
    const cy = H / 2
    const R = Math.min(cx, cy) - 120
    const pos: Record<string, Pt> = {}
    roleList.forEach((role, ri) => {
      const a = (ri / roleList.length) * Math.PI * 2 - Math.PI / 2
      const rc: Pt = { x: cx + Math.cos(a) * R, y: cy + Math.sin(a) * R }
      const members = byRole.get(role)!
      members.forEach((m, mi) => {
        const ma = (mi / Math.max(1, members.length)) * Math.PI * 2
        const sr = 14 + members.length * 6
        pos[m.ip] = { x: rc.x + Math.cos(ma) * sr, y: rc.y + Math.sin(ma) * sr }
      })
    })
    // behavioral edges: share >=1 target port
    const edges: { a: string; b: string; w: number }[] = []
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const shared = nodes[i].ports.filter((p) => nodes[j].ports.includes(p)).length
        if (shared > 0) edges.push({ a: nodes[i].ip, b: nodes[j].ip, w: shared })
      }
    }
    const roleCenters: { role: string; p: Pt }[] = roleList.map((role, ri) => {
      const a = (ri / roleList.length) * Math.PI * 2 - Math.PI / 2
      return { role, p: { x: cx + Math.cos(a) * R, y: cy + Math.sin(a) * R } }
    })
    return { pos, edges, roles: roleCenters }
  }, [nodes])

  const selNode = nodes.find((n) => n.ip === sel)

  return (
    <section className="mesh-section">
      <div className="mesh-head">
        <span className="mesh-kicker">{lang === 'zh' ? '设备行为网格' : 'device behavioral mesh'} · {cidr} · {nodes.length} {lang === 'zh' ? '活跃设备' : 'active'}</span>
        <span className="mesh-note">{lang === 'zh' ? '边 = 共享扫描目标端口（行为相似）；同网段直连流量本地交换、防火墙不可见' : 'edges = shared scan-target ports (behavioral); same-subnet direct traffic is locally switched, invisible to the firewall'}</span>
        <button className="tc-x" onClick={onClose}>✕</button>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="mesh-svg" preserveAspectRatio="xMidYMid meet">
        {edges.map((e, i) => {
          const a = pos[e.a]
          const b = pos[e.b]
          if (!a || !b) return null
          const hot = sel === e.a || sel === e.b
          return (
            <g key={i}>
              <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={`mesh-edge ${hot ? 'hot' : ''}`} style={{ strokeWidth: Math.min(2.5, 0.5 + e.w * 0.6) }} />
              {hot ? (
                <circle r="2.6" className="mesh-pulse">
                  <animateMotion dur="1.6s" repeatCount="indefinite" path={`M${a.x} ${a.y} L ${b.x} ${b.y}`} />
                </circle>
              ) : null}
            </g>
          )
        })}
        {roles.map((r) => (
          <text key={r.role} x={r.p.x} y={r.p.y - (r.role.length ? 46 : 40)} className="mesh-role" textAnchor="middle">{r.role}</text>
        ))}
        {nodes.map((n) => {
          const p = pos[n.ip]
          if (!p) return null
          const on = sel === n.ip
          return (
            <g key={n.ip} className="mesh-node" onClick={() => setSel(on ? null : n.ip)} style={{ cursor: 'pointer' }}>
              <circle cx={p.x} cy={p.y} r={rad(n.out)} fill={ROLE_COLOR[n.role] ?? '#7c8f8a'} fillOpacity={0.18} stroke={tcolor(n.threat)} strokeWidth={on ? 2.5 : 1.2} />
              {on ? <text x={p.x} y={p.y - rad(n.out) - 6} className="mesh-ip" textAnchor="middle">{n.ip}</text> : null}
            </g>
          )
        })}
      </svg>
      {selNode ? (
        <div className="mesh-profile">
          <strong>{selNode.ip}</strong>
          <span className="mp-role" style={{ color: ROLE_COLOR[selNode.role] }}>{selNode.role}</span>
          <span>out {short(selNode.out)} · deny {short(selNode.deny)} · ok {selNode.accept}</span>
          <span className="mp-ports">{selNode.ports.map((p) => `:${p}`).join(' ')}</span>
        </div>
      ) : null}
    </section>
  )
}
