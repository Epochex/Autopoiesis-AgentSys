/* Standalone design mockups — four ways to render the attack surface, on REAL
 * data, in the project's own SOC palette. Nothing here touches the app; it is a
 * throwaway preview so the look can be judged before any code is rewritten. */

const PAL = {
  ground: '#0b0d0e', surface: '#101315', surface2: '#15191b',
  ink: '#ece9e3', inkSoft: '#b8b3ab', muted: '#7f7a73',
  rule: '#303538', ruleStrong: '#565b5d',
  acid: '#ccff00', hi: '#e6552d', teal: '#3bb7a6',
  bound: '#c9c4bb', silent: '#3a4042',
}
const $ = (s) => `<span style="color:${PAL.muted}">${s}</span>`
const SVGNS = 'http://www.w3.org/2000/svg'
function el(tag, attrs, kids) {
  const n = document.createElementNS(SVGNS, tag)
  for (const k in attrs) n.setAttribute(k, attrs[k])
  for (const c of kids || []) n.appendChild(c)
  return n
}
function txt(x, y, s, attrs) {
  const n = el('text', { x, y, ...attrs }); n.textContent = s; return n
}

async function load() {
  const [g, env, pen] = await Promise.all(
    ['g1.json', 'env.json', 'pen.json'].map((f) => fetch(f).then((r) => r.json())),
  )
  const riskByIp = new Map((pen.surface || []).map((h) => [h.ip, h.risk_score]))
  const stateByIp = new Map()
  for (const seg of env.address_space) for (const c of seg.cells) stateByIp.set(c.ip, c.state)
  return { g, env, pen, riskByIp, stateByIp }
}

const fill = (state) =>
  state === 'contested' ? PAL.hi : state === 'leased' ? PAL.bound : state === 'unbound' ? PAL.teal : PAL.silent
const EDGE_COLOR = { clash: PAL.hi, bcast: PAL.teal, codst: PAL.ink, fleet: PAL.muted, family: PAL.muted }
const OBSERVED = new Set(['clash', 'bcast', 'codst'])

/* ══ 1. RADAR — concentric trust rings, sweep line, pulsing alerts ══════════ */
function radar(data) {
  const W = 900, H = 900, cx = W / 2, cy = H / 2
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'stage' })
  // rings = trust tiers from the gateway outward
  const tiers = [
    { r: 120, label: '网关 / 核心', roles: ['server'] },
    { r: 230, label: '服务器 · 摄像机', roles: ['camera', 'intercom', 'server'] },
    { r: 335, label: '终端 · 工位', roles: ['workstation', 'mobile', 'unknown'] },
    { r: 420, label: '外网 · 不可信', roles: [] },
  ]
  const g = el('g', {})
  // range rings
  for (const t of tiers) {
    g.appendChild(el('circle', { cx, cy, r: t.r, fill: 'none', stroke: PAL.rule, 'stroke-width': 1 }))
    g.appendChild(txt(cx + 6, cy - t.r + 16, t.label, { fill: PAL.muted, 'font-size': 12, 'letter-spacing': '.12em' }))
  }
  // spokes
  for (let a = 0; a < 360; a += 30) {
    const rad = (a * Math.PI) / 180
    g.appendChild(el('line', { x1: cx, y1: cy, x2: cx + 420 * Math.cos(rad), y2: cy + 420 * Math.sin(rad), stroke: PAL.rule, 'stroke-width': 0.5, opacity: 0.5 }))
  }
  // place devices on the tier that matches their role, angle by hash
  const roleTier = (role) => {
    for (let i = 0; i < tiers.length; i++) if (tiers[i].roles.includes(role)) return i
    return 2
  }
  const pos = new Map()
  const perTier = [0, 0, 0, 0]
  const counts = data.g.devices.reduce((m, d) => (m[roleTier(d.role)] = (m[roleTier(d.role)] || 0) + 1, m), {})
  for (const d of data.g.devices) {
    const ti = roleTier(d.role)
    const n = counts[ti] || 1
    const idx = perTier[ti]++
    const ang = (idx / n) * Math.PI * 2 - Math.PI / 2 + ti * 0.35
    const rr = ti === 0 ? 70 : tiers[ti - 1] ? (tiers[ti - 1].r + tiers[ti].r) / 2 : tiers[ti].r - 50
    pos.set(d.ip, { x: cx + rr * Math.cos(ang), y: cy + rr * Math.sin(ang), tier: ti })
  }
  // chords = mined relations
  for (const e of data.g.edges) {
    const a = pos.get(e.src), b = pos.get(e.dst)
    if (!a || !b) continue
    g.appendChild(el('path', {
      d: `M${a.x} ${a.y} Q${cx} ${cy} ${b.x} ${b.y}`, fill: 'none',
      stroke: EDGE_COLOR[e.kind] || PAL.muted, 'stroke-width': OBSERVED.has(e.kind) ? 1 : 0.7,
      'stroke-dasharray': OBSERVED.has(e.kind) ? '' : '3 4', opacity: 0.28,
    }))
  }
  // sweep line (animated)
  const sweep = el('g', {})
  sweep.appendChild(el('line', { x1: cx, y1: cy, x2: cx + 420, y2: cy, stroke: PAL.acid, 'stroke-width': 2 }))
  const wedge = el('path', { d: `M${cx} ${cy} L${cx + 420} ${cy} A420 420 0 0 0 ${cx + 420 * Math.cos(-0.5)} ${cy + 420 * Math.sin(-0.5)} Z`, fill: PAL.acid, opacity: 0.06 })
  sweep.appendChild(wedge)
  sweep.appendChild(el('animateTransform', { attributeName: 'transform', type: 'rotate', from: `0 ${cx} ${cy}`, to: `360 ${cx} ${cy}`, dur: '6s', repeatCount: 'indefinite' }))
  g.appendChild(sweep)
  // nodes
  for (const d of data.g.devices) {
    const p = pos.get(d.ip); if (!p) continue
    const st = data.stateByIp.get(d.ip) || 'silent'
    const risk = data.riskByIp.get(d.ip) || (d.threat === 'high' ? 60 : 10)
    const r = 3.5 + Math.sqrt(risk) * 1.1
    if (st === 'contested') {
      const ring = el('circle', { cx: p.x, cy: p.y, r: r + 4, fill: 'none', stroke: PAL.hi, 'stroke-width': 1.5 })
      ring.appendChild(el('animate', { attributeName: 'r', values: `${r + 3};${r + 12};${r + 3}`, dur: '1.6s', repeatCount: 'indefinite' }))
      ring.appendChild(el('animate', { attributeName: 'opacity', values: '0.9;0;0.9', dur: '1.6s', repeatCount: 'indefinite' }))
      g.appendChild(ring)
    }
    g.appendChild(el('circle', { cx: p.x, cy: p.y, r, fill: fill(st), stroke: PAL.ground, 'stroke-width': 1 }))
    if (st === 'contested' || risk >= 70)
      g.appendChild(txt(p.x, p.y + r + 12, d.ip.split('.').pop(), { fill: PAL.inkSoft, 'font-size': 10, 'text-anchor': 'middle', 'font-family': 'IBM Plex Mono, monospace' }))
  }
  // gateway core
  g.appendChild(el('circle', { cx, cy, r: 9, fill: PAL.acid }))
  g.appendChild(txt(cx, cy + 26, 'FortiGate', { fill: PAL.acid, 'font-size': 11, 'text-anchor': 'middle', 'letter-spacing': '.1em' }))
  svg.appendChild(g)
  return svg
}

/* ══ 2. ATTACK-CHAIN LANES — outside-in vertical depth ══════════════════════ */
function chain(data) {
  const W = 1360, H = 820
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'stage' })
  const g = el('g', {})
  const bf = data.env.findings.find((f) => f.fault_class === 'mgmt_bruteforce')
  const blocks = (bf?.measured.top_blocks || []).slice(0, 8)
  // lane 1: external attackers streaming down
  g.appendChild(txt(40, 34, `外网攻击源 · ${bf?.measured.distinct_sources || 0} 个地址 · ${(bf?.measured.failed_logins || 0).toLocaleString()} 次管理登录失败`, { fill: PAL.hi, 'font-size': 15, 'letter-spacing': '.05em' }))
  blocks.forEach((b, i) => {
    const x = 90 + i * 155
    g.appendChild(txt(x, 70, b.block, { fill: PAL.inkSoft, 'font-size': 11, 'text-anchor': 'middle', 'font-family': 'IBM Plex Mono, monospace' }))
    g.appendChild(txt(x, 86, `${b.failed_logins}×`, { fill: PAL.hi, 'font-size': 11, 'text-anchor': 'middle', 'font-family': 'IBM Plex Mono, monospace' }))
    // falling packets
    for (let k = 0; k < 4; k++) {
      const tri = el('path', { d: `M${x} ${100 + k * 22} l4 8 l-8 0 z`, fill: PAL.hi, opacity: 0.7 - k * 0.12 })
      tri.appendChild(el('animate', { attributeName: 'opacity', values: '0;0.8;0', dur: '1.4s', begin: `${(i * 0.1 + k * 0.2).toFixed(2)}s`, repeatCount: 'indefinite' }))
      g.appendChild(tri)
    }
  })
  // gate
  const gateY = 210
  g.appendChild(el('rect', { x: 40, y: gateY, width: W - 80, height: 46, fill: PAL.surface2, stroke: PAL.ruleStrong }))
  g.appendChild(txt(W / 2, gateY + 29, 'FortiGate 边界闸门 · 策略 ALL · NAT · 51 条来源 IP 临时登录禁用', { fill: PAL.ink, 'font-size': 13, 'text-anchor': 'middle', 'letter-spacing': '.06em' }))
  // deflected bounce marks
  for (let i = 0; i < 8; i++) {
    const x = 120 + i * 150
    g.appendChild(el('path', { d: `M${x} ${gateY} l-10 -14 M${x} ${gateY} l10 -14`, stroke: PAL.hi, 'stroke-width': 1.5, opacity: 0.5 }))
  }
  // lane 3: segments
  const segs = data.env.address_space
  const segW = (W - 80) / segs.length
  segs.forEach((seg, si) => {
    const x0 = 40 + si * segW
    g.appendChild(el('line', { x1: x0, y1: 300, x2: x0, y2: 300, stroke: PAL.rule }))
    g.appendChild(txt(x0 + 16, 300, seg.segment, { fill: PAL.ink, 'font-size': 14, 'letter-spacing': '.04em' }))
    g.appendChild(txt(x0 + 16, 318, `${seg.interface || ''} · 绑定 ${seg.counts.leased || 0} · 无绑定 ${seg.counts.unbound || 0} · 争用 ${seg.counts.contested || 0}`, { fill: PAL.muted, 'font-size': 10, 'letter-spacing': '.06em' }))
    // host dots for this segment (leased/unbound/contested only)
    const hosts = seg.cells.filter((c) => c.state !== 'silent')
    const cols = 14
    hosts.forEach((c, hi) => {
      const hx = x0 + 20 + (hi % cols) * 26
      const hy = 350 + Math.floor(hi / cols) * 26
      const r = 5 + Math.sqrt(data.riskByIp.get(c.ip) || 6) * 0.7
      if (c.state === 'contested') {
        const ring = el('circle', { cx: hx, cy: hy, r: r + 3, fill: 'none', stroke: PAL.hi, 'stroke-width': 1.5 })
        ring.appendChild(el('animate', { attributeName: 'r', values: `${r + 2};${r + 9};${r + 2}`, dur: '1.5s', repeatCount: 'indefinite' }))
        ring.appendChild(el('animate', { attributeName: 'opacity', values: '1;0;1', dur: '1.5s', repeatCount: 'indefinite' }))
        g.appendChild(ring)
        // attack line reaching down to the contested host
        const beam = el('line', { x1: x0 + segW / 2, y1: gateY + 46, x2: hx, y2: hy, stroke: PAL.hi, 'stroke-width': 1, 'stroke-dasharray': '4 4', opacity: 0.5 })
        g.appendChild(beam)
      }
      g.appendChild(el('circle', { cx: hx, cy: hy, r, fill: fill(c.state), stroke: PAL.ground, 'stroke-width': 1 }))
      if (c.state === 'contested') g.appendChild(txt(hx, hy - r - 5, c.ip, { fill: PAL.hi, 'font-size': 10, 'text-anchor': 'middle', 'font-family': 'IBM Plex Mono, monospace' }))
    })
    if (si) g.appendChild(el('line', { x1: x0, y1: 296, x2: x0, y2: H - 30, stroke: PAL.rule, 'stroke-dasharray': '2 6' }))
  })
  svg.appendChild(g)
  return svg
}

/* ══ 3. ISOMETRIC BATTLE GRID — 2.5D field, height = exposure ═══════════════ */
function iso(data) {
  const W = 1360, H = 860
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'stage' })
  const g = el('g', {})
  const TW = 17, TH = 8.5 // tile half-width/height
  const project = (col, row, ox, oy) => ({ x: ox + (col - row) * TW, y: oy + (col + row) * TH })
  const segs = data.env.address_space
  const origins = [{ ox: 320, oy: 170 }, { ox: 700, oy: 470 }, { ox: 1080, oy: 250 }]
  segs.forEach((seg, si) => {
    const { ox, oy } = origins[si]
    g.appendChild(txt(ox - 120, oy - 20, seg.segment, { fill: PAL.ink, 'font-size': 14, 'letter-spacing': '.04em' }))
    g.appendChild(txt(ox - 120, oy - 4, seg.interface || '', { fill: PAL.muted, 'font-size': 10, 'letter-spacing': '.12em' }))
    // 16x16 grid
    seg.cells.forEach((c, i) => {
      const col = i % 16, row = Math.floor(i / 16)
      const p = project(col, row, ox, oy)
      if (c.state === 'silent') {
        // flat faint diamond
        g.appendChild(el('path', { d: `M${p.x} ${p.y - TH} L${p.x + TW} ${p.y} L${p.x} ${p.y + TH} L${p.x - TW} ${p.y} Z`, fill: 'none', stroke: '#1c2224', 'stroke-width': 0.5 }))
        return
      }
      const risk = data.riskByIp.get(c.ip) || (c.state === 'contested' ? 90 : c.state === 'unbound' ? 20 : 14)
      const h = 6 + risk * 0.55
      const top = fill(c.state)
      // left + right faces (darker), then top diamond
      const left = `M${p.x - TW} ${p.y} L${p.x} ${p.y + TH} L${p.x} ${p.y + TH - h} L${p.x - TW} ${p.y - h} Z`
      const right = `M${p.x + TW} ${p.y} L${p.x} ${p.y + TH} L${p.x} ${p.y + TH - h} L${p.x + TW} ${p.y - h} Z`
      const shade = (hex, f) => {
        const n = parseInt(hex.slice(1), 16)
        const r = Math.max(0, ((n >> 16) & 255) * f), gg = Math.max(0, ((n >> 8) & 255) * f), b = Math.max(0, (n & 255) * f)
        return `rgb(${r | 0},${gg | 0},${b | 0})`
      }
      g.appendChild(el('path', { d: left.replace(/([\d.]+) ([\d.]+)/g, (m) => m), fill: shade(top, 0.55) }))
      g.appendChild(el('path', { d: right, fill: shade(top, 0.4) }))
      g.appendChild(el('path', { d: `M${p.x} ${p.y - TH - h} L${p.x + TW} ${p.y - h} L${p.x} ${p.y + TH - h} L${p.x - TW} ${p.y - h} Z`, fill: top, stroke: PAL.ground, 'stroke-width': 0.5 }))
      if (c.state === 'contested') {
        const glow = el('circle', { cx: p.x, cy: p.y - h, r: 10, fill: PAL.hi, opacity: 0.6 })
        glow.appendChild(el('animate', { attributeName: 'opacity', values: '0.2;0.8;0.2', dur: '1.4s', repeatCount: 'indefinite' }))
        glow.appendChild(el('animate', { attributeName: 'r', values: '8;18;8', dur: '1.4s', repeatCount: 'indefinite' }))
        g.appendChild(glow)
        g.appendChild(txt(p.x, p.y - h - 18, c.ip, { fill: PAL.hi, 'font-size': 10, 'text-anchor': 'middle', 'font-family': 'IBM Plex Mono, monospace' }))
      }
    })
  })
  svg.appendChild(g)
  return svg
}

/* ══ 4. HEX TERRITORY — factions by vendor/relation cluster ═════════════════ */
function hex(data) {
  const W = 1200, H = 760
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'stage' })
  const g = el('g', {})
  // clusters from the graph (connected components over strong edges)
  const adj = new Map()
  const strong = data.g.edges.filter((e) => OBSERVED.has(e.kind))
  for (const e of strong) {
    adj.set(e.src, [...(adj.get(e.src) || []), e.dst])
    adj.set(e.dst, [...(adj.get(e.dst) || []), e.src])
  }
  const comp = new Map(); let cc = 0
  for (const d of data.g.devices) {
    if (comp.has(d.ip)) continue
    const stack = [d.ip]; comp.set(d.ip, cc)
    while (stack.length) { const u = stack.pop(); for (const v of adj.get(u) || []) if (!comp.has(v)) { comp.set(v, cc); stack.push(v) } }
    cc++
  }
  const factionColor = [PAL.hi, PAL.teal, PAL.acid, '#b98cff', '#e0a94a', PAL.bound]
  const groups = new Map()
  for (const d of data.g.devices) { const c = comp.get(d.ip); groups.set(c, [...(groups.get(c) || []), d]) }
  const sorted = [...groups.values()].sort((a, b) => b.length - a.length)
  const HR = 20, HW = HR * Math.sqrt(3)
  let gx = 90, gy = 120, shelfH = 0
  sorted.forEach((grp, gi) => {
    const cols = Math.ceil(Math.sqrt(grp.length))
    const wpx = cols * HW + HW
    if (gx + wpx > W - 60 && gx > 90) { gx = 90; gy += shelfH + 70; shelfH = 0 }
    const col = factionColor[gi % factionColor.length]
    grp.forEach((d, i) => {
      const cc2 = i % cols, rr = Math.floor(i / cols)
      const hx = gx + cc2 * HW + (rr % 2 ? HW / 2 : 0)
      const hy = gy + rr * HR * 1.5
      const pts = []
      for (let a = 0; a < 6; a++) { const ang = (Math.PI / 3) * a - Math.PI / 6; pts.push(`${(hx + HR * Math.cos(ang)).toFixed(1)},${(hy + HR * Math.sin(ang)).toFixed(1)}`) }
      const st = data.stateByIp.get(d.ip) || 'silent'
      const contested = st === 'contested'
      g.appendChild(el('polygon', { points: pts.join(' '), fill: contested ? PAL.hi : PAL.surface2, stroke: col, 'stroke-width': contested ? 2 : 1.4, opacity: contested ? 1 : 0.9 }))
      g.appendChild(txt(hx, hy + 3, d.ip.split('.').pop(), { fill: contested ? PAL.ground : PAL.inkSoft, 'font-size': 9, 'text-anchor': 'middle', 'font-family': 'IBM Plex Mono, monospace' }))
      if (contested) {
        const ring = el('polygon', { points: pts.join(' '), fill: 'none', stroke: PAL.hi, 'stroke-width': 2 })
        ring.appendChild(el('animate', { attributeName: 'opacity', values: '1;0.1;1', dur: '1.3s', repeatCount: 'indefinite' }))
        g.appendChild(ring)
      }
      shelfH = Math.max(shelfH, (rr + 1) * HR * 1.5)
    })
    const label = grp[0].vendor && grp[0].vendor !== 'unknown' ? grp[0].vendor : `簇 ${gi + 1}`
    g.appendChild(txt(gx - 6, gy - 26, `${label} · ${grp.length}`, { fill: col, 'font-size': 12, 'letter-spacing': '.08em' }))
    gx += wpx + 40
  })
  svg.appendChild(g)
  return svg
}

const RENDER = { radar, chain, iso, hex }

load().then((data) => {
  for (const id in RENDER) {
    const host = document.getElementById(id)
    if (host) host.appendChild(RENDER[id](data))
  }
  document.body.dataset.ready = '1'
})
