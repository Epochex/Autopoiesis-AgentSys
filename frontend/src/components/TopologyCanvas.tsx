import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DataStats, Device, GraphAnalysis, Subnet, SubnetGraph, TheaterEvent, Topology } from '../types'
import { Scramble } from './Motion'
import { SubnetGraphLayer } from './SubnetGraph'
import { TheaterStage } from './TheaterStage'
import { Analyzing, type Threat, type WanThreat } from './ThreatCard'
import type { Lang } from '../i18n'

type Pt = { x: number; y: number }
/* The plate is a ~1.9:1 letterbox at every supported viewport (1920x1006,
 * 1440x826). The old 1360x1000 viewBox (1.36:1) could only ever be fitted by
 * HEIGHT, so ~550px of the 1920 plate was structural letterboxing — the "dead
 * space" was the viewBox, not the layout. 1920x1000 fits the plate. */
const VBW = 1920
const VBH = 1000
const bez = (a: Pt, b: Pt) => `M${a.x} ${a.y} C ${(a.x + b.x) / 2} ${a.y}, ${(a.x + b.x) / 2} ${b.y}, ${b.x} ${b.y}`
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)
const clipS = (s: string, n: number) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s)
const weight = (f: number) => Math.max(1, Math.min(5.5, 1 + Math.log10(f + 1) * 0.7))
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

function group(key: string): 'attack' | 'deny' | 'health' {
  if (key === 'admin_bruteforce_lockout') return 'attack'
  if (key === 'internal_policy_deny_expected' || key === 'device_service_port_probe_contained') return 'deny'
  return 'health'
}

function Edge({ a, b, tone, flows, dim, hot, hero, delay, tempo }: { a: Pt; b: Pt; tone: string; flows: number; dim: boolean; hot?: boolean; hero?: boolean; delay: number; tempo: number }) {
  const d = bez(a, b)
  const n = dim ? 1 : Math.max(1, Math.min(hero ? 8 : 5, Math.round(Math.log10(flows + 1) - 1)))
  const dur = Math.max(0.7, Math.max(1.4, 4.2 - Math.log10(flows + 1) * 0.42) / tempo)
  // Width encodes hierarchy, not just raw throughput: the hero threat path is
  // deliberately the boldest line; the supporting fan is capped thin so the eye
  // locks onto the focal core, and dimmed context is thinnest.
  const w = hero ? weight(flows) + 2.6 : dim ? Math.min(weight(flows), 2) : Math.min(weight(flows), 3.2)
  return (
    <>
      <path d={d} className={`flow-line ${tone} ${dim ? 'dim' : ''} ${hot ? 'hot' : ''} ${hero ? 'hero' : ''} appear`} style={{ strokeWidth: w + (hot ? 1.2 : 0), animationDelay: `${delay}s` }} />
      {Array.from({ length: n }).map((_, i) => (
        <circle key={i} r={dim ? 1.3 : hero ? 3.4 : hot ? 3 : 2.2} className={`pulse ${tone} ${dim ? 'dim' : ''} ${hero ? 'hero' : ''}`}>
          <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`${(i * dur) / n}s`} path={d} />
        </circle>
      ))}
    </>
  )
}

function Float({ x, y, w, lines, tone }: { x: number; y: number; w: number; lines: { k: string; v: string }[]; tone: string }) {
  return (
    <foreignObject x={x} y={clamp(y, 2, VBH - 60)} width={w} height={14 + lines.length * 19} className="float-fo">
      <div className={`float-panel ${tone}`}>
        {lines.map((l, i) => (
          <div key={i} className="float-row">
            <span className="float-k">{l.k}</span>
            <Scramble className="float-v" text={l.v} />
          </div>
        ))}
      </div>
    </foreignObject>
  )
}

type Atk = { ip: string; v: number; p: Pt }

/* WAN attack-surface deep data (lazy-loaded from /api/rca/attack_surface) powering
 * the in-place drill-down: /24 netblock clustering, per-IP attempt counts, deny-port
 * and Dahua-probe distributions, and the internal device exposure an attacker row
 * cross-links into. */
interface AsDevice { ip: string; vendor: string | null; role: string | null; deny: number | null; accept: number | null; topPorts: string[] | null; threat: string | null }
interface AsData {
  netblocks: { cidr: string; count: number; ips: [string, number][] }[]
  internalDenySrc: [string, number][]
  denyPorts: [string, number][]
  devicePortTop: [string, number][]; devicePortDeny: number
  adminLoginFailed: number; distinctSrc: number; lockouts: number
  assetExposure: { subnets: { cidr: string; exposed: AsDevice[] }[] }
}
/* the in-place offense drill focus — the topology reshapes itself per level */
type WanFocus = null | { kind: 'sources' } | { kind: 'attacker'; ip: string } | { kind: 'target' }

/* WAN ingress geometry — the tally block sits in the open WAN field, left of
 * the target it converges on. */
const TALLY = { x: 96, y: 214, cols: 26, cell: 8 }

/** WAN INGRESS — the credential assault, stated as facts.
 *
 *  The previous version scattered 84 dots through a polar annulus with a hash
 *  (`r = 268 + (hash % 1000)/1000 * 168`) to stand in for the 573 real sources.
 *  Every one of those positions was invented, and the field read as a map — a
 *  spatial claim the data cannot support. That is the exact failure the honesty
 *  rules name ("never synthesize positions-as-meaning").
 *
 *  This is a UNIT TALLY instead: ONE mark per real distinct source, laid out on
 *  a neutral grid that makes no spatial claim at all — the grid is admittedly a
 *  reading order, and it is labelled as such ("1 mark = 1 source"). The count is
 *  the message, and the count is real (`dataStats.distinctSrc`).
 *
 *  Severity is carried by ink weight and structure, never by hue: the assault is
 *  ink, the containment is stated in words + the real lockout count. */
const nfmt = (n: number) => (n ?? 0).toLocaleString('en-US')
/* deterministic per-ip jitter so the attacker field reads as an organic swarm, not
 * a rigid matrix — the same ip always lands in the same spot (no Math.random). */
const jit = (s: string, seed: number) => { let h = seed >>> 0; for (let i = 0; i < s.length; i++) h = (Math.imul(h, 31) + s.charCodeAt(i)) >>> 0; return (h % 1000) / 1000 - 0.5 }
const PORT_LAB: Record<string, string> = { '137': 'NetBIOS', '138': 'NetBIOS', '445': 'SMB', '53': 'DNS', '37777': 'Dahua DVR', '37809': 'Dahua', '37810': 'Dahua', '8000': 'HTTP-alt' }
const roleZh: Record<string, string> = { camera: '摄像头', workstation: '工作站', intercom: '门禁', mobile: '移动端', unknown: '未知' }

/** WAN INGRESS — the credential assault, now an in-place drill-down. The left field
 *  reshapes itself across focus levels rather than opening a side panel:
 *    null      the 573-mark tally + 3 named sources, whole tally clickable
 *    sources   the tally bursts into its two /24 netblocks, each IP a node
 *    attacker  one IP blooms a profile card, its attack line lit, and — when the
 *              same IP is an internal device — a cross-link drawn to the LAN side
 *    target    the admin-login target unfolds its deny-port + Dahua-probe evidence
 *  Every number is real (attack_surface); the geometry of the netblock burst is a
 *  reading layout, labelled as such, never a spatial claim. */
function WanSiege({
  core, atk, asData, wanFocus, setWanFocus, devByIp, distinctSrc, lockouts, lang, wan, onWan,
}: {
  core: Pt; atk: Atk[]; asData: AsData | null
  wanFocus: WanFocus; setWanFocus: (f: WanFocus) => void
  devByIp: Map<string, AsDevice & { cidr: string }>
  distinctSrc: number; lockouts: number; lang: Lang
  wan: WanThreat | null; onWan: (ip: string) => void
}) {
  const zh = lang === 'zh'
  const T: Pt = { x: core.x - 190, y: core.y }
  const gwFace: Pt = { x: core.x - 86, y: core.y }
  const brackets = (c: Pt, s: number, len: number) =>
    ([[-1, -1], [1, -1], [1, 1], [-1, 1]] as const).map(
      ([sx, sy]) => `M ${c.x + sx * s} ${c.y + sy * s - sy * len} L ${c.x + sx * s} ${c.y + sy * s} L ${c.x + sx * s - sx * len} ${c.y + sy * s}`,
    )
  const rows = Math.ceil(distinctSrc / TALLY.cols)
  const tallyW = TALLY.cols * TALLY.cell
  const tallyH = rows * TALLY.cell

  const focus = wanFocus
  const expanded = focus?.kind === 'sources' || focus?.kind === 'attacker'
  const blocks = asData?.netblocks ?? []
  // netblock burst: two blocks stacked in the WAN field, IPs in a row under each
  const blockGeom = blocks.map((b, bi) => {
    const cy = 190 + bi * 250
    const coordinated = b.ips.length >= 2 && b.ips.every((x) => x[1] === b.ips[0][1])
    return {
      cidr: b.cidr, count: b.count, coordinated,
      labelP: { x: 70, y: cy - 40 } as Pt,
      ips: b.ips.map((ip, j) => ({ ip: ip[0], v: ip[1], p: { x: 108 + j * 132 + jit(ip[0], 7) * 48, y: cy + jit(ip[0], 131) * 56 } as Pt })),
    }
  })
  // internal lateral sources sit below the two WAN /24s; those matched in the device
  // graph carry the cross-link that ties an external row to an internal device.
  const latGeom = (asData?.internalDenySrc ?? []).slice(0, 5).map((s, j) => ({ ip: s[0], v: s[1], p: { x: 108 + j * 116 + jit(s[0], 17) * 40, y: 600 + jit(s[0], 251) * 36 } as Pt, linked: devByIp.has(s[0]) }))
  const allIps = [...blockGeom.flatMap((b) => b.ips), ...latGeom]
  const selIp = focus?.kind === 'attacker' ? focus.ip : null
  const selNode = allIps.find((x) => x.ip === selIp)
  const linkedDev = selIp ? devByIp.get(selIp) : undefined
  const linkP: Pt = { x: 1240, y: T.y - 150 }

  return (
    <g className={`wan-siege ${wan ? 'dim' : ''}`}>
      {/* ── OVERVIEW: the tally (whole block clickable → sources) ── */}
      {!expanded && (
        <g className="ws-tally">
          <text x={TALLY.x} y={TALLY.y - 26} className="ws-kicker">{zh ? '外网入口 · 管理登录暴力破解' : 'WAN INGRESS · ADMIN BRUTE-FORCE'}</text>
          <text x={TALLY.x} y={TALLY.y - 9} className="ws-tally-cap">
            {zh ? `${distinctSrc} 个来源 · 每格 1 个 · ` : `${distinctSrc} sources · `}<tspan className="ws-drill-hint">{zh ? '点击展开攻击网段 ↴' : 'CLICK TO EXPAND NETBLOCKS ↴'}</tspan>
          </text>
          <g style={{ cursor: 'pointer' }} onClick={() => setWanFocus({ kind: 'sources' })}>
            {Array.from({ length: distinctSrc }).map((_, i) => (
              <rect key={i} x={TALLY.x + (i % TALLY.cols) * TALLY.cell} y={TALLY.y + Math.floor(i / TALLY.cols) * TALLY.cell} width={4} height={4} className="ws-src" />
            ))}
            <rect x={TALLY.x - 4} y={TALLY.y - 4} width={tallyW + 8} height={tallyH + 8} className="ws-tally-hit" />
          </g>
          {[0, 0.5, 1].map((f, i) => (
            <path key={i} d={`M ${TALLY.x + tallyW + 6} ${TALLY.y + tallyH * f} Q ${(TALLY.x + tallyW + T.x) / 2} ${TALLY.y + tallyH * f} ${T.x - 14} ${T.y}`} className="ws-thread" pointerEvents="none" />
          ))}
        </g>
      )}

      {/* ── OVERVIEW named sources → attacker profile ── */}
      {!expanded && (
        <g className="ws-nodes">
          {atk.map((a, i) => (
            <g key={i} className="ws-node" style={{ cursor: 'pointer' }} onClick={() => setWanFocus({ kind: 'attacker', ip: a.ip })}>
              <rect x={a.p.x - 3} y={a.p.y - 3} width="6" height="6" className="ws-node-dot" />
              <text x={a.p.x + 14} y={a.p.y - 1} className="ws-node-ip" textAnchor="start">{a.ip}</text>
              <text x={a.p.x + 14} y={a.p.y + 12} className="ws-node-v" textAnchor="start">{short(a.v)} {zh ? '次尝试' : 'attempts'}<tspan className="ws-node-hint"> ▸ {zh ? '画像' : 'PROFILE'}</tspan></text>
            </g>
          ))}
          {atk.map((a, i) => <line key={`v${i}`} x1={a.p.x + 6} y1={a.p.y} x2={T.x - 12} y2={T.y} className="ws-vector" pointerEvents="none" />)}
        </g>
      )}

      {/* ── SOURCES / ATTACKER: the netblock burst ── */}
      {expanded && (
        <g className="ws-blocks">
          {/* the 573 real distinct sources stay as a faint field behind the burst, so
              the density of the whole campaign is never lost when a few IPs are named */}
          <g className="ws-srcfield" pointerEvents="none">
            {Array.from({ length: distinctSrc }).map((_, i) => (
              <rect key={i} x={556 + (i % 22) * 5} y={92 + Math.floor(i / 22) * 5} width={2.6} height={2.6} className="ws-src faint" />
            ))}
            <text x={556} y={86} className="ws-field-lab">{zh ? `${distinctSrc} 个真实来源 · 6 个已具名` : `${distinctSrc} REAL SOURCES · 6 NAMED`}</text>
          </g>
          <text x={70} y={96} className="ws-back" style={{ cursor: 'pointer' }} onClick={() => setWanFocus(null)}>← {zh ? '收起 · 返回总览' : 'COLLAPSE'}</text>
          <text x={70} y={124} className="ws-kicker">{zh ? '攻击者网段 · /24 聚类 · 点 IP 看画像' : 'ATTACKER NETBLOCKS · CLICK AN IP'}</text>
          {/* two coordinated /24s hitting in identical lockstep → one botnet family */}
          {blockGeom.length >= 2 && blockGeom.every((b) => b.coordinated) && (() => {
            const c0 = blockGeom[0].ips[Math.floor(blockGeom[0].ips.length / 2)]?.p ?? blockGeom[0].ips[0].p
            const c1 = blockGeom[1].ips[Math.floor(blockGeom[1].ips.length / 2)]?.p ?? blockGeom[1].ips[0].p
            return (
              <g pointerEvents="none">
                <path d={`M${c0.x} ${c0.y} C ${c0.x - 40} ${(c0.y + c1.y) / 2}, ${c1.x - 40} ${(c0.y + c1.y) / 2}, ${c1.x} ${c1.y}`} className="ws-botnet-link" />
                <text x={c0.x - 46} y={(c0.y + c1.y) / 2} className="ws-botnet-lab">{zh ? '⟲ 同源' : '⟲ SAME'}</text>
              </g>
            )
          })()}
          {blockGeom.map((b) => (
            <g key={b.cidr}>
              <text x={b.labelP.x} y={b.labelP.y} className="ws-block-cidr">{b.cidr} · {nfmt(b.count)}</text>
              {b.coordinated && <text x={b.labelP.x} y={b.labelP.y + 15} className="ws-coord">{zh ? `⚠ 每 IP 恰好 ${nfmt(b.ips[0].v)} 次 · 协同僵尸网络` : `⚠ EXACTLY ${nfmt(b.ips[0].v)}/IP · BOTNET`}</text>}
              {/* lockstep link: the sibling IPs move as ONE — draw them interlinked */}
              {b.coordinated && b.ips.length >= 2 && (
                <path d={`M${b.ips[0].p.x} ${b.ips[0].p.y} ${b.ips.slice(1).map((ip) => `L${ip.p.x} ${ip.p.y}`).join(' ')}`} className="ws-coord-link" pointerEvents="none" />
              )}
              {b.ips.map((ip) => {
                const on = ip.ip === selIp
                return (
                  <g key={ip.ip} className={`ws-ipnode ${on ? 'on' : ''} ${selIp && !on ? 'mute' : ''}`} style={{ cursor: 'pointer' }} onClick={() => setWanFocus({ kind: 'attacker', ip: ip.ip })}>
                    <line x1={ip.p.x} y1={ip.p.y} x2={T.x - 12} y2={T.y} className={`ws-atk-line ${on ? 'hot' : ''}`} pointerEvents="none" />
                    <rect x={ip.p.x - 5} y={ip.p.y - 5} width={10} height={10} className="ws-ip-mark" transform={`rotate(45 ${ip.p.x} ${ip.p.y})`} />
                    <text x={ip.p.x} y={ip.p.y - 12} className="ws-ip-lab" textAnchor="middle">{ip.ip}</text>
                    <text x={ip.p.x} y={ip.p.y + 22} className="ws-ip-v" textAnchor="middle">{nfmt(ip.v)}{devByIp.has(ip.ip) ? (zh ? ' · 内网设备' : ' · DEV') : ''}</text>
                  </g>
                )
              })}
            </g>
          ))}
          {latGeom.length > 0 && (
            <g className="ws-latgroup">
              <text x={70} y={562} className="ws-block-cidr">{zh ? '内网横向源' : 'LATERAL SRC'} <tspan className="ws-coord">{zh ? '· 同子网聚类 · 点击看内外关联' : '· CLUSTERED · CROSS-LINK'}</tspan></text>
              {/* cluster the lateral sources by their /24 — the internal spread structure */}
              {(() => {
                const groups = new Map<string, typeof latGeom>()
                for (const s of latGeom) { const k = s.ip.split('.').slice(0, 3).join('.'); const g = groups.get(k) ?? []; g.push(s); groups.set(k, g) }
                return [...groups.values()].filter((g) => g.length >= 2).map((grp, gi) => (
                  <path key={gi} d={`M${grp[0].p.x} ${grp[0].p.y + 15} ${grp.slice(1).map((s) => `L${s.p.x} ${s.p.y + 15}`).join(' ')}`} className="ws-lat-cluster" pointerEvents="none" />
                ))
              })()}
              {latGeom.map((s) => {
                const on = s.ip === selIp
                return (
                  <g key={s.ip} className={`ws-ipnode ${on ? 'on' : ''} ${selIp && !on ? 'mute' : ''}`} style={{ cursor: 'pointer' }} onClick={() => setWanFocus({ kind: 'attacker', ip: s.ip })}>
                    <rect x={s.p.x - 5} y={s.p.y - 5} width={10} height={10} className="ws-ip-mark lat" />
                    <text x={s.p.x} y={s.p.y - 12} className="ws-ip-lab" textAnchor="middle">{s.ip}</text>
                    <text x={s.p.x} y={s.p.y + 22} className="ws-ip-v" textAnchor="middle">{nfmt(s.v)}{s.linked ? (zh ? ' · 设备✓' : ' · DEV✓') : ''}</text>
                  </g>
                )
              })}
            </g>
          )}
          {/* attacker profile card + cross-link */}
          {selNode && (
            <>
              {linkedDev && (
                <g pointerEvents="none">
                  <path d={bez({ x: selNode.p.x, y: selNode.p.y }, linkP)} className="ws-crosslink" />
                  <line x1={linkP.x} y1={linkP.y} x2={linkP.x} y2={linkP.y} className="ws-crosslink" />
                </g>
              )}
              <foreignObject x={clamp(selNode.p.x - 90, 40, VBW - 240)} y={selNode.p.y + 34} width={230} height={linkedDev ? 150 : 108} className="ws-card-fo">
                <div className={`ws-card ${zh ? '' : 'en'}`}>
                  <div className="ws-card-h"><b>{selNode.ip}</b><span>{selNode.v.toLocaleString()} {zh ? '次尝试' : 'hits'}</span></div>
                  <div className="ws-card-row">{zh ? '网段/子网' : 'net'} {blockGeom.find((b) => b.ips.some((x) => x.ip === selNode.ip))?.cidr ?? linkedDev?.cidr ?? '—'}</div>
                  <div className="ws-card-row">{zh ? '目标 · FortiGate 管理登录 · 已锁定 ' : 'target · admin login · '}{lockouts}{zh ? ' 次' : ' locks'}</div>
                  {linkedDev && <div className="ws-card-corr">⚠ {zh ? `内外同一实体:${linkedDev.cidr} 的${roleZh[linkedDev.role ?? ''] ?? linkedDev.role}·威胁${(linkedDev.threat ?? 'ok').toUpperCase()}·被拒${nfmt(linkedDev.deny ?? 0)}` : `same entity: ${linkedDev.role} on ${linkedDev.cidr} · ${linkedDev.threat}`}</div>}
                  <button className="ws-card-btn" onClick={() => onWan(selNode.ip)}>{zh ? 'DeepSeek 深度研判 ▸' : 'DEEPSEEK ANALYZE ▸'}</button>
                </div>
              </foreignObject>
            </>
          )}
        </g>
      )}

      <line x1={T.x} y1={T.y} x2={gwFace.x} y2={gwFace.y} className="ws-breach" pointerEvents="none" />

      {/* ── TARGET: admin login, clickable → its evidence unfolds ── */}
      <g className={`ws-target ${focus?.kind === 'target' ? 'on' : ''}`} style={{ cursor: 'pointer' }} onClick={() => setWanFocus(focus?.kind === 'target' ? null : { kind: 'target' })}>
        {brackets(T, 13, 6).map((d, i) => <path key={i} d={d} className="ws-lock-bracket" />)}
        <circle cx={T.x} cy={T.y} r={3} className="ws-lock-dot" />
        <text x={T.x} y={T.y - 24} className="ws-lock-label" textAnchor="middle">{zh ? '管理登录 · 目标' : 'ADMIN LOGIN · TARGET'}</text>
        <text x={T.x} y={T.y + 30} className="ws-contain" textAnchor="middle">{zh ? `已遏制 · ${lockouts} 次锁定 · 点击展开` : `CONTAINED · ${lockouts} LOCKOUTS · CLICK`}</text>
      </g>

      {/* target evidence unfold */}
      {focus?.kind === 'target' && asData && (
        <foreignObject x={T.x - 130} y={T.y + 48} width={280} height={210} className="ws-card-fo">
          <div className="ws-tcard">
            <div className="ws-tcard-h">{zh ? '被攻击详情 · 管理登录' : 'TARGET EVIDENCE · ADMIN LOGIN'}</div>
            <div className="ws-tcard-funnel"><b>{nfmt(asData.adminLoginFailed)}</b> {zh ? '失败' : 'failed'} → <b>{nfmt(asData.distinctSrc)}</b> {zh ? '源' : 'src'} → <b>{nfmt(asData.lockouts)}</b> {zh ? '锁定' : 'locks'}</div>
            <div className="ws-tcard-lab">{zh ? '被拒端口 TOP' : 'TOP DENY PORTS'}</div>
            {asData.denyPorts.slice(0, 4).map((p) => (
              <div key={p[0]} className="ws-tcard-bar"><span>:{p[0]} {PORT_LAB[p[0]] ?? ''}</span><i style={{ width: `${(p[1] / Math.max(1, asData.denyPorts[0][1])) * 100}%` }} /><em>{nfmt(p[1])}</em></div>
            ))}
            <div className="ws-tcard-lab">{zh ? `大华设备端口探测 · 被拒 ${nfmt(asData.devicePortDeny)}` : `DAHUA PROBES · ${nfmt(asData.devicePortDeny)}`}</div>
            <div className="ws-tcard-dahua">{asData.devicePortTop.slice(0, 4).map((p) => <span key={p[0]}>:{p[0]}·{nfmt(p[1])}</span>)}</div>
          </div>
        </foreignObject>
      )}
    </g>
  )
}

/** The FortiGate core, framed as the focal node: angular corner brackets, ticks,
 *  and the ONE acid focal accent.
 *
 *  The rotating crimson "lock ring" is gone. It was permanent decorative motion
 *  (an 18s infinite spin that encoded nothing) in a second accent family, on a
 *  page whose design law allows neither. The brackets already say "this is the
 *  focal node" structurally — which is what the law asks for. */
function CoreReticle({ core }: { core: Pt }) {
  const bx = 82
  const by = 54
  const L = 17
  const corners = [
    `M ${core.x - bx} ${core.y - by + L} L ${core.x - bx} ${core.y - by} L ${core.x - bx + L} ${core.y - by}`,
    `M ${core.x + bx - L} ${core.y - by} L ${core.x + bx} ${core.y - by} L ${core.x + bx} ${core.y - by + L}`,
    `M ${core.x + bx} ${core.y + by - L} L ${core.x + bx} ${core.y + by} L ${core.x + bx - L} ${core.y + by}`,
    `M ${core.x - bx + L} ${core.y + by} L ${core.x - bx} ${core.y + by} L ${core.x - bx} ${core.y + by - L}`,
  ]
  return (
    <g className="core-reticle" pointerEvents="none">
      {corners.map((d, i) => (
        <path key={i} d={d} className="core-bracket" />
      ))}
      <line x1={core.x} y1={core.y - by} x2={core.x} y2={core.y - by + 9} className="core-tick" />
      <line x1={core.x} y1={core.y + by} x2={core.x} y2={core.y + by - 9} className="core-tick" />
      <line x1={core.x - bx} y1={core.y} x2={core.x - bx + 9} y2={core.y} className="core-tick" />
      <line x1={core.x + bx} y1={core.y} x2={core.x + bx - 9} y2={core.y} className="core-tick" />
      <rect x={core.x - 32} y={core.y - by - 5} width={64} height={5} className="core-acid" />
    </g>
  )
}

/** Corner situational read-outs — plain-language big picture: data window,
 *  attack-source / lockout counts, device / link / subnet counts.
 *
 *  The window label used to read "近 48 小时 / LAST 48H", hardcoded. The payload's
 *  real window (`dataStats.windowDays`) is a fixed 2-day HELD-OUT capture, not a
 *  trailing 48h from now — on 2026-07-17 it still reports 2026-06-16..17, a month
 *  stale. "LAST 48H" was simply false, and it was the page's loudest recency
 *  claim. It now prints the dates the payload actually carries. */
function HudReadouts({ stats, meshCount, ifCount, subCount, lang }: { stats: DataStats; meshCount: number; ifCount: number; subCount: number; lang: Lang }) {
  const zh = lang === 'zh'
  const days = stats.windowDays ?? []
  const window = days.length ? (days.length > 1 ? `${days[0]} → ${days[days.length - 1]}` : days[0]) : (zh ? '窗口未知' : 'WINDOW UNKNOWN')
  return (
    <g className="hud-readouts" pointerEvents="none">
      <g textAnchor="end">
        <text x={VBW - 44} y={30} className="hud-r-dim">{zh ? '保留集窗口' : 'HELD-OUT WINDOW'} · {window}</text>
        <text x={VBW - 44} y={50} className="hud-r-line"><tspan className="hot">{short(stats.distinctSrc)}</tspan> {zh ? '攻击来源' : 'sources'} · <tspan className="hot">{stats.lockouts ?? 0}</tspan> {zh ? '次锁定' : 'lockouts'}</text>
        <text x={VBW - 44} y={67} className="hud-r-line"><tspan className="acc">{meshCount}</tspan> {zh ? '设备' : 'devices'} · {ifCount} {zh ? '接口' : 'links'} · {subCount} {zh ? '网段' : 'subnets'}</text>
      </g>
    </g>
  )
}

/** THE THESIS — what this page is, in the words the payload can support.
 *
 *  Page 1 never said what it was; it read as a generic network map. Pages 2 and 3
 *  share a spine (trace → evidence → verdict), and page 1 is the EVIDENCE surface:
 *  the real network the agent reasons over. Every value here is lifted straight
 *  from the payload — `dataStats.source`, `dataStats.windowDays`, `note`'s reasoner
 *  — and the read-path sentence describes triggers this component actually wires.
 *
 *  It occupies the resting state only: open a source or a segment and this space
 *  becomes the analysis surface, which is why the console has a bottom band at all.
 *
 *  NOTE: the reasoner that produced the case diagnosis (`snapshot.reasonerMode`)
 *  would belong on this block, but it is not passed to this component and App.tsx
 *  is owned elsewhere. It is omitted rather than guessed. */
function Thesis({ stats, lang }: { stats: DataStats; lang: Lang }) {
  const zh = lang === 'zh'
  const days = stats.windowDays ?? []
  const window = days.length > 1 ? `${days[0]} → ${days[days.length - 1]}` : days[0] ?? '—'
  return (
    <g className="thesis" pointerEvents="none">
      <line x1={96} y1={772} x2={780} y2={772} className="th-rule" />
      <text x={96} y={808} className="th-kicker">{zh ? '态势 · 证据面' : 'CONSOLE · THE EVIDENCE SURFACE'}</text>
      <text x={96} y={852} className="th-meta">
        <tspan className="th-k">{zh ? '来源 ' : 'SOURCE '}</tspan>{stats.source}
        <tspan className="th-k">{zh ? '  窗口 ' : '   WINDOW '}</tspan>{window}
      </text>
      <text x={96} y={888} className="th-read">
        {zh
          ? '点来源 → 入侵研判 · 点网段 → 展开设备关系图'
          : 'click a source → intrusion verdict · click a segment → its device graph'}
      </text>
    </g>
  )
}

const THREAT_DX: Record<string, number> = { high: 0, watch: 30, ok: 56 }
const ROLE_ZH: Record<string, string> = {
  camera: '摄像头', intercom: '门禁对讲', mobile: '移动端', workstation: '工作站', server: '服务器', unknown: '未识别',
}
const KILLCHAIN: { k: string; zh: string; en: string }[] = [
  { k: 'recon', zh: '踩点', en: 'recon' },
  { k: 'credential-access', zh: '盗密码', en: 'steal creds' },
  { k: 'lateral-movement', zh: '内网扩散', en: 'spread' },
  { k: 'impact', zh: '破坏', en: 'impact' },
]

const SEV_ZH: Record<string, string> = { critical: '严重', high: '高危', medium: '中危', low: '低危' }
const sevLabel = (sev: string | undefined, lang: Lang) => (lang === 'zh' ? SEV_ZH[sev ?? ''] ?? sev ?? '' : sev ?? '')

export function TopologyCanvas({
  topo,
  stats,
  activeKey,
  drillSub,
  drillDev,
  tempo,
  marks,
  threat,
  lang,
  meshCount,
  meshLoading,
  hover3D,
  hover3DCidr,
  topoAlert,
  wan,
  graph,
  graphAnalysis,
  hoverDev,
  onHoverDev,
  onGraphAnalyze,
  onCloseGraphAnalysis,
  onWan,
  onCloseWan,
  onHoverSubnet,
  onOpen3D,
  onCloseThreat,
  onSub,
  onDev,
  onBatch,
  onPentest,
  theater,
  allGraphs,
  onCloseTheater,
  onOpenTheater,
}: {
  topo: Topology
  stats: DataStats
  activeKey: string
  drillSub: string | null
  drillDev: string | null
  tempo: number
  marks: Record<string, { severity: string; verdict: string }>
  threat: Threat | null
  lang: Lang
  meshCount: number
  meshLoading: boolean
  hover3D: string | null
  hover3DCidr: string | null
  topoAlert: { cidr: string; ip: string; verdict: string; severity: string } | null
  wan: WanThreat | null
  graph: SubnetGraph | null
  graphAnalysis: GraphAnalysis | null
  hoverDev: string | null
  onHoverDev: (ip: string | null) => void
  onGraphAnalyze: (cidr: string) => void
  onCloseGraphAnalysis: () => void
  onWan: (ip: string) => void
  onCloseWan: () => void
  onHoverSubnet?: (cidr: string | null) => void
  onOpen3D: () => void
  onCloseThreat: () => void
  onSub: (s: Subnet | null) => void
  onDev: (d: Device | null, cidr: string) => void
  onBatch: (cidr: string) => void
  onPentest?: () => void
  /* ── event-driven full-chain topology theater ── */
  theater?: TheaterEvent | null
  allGraphs?: Record<string, SubnetGraph>
  onCloseTheater?: () => void
  onOpenTheater?: () => void
}) {
  const g = group(activeKey)
  /* Flow reads left→right across the full plate: WAN tally → admin target →
   * FortiGate → interfaces → segments. The old core sat at (452,340) of a 1360
   * box, which pinned every mark into the upper-left quadrant. */
  const core: Pt = { x: 700, y: 430 }
  // WAN attack-surface deep data + the in-place offense drill focus. The topology
  // itself reshapes across focus levels (sources → attacker → target); this is the
  // interactive offense layer the left WAN field expands into, not a side panel.
  const [asData, setAsData] = useState<AsData | null>(null)
  const [wanFocus, setWanFocus] = useState<WanFocus>(null)
  useEffect(() => {
    let gone = false
    fetch('/api/rca/attack_surface').then((r) => (r.ok ? r.json() : null)).then((d: AsData | null) => { if (!gone && d) setAsData(d) }).catch(() => {})
    return () => { gone = true }
  }, [])
  const devByIp = useMemo(() => {
    const m = new Map<string, AsDevice & { cidr: string }>()
    for (const s of asData?.assetExposure.subnets ?? []) for (const e of s.exposed) m.set(e.ip, { ...e, cidr: s.cidr })
    return m
  }, [asData])
  const ref = useRef<SVGSVGElement | null>(null)
  // ── viewport: wheel-zoom about the cursor, drag-to-pan anywhere on the plate ──
  const [view, setView] = useState({ k: 1, x: 0, y: 0 })
  const drag = useRef<{ px: number; py: number; x: number; y: number; moved: boolean } | null>(null)
  const [panning, setPanning] = useState(false)

  /** client px → root user space (accounts for the meet-fit letterboxing) */
  const toLocal = useCallback((e: React.MouseEvent | React.WheelEvent): Pt => {
    const svg = ref.current
    if (!svg) return { x: 0, y: 0 }
    const box = svg.getBoundingClientRect()
    const s = Math.min(box.width / VBW, box.height / VBH)
    return {
      x: (e.clientX - box.left - (box.width - VBW * s) / 2) / s,
      y: (e.clientY - box.top - (box.height - VBH * s) / 2) / s,
    }
  }, [])

  const onDown = (e: React.MouseEvent) => {
    drag.current = { px: e.clientX, py: e.clientY, x: view.x, y: view.y, moved: false }
  }
  const onMove = (e: React.MouseEvent) => {
    const d = drag.current
    const svg = ref.current
    if (!d || !svg) return
    const box = svg.getBoundingClientRect()
    const s = Math.min(box.width / VBW, box.height / VBH)
    const dx = (e.clientX - d.px) / s
    const dy = (e.clientY - d.py) / s
    if (!d.moved && Math.hypot(dx, dy) < 3) return // let clicks through
    d.moved = true
    if (!panning) setPanning(true)
    setView((v) => ({ ...v, x: d.x + dx, y: d.y + dy }))
  }
  const endDrag = () => {
    drag.current = null
    setPanning(false)
  }
  const onWheel = (e: React.WheelEvent) => {
    const p = toLocal(e)
    setView((v) => {
      const k = Math.max(0.45, Math.min(6, v.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)))
      // keep the point under the cursor pinned while the scale changes
      return { k, x: p.x - ((p.x - v.x) / v.k) * k, y: p.y - ((p.y - v.y) / v.k) * k }
    })
  }
  const resetView = () => setView({ k: 1, x: 0, y: 0 })

  // ── ego-focus: click any host to promote its relation sub-graph to the whole
  //    field. We reframe the viewport onto {node ∪ neighbours} and the graph layer
  //    fades every unrelated host, so a dense community becomes one legible ego net. */
  const [focusDev, setFocusDev] = useState<string | null>(null)
  // opening/closing a segment resets both the ego-focus and the viewport. This is
  // the "adjust state when a prop changes" pattern (compare during render), not an
  // effect — so it stays in sync without a post-render flash.
  const [prevDrill, setPrevDrill] = useState(drillSub)
  if (prevDrill !== drillSub) {
    setPrevDrill(drillSub)
    if (focusDev !== null) setFocusDev(null)
    if (view.k !== 1 || view.x !== 0 || view.y !== 0) setView({ k: 1, x: 0, y: 0 })
  }
  // entering/leaving the theater resets the viewport the same way
  const [prevTheater, setPrevTheater] = useState(theater?.id ?? null)
  if (prevTheater !== (theater?.id ?? null)) {
    setPrevTheater(theater?.id ?? null)
    if (view.k !== 1 || view.x !== 0 || view.y !== 0) setView({ k: 1, x: 0, y: 0 })
  }
  const focusOn = (ip: string | null, pos: Record<string, Pt>) => {
    setFocusDev(ip)
    if (!ip || !graph) {
      resetView()
      return
    }
    const ego = new Set<string>([ip])
    for (const e of graph.edges) {
      if (e.src === ip) ego.add(e.dst)
      if (e.dst === ip) ego.add(e.src)
    }
    const pts = [...ego].map((x) => pos[x]).filter(Boolean)
    if (!pts.length) return
    const xs = pts.map((p) => p.x)
    const ys = pts.map((p) => p.y)
    const pad = 150
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2
    const cy = (Math.min(...ys) + Math.max(...ys)) / 2
    const bw = Math.max(Math.max(...xs) - Math.min(...xs) + pad * 2, 260)
    const bh = Math.max(Math.max(...ys) - Math.min(...ys) + pad * 2, 260)
    const k = Math.max(0.6, Math.min(3.4, Math.min(VBW / bw, VBH / bh)))
    setView({ k, x: VBW / 2 - cx * k, y: VBH / 2 - cy * k })
  }

  const zoomBy = (f: number) =>
    setView((v) => {
      const k = Math.max(0.45, Math.min(6, v.k * f))
      const cx = VBW / 2
      const cy = VBH / 2
      return { k, x: cx - ((cx - v.x) / v.k) * k, y: cy - ((cy - v.y) / v.k) * k }
    })

  const layout = useMemo(() => {
    // named sources sit under their own tally, still in the WAN column
    const atk = stats.topAttackerSrc.slice(0, 3).map((d, i) => ({ ip: d[0], v: d[1], p: { x: 102, y: 468 + i * 46 } as Pt }))
    const lan = topo.interfaces.filter((it) => it.kind === 'lan')
    const ys = [150, 330, 512, 692]
    const ifs = lan.map((it, i) => {
      const p: Pt = { x: 1090, y: ys[i] ?? 150 + i * 182 }
      const sub = topo.subnets.find((s) => s.intf === it.name && s.hosts > 1)
      return { it, p, sub, subP: { x: 1350, y: p.y } as Pt }
    })
    return { atk, ifs }
  }, [topo, stats])

  const openSub = layout.ifs.find((f) => f.sub && drillSub === f.sub.cidr)?.sub ?? null
  const drilled = !!graph && drillSub === graph.cidr
  // Theater mode owns the whole plate: every ordinary console layer yields,
  // exactly like a drilled segment does.
  const inTheater = !!theater
  // Expanding a segment hands it the ENTIRE plate: the gateway chain collapses to
  // a breadcrumb and the ~120 hosts spread across a wide ellipse that fills the
  // whole field (kept clear of the bottom-left agent panel).
  const meshCenter: Pt = { x: 1016, y: 424 }
  const meshRX = 830
  const meshRY = 368
  const devPos: Record<string, Pt> = {}
  if (drilled && graph) {
    for (const dv of graph.devices) {
      devPos[dv.ip] = { x: meshCenter.x + dv.x * meshRX, y: meshCenter.y + dv.y * meshRY }
    }
  } else if (openSub) {
    ;(openSub.devices ?? []).slice(0, 7).forEach((dv, j) => {
      devPos[dv.ip] = { x: 1560 + (THREAT_DX[dv.threat] ?? 40), y: 90 + j * 80 }
    })
  }
  const anchor = threat ? devPos[threat.ip] : undefined

  return (
    <svg
      ref={ref}
      viewBox={`0 0 ${VBW} ${VBH}`}
      className="flow-canvas"
      preserveAspectRatio="xMidYMid meet"
      onMouseDown={onDown}
      onMouseMove={onMove}
      onMouseUp={endDrag}
      onMouseLeave={endDrag}
      onWheel={onWheel}
      onContextMenu={(e) => e.preventDefault()}
      style={{ cursor: panning ? 'grabbing' : 'grab' }}
    >
      <g
        transform={`translate(${view.x} ${view.y}) scale(${view.k})`}
        style={{ transition: panning ? 'none' : 'transform 0.5s cubic-bezier(0.4, 0, 0.2, 1)' }}
      >
        {/* Drilling into a segment hands the whole field to that LAN: the WAN
            ingress and the sibling interfaces fall away so the eye analyses one
            subnet's device relations, not the whole console. */}
        {inTheater && theater ? (
          <TheaterStage
            topo={topo}
            stats={stats}
            graphs={allGraphs ?? {}}
            theater={theater}
            lang={lang}
            onClose={() => onCloseTheater?.()}
            onNode={(ip) => onWan(ip)}
            busyIp={wan?.loading ? wan.ip : null}
            impactNodes={wan && !wan.loading && !wan.error ? wan.impactNodes ?? [] : []}
            analysisIp={wan && !wan.loading && !wan.error ? wan.ip : null}
          />
        ) : null}
        {!drilled && !inTheater ? (
          <WanSiege core={core} atk={layout.atk} asData={asData} wanFocus={wanFocus} setWanFocus={setWanFocus} devByIp={devByIp} distinctSrc={stats.distinctSrc} lockouts={stats.lockouts ?? 0} lang={lang} wan={wan} onWan={onWan} />
        ) : null}
        {!drilled && !inTheater
          ? layout.ifs.map((f, i) => {
              const focused = drillSub === f.sub?.cidr
              const fade = g === 'attack' || (drillSub && !focused) || !!wan
              return (
                <g key={`if${i}`}>
                  <Edge a={core} b={f.p} tone="t-flow" flows={f.it.flows} dim={!!fade} hot={focused} delay={0.4 + i * 0.06} tempo={tempo} />
                  {f.sub ? <Edge a={f.p} b={f.subP} tone="t-flow" flows={f.sub.flows} dim={!!fade} hot={focused} delay={0.6 + i * 0.06} tempo={tempo} /> : null}
                </g>
              )
            })
          : null}

        {!drilled && !inTheater ? (
          <>
            <g className="node gw-node appear" style={{ animationDelay: '0.3s' }}>
              <rect x={core.x - 60} y={core.y - 32} width="120" height="64" rx="1" />
              <text x={core.x} y={core.y - 5} className="n-title">{topo.core.name}</text>
              <text x={core.x} y={core.y + 14} className="n-sub">{topo.core.ip}</text>
            </g>
            <CoreReticle core={core} />
          </>
        ) : null}

        {/* drilled breadcrumb — the collapsed gateway chain, one click back to全网
            (hidden during ego focus, which shows its own "返回网段" control) */}
        {drilled && graph && !focusDev ? (
          <g className="mesh-crumb" onClick={() => onSub(null)} style={{ cursor: 'pointer' }}>
            {/* SVG <text> only takes pointer events on the glyphs themselves, so
                this control's own bounding-box centre (the gap between its two
                lines) fell through to the canvas — aiming at the breadcrumb and
                missing it was the default outcome, for a person and for a driver.
                An explicit transparent hit target gives the trigger a real box. */}
            <rect x={24} y={182} width={230} height={48} fill="transparent" />
            <text x={30} y={200} className="mesh-crumb-t">
              ◂ {topo.core.name} · {layout.ifs.find((f) => f.sub?.cidr === graph.cidr)?.it.name ?? 'LAN'}
            </text>
            <text x={30} y={220} className="mesh-crumb-b">{lang === 'zh' ? '返回全网态势' : 'BACK TO CONSOLE'}</text>
          </g>
        ) : null}

        {/* The thesis owns the resting state's bottom band; an open analysis takes
            the same space, so they are mutually exclusive by construction. */}
        {!drilled && !inTheater && !threat && !wan ? <Thesis stats={stats} lang={lang} /> : null}

        {layout.ifs.map((f, i) => {
          const focused = drillSub === f.sub?.cidr
          if (drilled || inTheater) return null
          const dimIf = g === 'attack' || (drillSub && !focused) || !!wan
          const highThreat = f.sub?.devices?.some((dv) => dv.threat === 'high')
          return (
            <g key={`ifn${i}`} className={`node appear ${dimIf ? 'node-dim' : ''}`} style={{ animationDelay: `${0.5 + i * 0.06}s` }}>
              <rect x={f.p.x - 52} y={f.p.y - 16} width="104" height="32" className="m-intf" />
              <text x={f.p.x} y={f.p.y - 1} className="n-intf">{f.it.name}</text>
              <text x={f.p.x} y={f.p.y + 12} className="n-sub">{short(f.it.flows)} {lang === 'zh' ? '连接' : 'conns'}</text>
              {f.sub ? (
                <g
                  className={`subnet ${focused ? 'open' : ''} ${hover3DCidr === f.sub.cidr ? 'mapped' : ''}`}
                  onClick={() => onSub(focused ? null : f.sub!)}
                  onMouseEnter={() => onHoverSubnet?.(f.sub!.cidr)}
                  onMouseLeave={() => onHoverSubnet?.(null)}
                  style={{ cursor: 'pointer' }}
                >
                  {hover3DCidr === f.sub.cidr ? <circle cx={f.subP.x} cy={f.subP.y} r="18" className="map-ring" /> : null}
                  <rect x={f.subP.x - 9} y={f.subP.y - 9} width="18" height="18" className="m-host" />
                  {highThreat ? <circle cx={f.subP.x + 9} cy={f.subP.y - 9} r="3.5" className="threat-pip" /> : null}
                  <text x={f.subP.x + 18} y={f.subP.y - 1} className="n-ip" textAnchor="start">{f.sub.cidr}</text>
                  <text x={f.subP.x + 18} y={f.subP.y + 12} className="n-v amber" textAnchor="start">{f.sub.hosts} {lang === 'zh' ? '台设备' : 'devices'} {focused ? '▾' : '▸'}</text>
                  {hover3DCidr === f.sub.cidr && hover3D ? (
                    <text x={f.subP.x + 18} y={f.subP.y + 26} className="map-ip" textAnchor="start">◂ {hover3D}</text>
                  ) : null}
                  {topoAlert && topoAlert.cidr === f.sub.cidr ? (
                    <g>
                      <circle cx={f.subP.x} cy={f.subP.y} r="21" className="alert-ring" />
                      <text x={f.subP.x + 18} y={f.subP.y + (hover3DCidr === f.sub.cidr ? 40 : 26)} className="alert-verdict" textAnchor="start">
                        ⚠ {topoAlert.ip} · {topoAlert.verdict || (lang === 'zh' ? '分析中…' : 'analyzing…')}
                      </text>
                    </g>
                  ) : null}
                </g>
              ) : null}
            </g>
          )
        })}

        {/* open subnet → the FULL segment: every host, every mined relation */}
        {drilled && graph ? (
                <g key={`mesh-${graph.cidr}`}>
                  <SubnetGraphLayer
                    graph={graph}
                    analysis={graphAnalysis}
                    center={meshCenter}
                    rx={meshRX}
                    ry={meshRY}
                    vbw={VBW}
                    vbh={VBH}
                    lang={lang}
                    hoverIp={hoverDev}
                    selectedIp={drillDev}
                    focusIp={focusDev}
                    marks={marks}
                    showPanel={!threat}
                    onHover={onHoverDev}
                    onFocus={(ip) => focusOn(ip, devPos)}
                    onAnalyze={() => onGraphAnalyze(graph.cidr)}
                    onCloseAnalysis={onCloseGraphAnalysis}
                  />
                </g>
          ) : null}

        {/* fallback: subnet opened but no mined graph (or still loading) */}
        {layout.ifs.map((f) => {
          if (!f.sub || drillSub !== f.sub.cidr || drilled) return null
          const devs = (f.sub.devices ?? []).slice(0, 7)
          const subNode: Pt = { x: f.subP.x, y: f.subP.y }
          const hi = devs.filter((d) => d.threat === 'high').length
          return (
            <g key={`tree-${f.sub.cidr}`}>
              <Float x={subNode.x - 96} y={subNode.y - 84} w={232} tone={hi ? 'alert' : 'flow'}
                lines={[
                  { k: lang === 'zh' ? '网段' : 'NET', v: f.sub.cidr },
                  { k: lang === 'zh' ? '设备' : 'DEVICES', v: `${f.sub.hosts} · ${short(f.sub.flows)} ${lang === 'zh' ? '连接' : 'conns'}` },
                  { k: lang === 'zh' ? '可疑' : 'FLAGGED', v: lang === 'zh' ? `${hi} 高危 · ${short(f.sub.accept)} 放行` : `${hi} high · ${short(f.sub.accept)} allowed` },
                ]} />
              <g className="batch-trig" onClick={() => onBatch(f.sub!.cidr)} style={{ cursor: 'pointer' }}>
                <rect x={subNode.x - 8} y={subNode.y + 24} width="132" height="22" />
                <text x={subNode.x + 58} y={subNode.y + 39}>⚡ {lang === 'zh' ? '全部分析' : 'ANALYZE ALL'}</text>
              </g>
              {devs.map((dv, j) => {
                const dy = 90 + j * 80
                const dp: Pt = { x: 1560 + (THREAT_DX[dv.threat] ?? 40), y: dy }
                const open = drillDev === dv.ip
                const mark = marks[dv.ip]
                const alert = !!mark && (mark.severity === 'high' || mark.severity === 'medium')
                const tone = alert ? 'alert' : dv.threat
                const showPorts = open || alert
                const rad = dv.threat === 'high' ? 8 : dv.threat === 'watch' ? 6 : 5
                return (
                  <g key={dv.ip} className="branch-in">
                    <path d={bez(subNode, dp)} className={`branch ${tone}`} />
                    <g className="dev-node" onClick={() => onDev(open ? null : dv, f.sub!.cidr)} style={{ cursor: 'pointer' }}>
                      <circle cx={dp.x} cy={dp.y} r={rad} className={`m-dev ${tone} ${open ? 'sel' : ''}`} />
                      <text x={dp.x + 14} y={dp.y - 1} className="n-ip" textAnchor="start">{dv.ip}</text>
                      {mark ? (
                        <text x={dp.x + 14} y={dp.y + 12} className={`n-verdict ${alert ? 'alert' : ''}`} textAnchor="start">{mark.verdict}</text>
                      ) : (
                        <text x={dp.x + 14} y={dp.y + 12} className={`n-v ${dv.threat === 'high' ? '' : 'amber'}`} textAnchor="start">{short(dv.deny)} {lang === 'zh' ? '次拦截' : 'blocked'}</text>
                      )}
                    </g>
                    {open ? (
                      <Float x={dp.x - 6} y={dp.y - 64} w={206} tone={alert ? 'alert' : dv.threat}
                        lines={[
                          { k: lang === 'zh' ? '拦截' : 'BLOCKED', v: lang === 'zh' ? `${short(dv.deny)} · ${dv.accept} 放行` : `${short(dv.deny)} · ${dv.accept} allowed` },
                          { k: lang === 'zh' ? '端口' : 'PORTS', v: dv.top_ports.map((p) => `:${p}`).join(' ') },
                        ]} />
                    ) : null}
                    {showPorts
                      ? dv.top_ports.slice(0, 3).map((pt, k) => {
                          const lp: Pt = { x: dp.x + 172, y: clamp(dp.y + (k - (dv.top_ports.length - 1) / 2) * 22, 16, VBH - 16) }
                          return (
                            <g key={pt} className="branch-in leaf">
                              <path d={bez(dp, lp)} className={`branch ${tone} ${alert ? 'flow-alert' : ''}`} />
                              <rect x={lp.x - 4} y={lp.y - 4} width="8" height="8" className={`m-leaf ${tone}`} />
                              <text x={lp.x + 12} y={lp.y + 3} className="n-leaf" textAnchor="start">:{pt}</text>
                            </g>
                          )
                        })
                      : null}
                  </g>
                )
              })}
            </g>
          )
        })}

        {/* 3D constellation portal — hidden while the WAN pivots or a segment mesh own the right field */}
        {meshCount > 0 && !wan && !drilled && !inTheater ? (
          <>
            {/* The connector paths used to live INSIDE the clickable group, which
                stretched its bounding box from the segment nodes all the way to
                the portal — so the trigger's own centre landed in empty space
                between two beziers and the click fell through to the canvas. The
                links are context, not the control: they render as a sibling that
                takes no pointer events, and the trigger keeps a real hit target. */}
            <g className="portal-links" pointerEvents="none">
              {layout.ifs.map((f, i) => (f.sub ? <path key={i} d={bez(f.subP, { x: 1724, y: 430 })} className="portal-link" /> : null))}
            </g>
            <g className="portal3d" onClick={onOpen3D} style={{ cursor: 'pointer' }}>
              <rect x={1660} y={396} width={128} height={106} fill="transparent" />
              <circle cx={1724} cy={430} r="30" className="portal-halo" />
              <circle cx={1724} cy={430} r="20" className="portal-ring" />
              <text x={1724} y={435} className="portal-glyph" textAnchor="middle">⬡</text>
              <text x={1724} y={476} className="portal-label" textAnchor="middle">{meshLoading ? (lang === 'zh' ? '载入中…' : 'loading…') : lang === 'zh' ? '3D 全网视图' : '3D NETWORK VIEW'}</text>
              <text x={1724} y={491} className="portal-sub" textAnchor="middle">{meshCount} {lang === 'zh' ? '设备' : 'devices'}</text>
            </g>
          </>
        ) : null}

        {/* in-canvas analysis layer: leader line + panel + impact subgraph */}
        {threat && anchor ? (
          (() => {
            const panelTop: Pt = { x: 360, y: 706 }
            const cx = 1240
            const cy = 824
            const peers = threat.impactPeers ?? []
            return (
              <g className="analysis-layer">
                <path d={bez(anchor, panelTop)} className="leader" />
                <path d={bez(anchor, { x: cx, y: cy - 30 })} className="leader" />
                {threat.loading ? (
                  <foreignObject x={40} y={690} width={560} height={120}>
                    <div className="an-panel">
                      <div className="an-head"><span className="an-kicker">{lang === 'zh' ? 'AI 分析' : 'AI'} · {threat.ip}</span></div>
                      <Analyzing lang={lang} />
                    </div>
                  </foreignObject>
                ) : threat.error ? (
                  <foreignObject x={40} y={690} width={560} height={90}>
                    <div className="an-panel"><div className="an-body err">{threat.error}</div></div>
                  </foreignObject>
                ) : (
                  <>
                    <foreignObject x={40} y={680} width={600} height={300}>
                      <div className={`an-panel sev-${threat.severity}`}>
                        <div className="an-head">
                          <span className="an-kicker">{lang === 'zh' ? 'AI 分析' : 'AI'} · {threat.ip}</span>
                          <button className="an-x" onClick={onCloseThreat}>✕</button>
                        </div>
                        <div className="an-verdict">
                          <span className={`sev-dot ${threat.severity}`} />
                          <Scramble className="an-vtxt" text={threat.verdict ?? ''} />
                          <span className={`sev-tag ${threat.severity ?? ''}`}>{sevLabel(threat.severity, lang)}</span>
                        </div>
                        <p className="an-analysis">{threat.analysis}</p>
                        <div className="an-pred">
                          <div><span className="pl">{lang === 'zh' ? '最可能' : 'likely'}</span>{threat.mostLikely}</div>
                          <div><span className="pl bad">{lang === 'zh' ? '最坏' : 'worst'}</span>{threat.worstCase}</div>
                          <div><span className="pl ok">{lang === 'zh' ? '恢复' : 'recovery'}</span>{threat.recovery?.action} <b>· {threat.recovery?.eta}</b></div>
                        </div>
                      </div>
                    </foreignObject>

                    <text x={cx} y={cy - 96} className="impact-tag" textAnchor="middle">
                      {lang === 'zh' ? '影响范围' : 'IMPACT MAP'}
                    </text>
                    <circle cx={cx} cy={cy} r="11" className="m-dev alert sel" />
                    <text x={cx} y={cy + 26} className="n-ip" textAnchor="middle">{threat.ip}</text>
                    {peers.map((p, i) => {
                      const py = cy + (i - (peers.length - 1) / 2) * 70
                      const px = cx + 200
                      const real = devPos[p.ip]
                      return (
                        <g key={p.ip} className="branch-in">
                          <path d={bez({ x: cx, y: cy }, { x: px, y: py })} className="branch alert flow-alert" />
                          {real ? <path d={bez({ x: px, y: py }, real)} className="link-up" /> : null}
                          <circle cx={px} cy={py} r="7" className="m-dev high" />
                          <text x={px + 14} y={py - 1} className="n-ip" textAnchor="start">{p.ip}</text>
                          <text x={px + 14} y={py + 12} className="n-verdict alert" textAnchor="start">{p.relation}</text>
                        </g>
                      )
                    })}
                  </>
                )}
              </g>
            )
          })()
        ) : null}

        {/* WAN intrusion deep-analysis: campaign lockstep → admin target → cross-canvas
            pivots. In theater mode the geometry belongs to TheaterStage (impact lines
            on the full expansion) and only the verdict/playbook panel renders here. */}
        {wan ? (
          (() => {
            const wa = layout.atk.find((a) => a.ip === wan.ip)
            const anchorP: Pt = wa ? wa.p : { x: 70, y: 280 }
            const fg = core
            const sibs = wan.siblings ?? []
            const inter = wan.internalCorrelation ?? []
            const stage = wan.killChain ?? ''
            return (
              <g className="wan-layer">
                {!inTheater ? (
                  <>
                    <path d={bez(anchorP, fg)} className="wan-spine appear" />
                    <text x={(anchorP.x + fg.x) / 2 - 6} y={(anchorP.y + fg.y) / 2 - 10} className="wan-spine-tag" textAnchor="middle">
                      {short(wan.attempts ?? 0)} {lang === 'zh' ? '次登录尝试' : 'login attempts'}
                    </text>

                    {/* /24 lockstep sibling cluster */}
                    <text x={anchorP.x} y={anchorP.y - 26} className="wan-netblock" textAnchor="start">
                      ◇ {lang === 'zh' ? '整段 IP 协同' : 'whole IP block'} · {short(wan.netblockAttempts ?? 0)}
                    </text>
                    {sibs.map((s, k) => {
                      const sp: Pt = { x: anchorP.x + 78, y: clamp(anchorP.y - 44 + k * 30, 20, VBH - 20) }
                      return (
                        <g key={s.ip} className="branch-in">
                          <path d={bez(anchorP, sp)} className="wan-sib-link" />
                          <rect x={sp.x - 5} y={sp.y - 5} width="10" height="10" className="m-attack sib" transform={`rotate(45 ${sp.x} ${sp.y})`} />
                          <text x={sp.x + 12} y={sp.y + 3} className="wan-sib-ip" textAnchor="start">{s.ip} · {short(s.attempts ?? 0)}</text>
                        </g>
                      )
                    })}

                    {/* FortiGate admin target + lockout impact */}
                    {!wan.loading && !wan.error ? (
                      <>
                        <circle cx={fg.x} cy={fg.y} r="34" className="wan-lockring" />
                        <text x={fg.x} y={fg.y + 54} className="wan-lock" textAnchor="middle">⊘ {wan.lockouts ?? 0} {lang === 'zh' ? '次账号锁定' : 'account lockouts'}</text>
                      </>
                    ) : null}

                    {/* cross-canvas post-compromise pivots */}
                    {inter.length ? (
                      <text x={1560} y={112} className="impact-tag" textAnchor="start">
                        {lang === 'zh' ? '内网扩散' : 'INTERNAL SPREAD'}
                      </text>
                    ) : null}
                    {inter.map((c, k) => {
                      const span = inter.length > 1 ? (VBH - 320) / (inter.length - 1) : 0
                      const ip_: Pt = { x: 1560, y: clamp(150 + k * span, 130, VBH - 90) }
                      return (
                        <g key={c.ip} className="branch-in wan-pivot">
                          <path d={bez(fg, ip_)} className="wan-pivot-link" />
                          <circle cx={ip_.x} cy={ip_.y} r="6" className="m-dev high" />
                          <text x={ip_.x + 12} y={ip_.y - 1} className="n-ip" textAnchor="start">{c.ip} · {short(c.deny ?? 0)} {lang === 'zh' ? '次拦截' : 'blocked'}</text>
                          <text x={ip_.x + 12} y={ip_.y + 12} className="wan-rel" textAnchor="start">{clipS(c.relation, 26)}</text>
                        </g>
                      )
                    })}
                  </>
                ) : null}

                {/* deep-analysis panel */}
                {wan.loading ? (
                  <foreignObject x={36} y={678} width={580} height={130}>
                    <div className="an-panel wan-panel">
                      <div className="an-head"><span className="an-kicker">{lang === 'zh' ? 'AI 分析' : 'AI'} · {wan.ip}</span></div>
                      <Analyzing lang={lang} />
                    </div>
                  </foreignObject>
                ) : wan.error ? (
                  <foreignObject x={36} y={690} width={560} height={90}>
                    <div className="an-panel wan-panel"><div className="an-body err">{wan.error}</div></div>
                  </foreignObject>
                ) : (
                  <foreignObject x={28} y={520} width={700} height={472}>
                    <div className={`an-panel wan-panel sev-${wan.severity}`}>
                      <div className="an-head">
                        <span className="an-kicker">{lang === 'zh' ? 'AI 分析 · 入侵判定' : 'AI · INTRUSION'} · {wan.ip}</span>
                        <button className="an-x" onClick={onCloseWan}>✕</button>
                      </div>
                      <div className="an-verdict">
                        <span className={`sev-dot ${wan.severity}`} />
                        <Scramble className="an-vtxt" text={wan.verdict ?? ''} />
                        <span className={`sev-tag ${wan.severity ?? ''}`}>{sevLabel(wan.severity, lang)}</span>
                        {typeof wan.confidence === 'number' ? <span className="wan-conf">{Math.round(wan.confidence * 100)}%</span> : null}
                      </div>
                      <div className="kc-rail">
                        {KILLCHAIN.map((s) => (
                          <span key={s.k} className={`kc-step ${s.k === stage ? 'on' : ''}`}>{lang === 'zh' ? s.zh : s.en}</span>
                        ))}
                      </div>
                      <p className="an-analysis">{wan.campaign}</p>
                      <div className="wan-meta">
                        <span><i>{lang === 'zh' ? '疑似来自' : 'SOURCE'}</i>{wan.attribution}</span>
                        <span><i className="bad">{lang === 'zh' ? '影响' : 'IMPACT'}</i>{wan.blast}</span>
                      </div>
                      {/* impact surface: every node on the attack path (the panel names them; the
                          matching topology nodes carry the .impact class while this panel is open) */}
                      {wan.impactNodes && wan.impactNodes.length ? (
                        <div className="wan-impact">
                          <span className="wan-impact-lab">{lang === 'zh' ? '影响面 · 攻击链节点' : 'IMPACT SURFACE'}</span>
                          {wan.impactNodes.map((n, i) => <span key={i} className={`wan-impact-node ${n === '192.168.1.1' ? 'fw' : ''}`}>{n}</span>)}
                        </div>
                      ) : null}
                      {/* runnable playbook, display-only — the console never executes any of it */}
                      {wan.playbook && wan.playbook.length ? (
                        <div className="wan-pb">
                          <div className="wan-pb-h">
                            <span>{lang === 'zh' ? '处置预案 · 可运行剧本' : 'REMEDIATION PLAYBOOK'}</span>
                            <span className="wan-pb-gate">🔒 {lang === 'zh' ? '仅供审阅 · 系统不执行' : 'REVIEW ONLY · NOT EXECUTED'}</span>
                          </div>
                          {wan.playbook.map((s, i) => (
                            <div key={i} className={`wan-pb-step layer-${s.layer}`}>
                              <div className="wan-pb-step-h">
                                <span className="wan-pb-seq">{String(i + 1).padStart(2, '0')}</span>
                                <span className="wan-pb-target">▸ {s.target}</span>
                                <span className="wan-pb-layer">{s.layer}</span>
                                <span className="wan-pb-why">{s.why}</span>
                              </div>
                              <pre className="wan-pb-cmds">{s.commands.join('\n')}</pre>
                            </div>
                          ))}
                        </div>
                      ) : wan.actions && wan.actions.length ? (
                        <ol className="wan-actions">
                          {wan.actions.map((a, i) => (<li key={i}>{a}</li>))}
                        </ol>
                      ) : null}
                      {onPentest ? (
                        <button className="wan-pentest-cta" onClick={onPentest}>
                          <span className="wpc-txt">{lang === 'zh' ? '去实测暴露面' : 'TEST THE EXPOSURE'}</span>
                          <span className="wpc-arrow">▸</span>
                        </button>
                      ) : null}
                    </div>
                  </foreignObject>
                )}
              </g>
            )
          })()
        ) : null}

        {!drilled && !inTheater ? (
          <HudReadouts
            stats={stats}
            meshCount={meshCount}
            ifCount={layout.ifs.length}
            subCount={layout.ifs.filter((f) => f.sub).length}
            lang={lang}
          />
        ) : null}

        {/* direct door into the full-chain theater (event-less browse mode) */}
        {!drilled && !inTheater && !wan && onOpenTheater ? (
          <g className="theater-door" onClick={onOpenTheater} style={{ cursor: 'pointer' }}>
            <rect x={VBW - 262} y={84} width={220} height={24} fill="transparent" />
            <text x={VBW - 44} y={100} className="theater-door-t" textAnchor="end">
              ⧉ {lang === 'zh' ? '全链路拓扑剧场' : 'FULL-CHAIN THEATER'} ▸
            </text>
          </g>
        ) : null}
      </g>

      {/* ego-network deep-read — pinned to the viewport (OUTSIDE the pan/zoom group,
          which is why it lives here and not in SubnetGraphLayer): identity + every
          justified relationship, each row a jump to that neighbour's own ego net. */}
      {drilled && graph && focusDev
        ? (() => {
            const f = graph.devices.find((d) => d.ip === focusDev)
            if (!f) return null
            const rel = graph.edges
              .filter((e) => e.src === focusDev || e.dst === focusDev)
              .map((e) => ({ ip: e.src === focusDev ? e.dst : e.src, evidence: e.evidence, observed: e.observed, weight: e.weight }))
              .sort((a, b) => b.weight - a.weight)
            const nameOf = (ip: string) => graph.devices.find((d) => d.ip === ip)?.name || ip
            const role = ROLE_ZH[f.role] ? (lang === 'zh' ? ROLE_ZH[f.role] : f.role) : f.role
            return (
              <foreignObject x={20} y={90} width={324} height={Math.min(640, 156 + rel.length * 33)} className="ego-fo">
                <div className="sg-ego">
                  <div className="sg-ego-h">
                    <button className="sg-ego-back" onClick={() => focusOn(null, devPos)}>◂ {lang === 'zh' ? '返回网段' : 'BACK'}</button>
                    <span className="sg-ego-k">{lang === 'zh' ? '关系网 · 深度' : 'EGO NET · DEEP'}</span>
                  </div>
                  <div className={`sg-ego-id t-${f.threat}`}>
                    <b>{f.ip}</b>
                    <span>{f.name ?? (lang === 'zh' ? '无主机名' : 'no hostname')}</span>
                  </div>
                  <div className="sg-ego-meta">
                    {role}{f.vendor !== 'unknown' ? ` · ${f.vendor}` : ''}{f.os ? ` · ${f.os}` : ''}
                    {' · '}
                    {f.seenBy === 'dhcp'
                      ? (lang === 'zh' ? '静默(仅DHCP)' : 'silent (DHCP)')
                      : `${short(f.deny)} ${lang === 'zh' ? '拦截' : 'blocked'}`}
                  </div>
                  <div className="sg-ego-rel-h">{rel.length} {lang === 'zh' ? '条关联 · 点击跳转' : 'relations · click to pivot'}</div>
                  <div className="sg-ego-rels">
                    {rel.length === 0 ? (
                      <div className="sg-ego-iso">{lang === 'zh' ? '孤立主机 — 无任何可观测关联' : 'isolated — no observable relations'}</div>
                    ) : rel.map((r, i) => (
                      <button key={i} className="sg-ego-rel" onClick={() => focusOn(r.ip, devPos)}
                        onMouseEnter={() => onHoverDev(r.ip)} onMouseLeave={() => onHoverDev(null)}>
                        <span className={`sg-ego-tag ${r.observed ? 'obs' : 'inf'}`}>{r.observed ? (lang === 'zh' ? '实测' : 'OBS') : (lang === 'zh' ? '推断' : 'INF')}</span>
                        <span className="sg-ego-ip">{nameOf(r.ip)}</span>
                        <span className="sg-ego-why">{r.evidence}</span>
                      </button>
                    ))}
                  </div>
                  {f.threat !== 'ok' ? (
                    <button className="sg-ego-ai" onClick={() => onDev({ ip: f.ip, flows: f.flows, deny: f.deny, accept: f.accept, threat: f.threat, top_ports: f.topPorts }, graph.cidr)}>
                      ⚡ {lang === 'zh' ? 'AI 威胁研判' : 'AI THREAT VERDICT'}
                    </button>
                  ) : null}
                </div>
              </foreignObject>
            )
          })()
        : null}

      {/* viewport controls — pinned to the plate (never zoomed), sits above the
          legend on the right so it never collides with the bottom-left agent panel */}
      <foreignObject x={VBW - 218} y={drilled ? VBH - 174 : VBH - 44} width={214} height={32} className="zoom-fo">
        <div className="zoom-ctl">
          <button onClick={() => zoomBy(1 / 1.3)} title="zoom out">−</button>
          <span className="zoom-k">{Math.round(view.k * 100)}%</span>
          <button onClick={() => zoomBy(1.3)} title="zoom in">+</button>
          <button className="zoom-reset" onClick={resetView}>{lang === 'zh' ? '复位' : 'RESET'}</button>
          <span className="zoom-hint">{lang === 'zh' ? '拖动平移 · 滚轮缩放' : 'drag · scroll'}</span>
        </div>
      </foreignObject>
    </svg>
  )
}
