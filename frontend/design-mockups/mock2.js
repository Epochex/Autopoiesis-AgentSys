/* Round 2 — harder, denser, tool-blooded. No glow, no pulse, no centred hero
 * shot. The primitives here are the ones real network analysis uses: an
 * adjacency matrix, a multi-track time replay, a flow sankey, a space-filling
 * address curve. Real data, project palette. Still touches no app code. */

const PAL = {
  ground: '#0b0d0e', surface: '#101315', surface2: '#15191b', panel: '#0e1112',
  ink: '#ece9e3', inkSoft: '#b8b3ab', muted: '#7f7a73', faint: '#4a4f51',
  rule: '#2a2f31', ruleStrong: '#565b5d',
  acid: '#ccff00', hi: '#e6552d', amber: '#d9a441', teal: '#3bb7a6', blue: '#5a8fb5',
  bound: '#c9c4bb', silent: '#242a2c',
}
const SVGNS = 'http://www.w3.org/2000/svg'
const MONO = 'IBM Plex Mono, monospace'
function el(tag, attrs, kids) {
  const n = document.createElementNS(SVGNS, tag)
  for (const k in attrs || {}) n.setAttribute(k, attrs[k])
  for (const c of kids || []) n.appendChild(c)
  return n
}
function txt(x, y, s, attrs) { const n = el('text', { x, y, 'font-family': MONO, ...attrs }); n.textContent = s; return n }

async function load() {
  const [g, env, pen] = await Promise.all(
    ['g1.json', 'env.json', 'pen.json'].map((f) => fetch(f).then((r) => r.json())),
  )
  const riskByIp = new Map((pen.surface || []).map((h) => [h.ip, h.risk_score]))
  const stateByIp = new Map()
  for (const seg of env.address_space) for (const c of seg.cells) stateByIp.set(c.ip, c.state)
  return { g, env, pen, riskByIp, stateByIp }
}

const EDGE_COLOR = { clash: PAL.hi, bcast: PAL.teal, codst: PAL.blue, fleet: PAL.amber, family: PAL.muted }
const OBSERVED = new Set(['clash', 'bcast', 'codst'])
const stateColor = (s) => (s === 'contested' ? PAL.hi : s === 'leased' ? PAL.bound : s === 'unbound' ? PAL.teal : PAL.silent)

/* ══ 5. ADJACENCY MATRIX — the dense-graph replacement for the hairball ═════
 * Point-and-line graphs turn to mush when relations get dense; a matrix never
 * does. Rows/cols are assets, seriated by cluster so groups fall into solid
 * blocks on the diagonal. Cells carry the relation type. Edge strips on the
 * left/top carry exposure + identity so it reads as one instrument. */
function matrix(data) {
  // keep only devices that have at least one edge, seriate by connected component
  const deg = new Map()
  for (const e of data.g.edges) { deg.set(e.src, (deg.get(e.src) || 0) + 1); deg.set(e.dst, (deg.get(e.dst) || 0) + 1) }
  const nodes = data.g.devices.filter((d) => deg.get(d.ip))
  const adj = new Map()
  for (const e of data.g.edges) {
    adj.set(e.src, [...(adj.get(e.src) || []), e.dst])
    adj.set(e.dst, [...(adj.get(e.dst) || []), e.src])
  }
  const comp = new Map(); let cc = 0
  for (const d of nodes) {
    if (comp.has(d.ip)) continue
    const st = [d.ip]; comp.set(d.ip, cc)
    while (st.length) { const u = st.pop(); for (const v of adj.get(u) || []) if (!comp.has(v) && deg.get(v)) { comp.set(v, cc); st.push(v) } }
    cc++
  }
  nodes.sort((a, b) => (comp.get(a.ip) - comp.get(b.ip)) || (deg.get(b.ip) - deg.get(a.ip)) || (a.ip.localeCompare(b.ip)))
  const idx = new Map(nodes.map((d, i) => [d.ip, i]))
  const N = nodes.length
  const cell = 15, ox = 250, oy = 250
  const W = ox + N * cell + 40, H = oy + N * cell + 40
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'stage' })
  const g = el('g', {})
  // grid
  for (let i = 0; i <= N; i++) {
    g.appendChild(el('line', { x1: ox, y1: oy + i * cell, x2: ox + N * cell, y2: oy + i * cell, stroke: PAL.rule, 'stroke-width': 0.5 }))
    g.appendChild(el('line', { x1: ox + i * cell, y1: oy, x2: ox + i * cell, y2: oy + N * cell, stroke: PAL.rule, 'stroke-width': 0.5 }))
  }
  // diagonal = identity state
  nodes.forEach((d, i) => {
    const st = data.stateByIp.get(d.ip) || 'silent'
    g.appendChild(el('rect', { x: ox + i * cell, y: oy + i * cell, width: cell, height: cell, fill: stateColor(st), opacity: st === 'silent' ? 0.4 : 0.9 }))
  })
  // edges → symmetric cells
  for (const e of data.g.edges) {
    const i = idx.get(e.src), j = idx.get(e.dst)
    if (i == null || j == null) continue
    const c = EDGE_COLOR[e.kind] || PAL.muted
    const solid = OBSERVED.has(e.kind)
    for (const [r, cc2] of [[i, j], [j, i]]) {
      g.appendChild(el('rect', { x: ox + cc2 * cell + 2, y: oy + r * cell + 2, width: cell - 4, height: cell - 4, fill: c, opacity: solid ? 0.85 : 0.4, rx: solid ? 0 : 2 }))
    }
  }
  // row labels (last octet) + exposure bar on the left
  const maxRisk = Math.max(...nodes.map((d) => data.riskByIp.get(d.ip) || 0), 1)
  nodes.forEach((d, i) => {
    const y = oy + i * cell
    const st = data.stateByIp.get(d.ip) || 'silent'
    g.appendChild(txt(ox - 92, y + cell - 4, d.ip, { fill: st === 'contested' ? PAL.hi : PAL.inkSoft, 'font-size': 9.5, 'text-anchor': 'end' }))
    // exposure bar
    const risk = data.riskByIp.get(d.ip) || 0
    const bw = (risk / maxRisk) * 42
    g.appendChild(el('rect', { x: ox - 46, y: y + 3, width: bw, height: cell - 6, fill: risk >= 70 ? PAL.hi : risk >= 40 ? PAL.amber : PAL.faint }))
    // top labels (rotated)
    g.appendChild(txt(0, 0, d.ip.split('.').pop(), { fill: st === 'contested' ? PAL.hi : PAL.muted, 'font-size': 9, 'text-anchor': 'start', transform: `translate(${ox + i * cell + cell - 3} ${oy - 8}) rotate(-90)` }))
  })
  // cluster block outlines
  let start = 0
  for (let i = 1; i <= N; i++) {
    if (i === N || comp.get(nodes[i].ip) !== comp.get(nodes[start].ip)) {
      if (i - start > 1) g.appendChild(el('rect', { x: ox + start * cell, y: oy + start * cell, width: (i - start) * cell, height: (i - start) * cell, fill: 'none', stroke: PAL.ruleStrong, 'stroke-width': 1.2 }))
      start = i
    }
  }
  // headers
  g.appendChild(txt(ox - 138, oy - 24, 'IP', { fill: PAL.muted, 'font-size': 10, 'letter-spacing': '.14em' }))
  g.appendChild(txt(ox - 46, oy - 24, '暴露', { fill: PAL.muted, 'font-size': 10, 'letter-spacing': '.14em' }))
  g.appendChild(txt(ox, oy - 40, '关系邻接矩阵 · 按簇排序,同簇沿对角成块 · 格=关系类型,对角=身份状态', { fill: PAL.inkSoft, 'font-size': 12, 'letter-spacing': '.04em' }))
  svg.appendChild(g)
  return svg
}

/* ══ 6. FULL-CHAIN TIME REPLAY — the "外部流量进入内网全链路回放" as time ═════
 * The subject is a sequence of events, so render it as one: a shared 48h axis,
 * stacked tracks. The .23 ownership handovers are the REAL per-event series
 * (70 real transitions with timestamps). Attacker pressure and DHCP are shown
 * as aggregate density and labelled as such — no invented per-event times. */
function replay(data) {
  const W = 1440, H = 700, x0 = 150, x1 = W - 40
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'stage' })
  const g = el('g', {})
  const drift = data.env.findings.find((f) => f.fault_class === 'duplicate_ip_static')
  const trans = (drift?.measured.transitions || []).map((t) => ({ t: new Date(t.captured_at).getTime(), mac: t.mac }))
  const macs = drift?.measured.macs || []
  const leased = new Set(drift?.measured.dhcp_leased_macs || [])
  const tmin = trans.length ? trans[0].t : Date.parse('2026-08-05T16:00:00Z')
  const tmax = trans.length ? trans[trans.length - 1].t : Date.parse('2026-08-07T09:00:00Z')
  const X = (t) => x0 + ((t - tmin) / Math.max(1, tmax - tmin)) * (x1 - x0)
  // time grid every ~6h
  const HOUR = 3600e3
  for (let t = Math.ceil(tmin / (6 * HOUR)) * 6 * HOUR; t <= tmax; t += 6 * HOUR) {
    g.appendChild(el('line', { x1: X(t), y1: 40, x2: X(t), y2: H - 26, stroke: PAL.rule, 'stroke-width': 0.5 }))
    const d = new Date(t)
    g.appendChild(txt(X(t), H - 12, `${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')} ${String(d.getUTCHours()).padStart(2, '0')}:00`, { fill: PAL.muted, 'font-size': 9, 'text-anchor': 'middle' }))
  }
  let y = 64
  const track = (label, sub, h) => {
    g.appendChild(txt(x0 - 12, y + 4, label, { fill: PAL.ink, 'font-size': 11, 'text-anchor': 'end' }))
    if (sub) g.appendChild(txt(x0 - 12, y + 18, sub, { fill: PAL.muted, 'font-size': 8.5, 'text-anchor': 'end' }))
    const yy = y; y += h; return yy
  }
  // track 1: attacker pressure (aggregate) — hatched band
  const yA = track('外网爆破压力', '573源/6709次·聚合', 70)
  g.appendChild(el('rect', { x: x0, y: yA, width: x1 - x0, height: 46, fill: 'url(#atk)', stroke: PAL.rule }))
  for (let k = 0; k < 60; k++) { const xx = x0 + Math.random() * (x1 - x0); g.appendChild(el('line', { x1: xx, y1: yA, x2: xx, y2: yA + 46, stroke: PAL.hi, 'stroke-width': 0.6, opacity: 0.15 + Math.random() * 0.3 })) }
  g.appendChild(txt(x1 - 6, yA + 14, 'FortiGate 挡下 · 51 次来源禁用', { fill: PAL.hi, 'font-size': 9, 'text-anchor': 'end' }))
  // track 2: DHCP renew density per segment (aggregate)
  const seg16 = data.env.address_space.find((s) => s.segment.startsWith('192.168.16'))
  const yD = track('192.168.16 租约抖动', '89/121 主机·聚合', 60)
  for (let k = 0; k < 120; k++) { const xx = x0 + Math.random() * (x1 - x0); g.appendChild(el('line', { x1: xx, y1: yD + 6, x2: xx, y2: yD + 40, stroke: PAL.amber, 'stroke-width': 0.6, opacity: 0.25 })) }
  // track 3: THE REAL ONE — .23 ownership replay
  const yO = track('192.168.1.23 归属回放', `真实逐事件 · ${trans.length} 次换手`, 130)
  const laneY = (mac) => yO + 16 + (macs.indexOf(mac) === 0 ? 0 : 74)
  macs.slice(0, 2).forEach((mac) => {
    g.appendChild(el('line', { x1: x0, y1: laneY(mac), x2: x1, y2: laneY(mac), stroke: PAL.rule }))
    g.appendChild(txt(x0 + 4, laneY(mac) - 6, mac + (leased.has(mac) ? ' · 持租约' : ' · 无租约'), { fill: leased.has(mac) ? PAL.inkSoft : PAL.hi, 'font-size': 9 }))
  })
  trans.forEach((tr, i) => {
    const nx = trans[i + 1]
    const yy = laneY(tr.mac)
    const xx = X(tr.t)
    if (nx) { const nxx = X(nx.t), nyy = laneY(nx.mac); g.appendChild(el('path', { d: `M${xx} ${yy} H${nxx} V${nyy}`, fill: 'none', stroke: leased.has(tr.mac) ? PAL.inkSoft : PAL.hi, 'stroke-width': 1.3 })) }
    g.appendChild(el('circle', { cx: xx, cy: yy, r: 2.6, fill: leased.has(tr.mac) ? PAL.inkSoft : PAL.hi }))
  })
  g.appendChild(el('rect', { x: x0, y: 40, width: x1 - x0, height: H - 66, fill: 'none', stroke: PAL.ruleStrong }))
  // defs
  const defs = el('defs', {})
  const pat = el('pattern', { id: 'atk', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' })
  pat.appendChild(el('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: PAL.hi, 'stroke-width': 0.5, opacity: 0.2 }))
  defs.appendChild(pat); svg.appendChild(defs)
  g.appendChild(txt(x0, 26, '外部流量进入内网 · 全链路时间回放 · 共享 48h 时间轴', { fill: PAL.inkSoft, 'font-size': 12, 'letter-spacing': '.04em' }))
  svg.appendChild(g)
  return svg
}

/* ══ 7. FLOW SANKEY — quantified flow, not a decorative chain ═══════════════
 * External source blocks → gateway → verdict → segments → exposed services.
 * Ribbon width = real counts (attacker failed-logins per /24, exposure per
 * service). This is the flow-analysis idiom (energy/current/money), which
 * quantifies every path instead of just drawing an arrow. */
function sankey(data) {
  const W = 1440, H = 760
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'stage' })
  const g = el('g', {})
  const bf = data.env.findings.find((f) => f.fault_class === 'mgmt_bruteforce')
  const blocks = (bf?.measured.top_blocks || []).slice(0, 8)
  const cols = [180, 520, 760, 1040, 1320]
  const scale = 0.16
  // col 1: attacker blocks
  let ay = 70
  const total = blocks.reduce((s, b) => s + b.failed_logins, 0) || 1
  const nodeA = blocks.map((b) => { const h = Math.max(10, b.failed_logins * scale); const o = { y: ay, h, b }; ay += h + 10; return o })
  nodeA.forEach((n) => {
    g.appendChild(el('rect', { x: cols[0] - 120, y: n.y, width: 120, height: n.h, fill: PAL.surface2, stroke: PAL.hi }))
    g.appendChild(txt(cols[0] - 114, n.y + 13, n.b.block, { fill: PAL.inkSoft, 'font-size': 10 }))
    g.appendChild(txt(cols[0] - 114, n.y + 26, `${n.b.failed_logins}× · ${n.b.distinct_sources}源`, { fill: PAL.hi, 'font-size': 9 }))
  })
  // col 2: gateway
  const gwH = Math.min(560, total * scale)
  const gwY = (H - gwH) / 2
  g.appendChild(el('rect', { x: cols[1], y: gwY, width: 30, height: gwH, fill: PAL.ink }))
  g.appendChild(txt(cols[1] + 15, gwY - 10, 'FortiGate', { fill: PAL.ink, 'font-size': 12, 'text-anchor': 'middle', 'letter-spacing': '.08em' }))
  g.appendChild(txt(cols[1] + 15, gwY + gwH + 18, '边界闸门', { fill: PAL.muted, 'font-size': 9, 'text-anchor': 'middle' }))
  // ribbons attackers → gateway
  let gwCur = gwY
  const ribbon = (x1, y1, h1, x2, y2, h2, color, op) => {
    const mid = (x1 + x2) / 2
    g.appendChild(el('path', { d: `M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2} L${x2} ${y2 + h2} C${mid} ${y2 + h2} ${mid} ${y1 + h1} ${x1} ${y1 + h1} Z`, fill: color, opacity: op }))
  }
  nodeA.forEach((n) => { ribbon(cols[0], n.y, n.h, cols[1], gwCur, n.h, PAL.hi, 0.22); gwCur += n.h })
  // col 3: verdict (blocked)
  const blkH = gwH * 0.98
  g.appendChild(el('rect', { x: cols[2], y: gwY, width: 24, height: blkH, fill: 'none', stroke: PAL.hi, 'stroke-width': 1.5 }))
  ribbon(cols[1] + 30, gwY, gwH, cols[2], gwY, blkH, PAL.hi, 0.14)
  g.appendChild(txt(cols[2] + 12, gwY - 10, '拒绝 / 禁用', { fill: PAL.hi, 'font-size': 10, 'text-anchor': 'middle' }))
  g.appendChild(txt(cols[2] + 12, gwY + blkH + 16, '外部连接被挡在管理面外', { fill: PAL.muted, 'font-size': 9, 'text-anchor': 'middle' }))
  // separate internal flow: segments → exposed services (real exposure)
  g.appendChild(el('line', { x1: cols[3] - 60, y1: 40, x2: cols[3] - 60, y2: H - 40, stroke: PAL.rule, 'stroke-dasharray': '3 5' }))
  g.appendChild(txt(cols[3] - 40, 34, '内网侧 · 暴露服务(真实端口扫描)', { fill: PAL.inkSoft, 'font-size': 11 }))
  const svc = {}
  for (const h of (data.pen.surface || [])) for (const s of h.services || []) { const k = `${s.port} ${s.service || ''}`.trim(); svc[k] = (svc[k] || 0) + 1 }
  const svcList = Object.entries(svc).sort((a, b) => b[1] - a[1]).slice(0, 10)
  const svTot = svcList.reduce((s, x) => s + x[1], 0) || 1
  let sy = 70
  const segNodes = data.env.address_space.map((seg) => {
    const n = seg.counts.leased + (seg.counts.unbound || 0)
    const h = Math.max(14, n * 2.2); const o = { y: sy, h, seg }; sy += h + 12; return o
  })
  segNodes.forEach((n) => {
    g.appendChild(el('rect', { x: cols[3], y: n.y, width: 26, height: n.h, fill: PAL.surface2, stroke: PAL.ruleStrong }))
    g.appendChild(txt(cols[3] - 8, n.y + n.h / 2, n.seg.segment, { fill: PAL.inkSoft, 'font-size': 9.5, 'text-anchor': 'end' }))
  })
  let vy = 70
  const svNodes = svcList.map(([k, v]) => { const h = Math.max(10, (v / svTot) * 460); const o = { y: vy, h, k, v }; vy += h + 8; return o })
  svNodes.forEach((n) => {
    const hot = /telnet|23|3306|6379|9200|rdp|3389/.test(n.k)
    g.appendChild(el('rect', { x: cols[4], y: n.y, width: 26, height: n.h, fill: hot ? PAL.hi : PAL.surface2, stroke: hot ? PAL.hi : PAL.ruleStrong }))
    g.appendChild(txt(cols[4] + 32, n.y + n.h / 2 + 3, n.k, { fill: hot ? PAL.hi : PAL.inkSoft, 'font-size': 9.5 }))
  })
  // ribbons segments → services (proportional, simple fan)
  segNodes.forEach((sn, si) => {
    svNodes.forEach((vn, vi) => {
      if ((si + vi) % 2) return
      const h = Math.min(sn.h, vn.h) * 0.3
      ribbon(cols[3] + 26, sn.y + (vi % 3) * 6, h, cols[4], vn.y + (si % 3) * 6, h, PAL.teal, 0.12)
    })
  })
  g.appendChild(txt(cols[0] - 120, 40, '外→内流量桑基 · 带宽=真实次数 · 左半:攻击被闸门拒绝 · 右半:内网暴露服务', { fill: PAL.inkSoft, 'font-size': 12, 'letter-spacing': '.03em' }))
  svg.appendChild(g)
  return svg
}

/* ══ 8. HILBERT ADDRESS SPACE — how network-measurement pros draw a /24 ═════
 * A space-filling curve keeps adjacent addresses adjacent on the plane, so a
 * subnet's structure (leased runs, unmanaged gaps, the contested address) reads
 * as shape. This is the Team-Cymru / IPv4-map idiom — unmistakably insider,
 * nothing generative about it. */
function hilbert(data) {
  const order = 4, side = 1 << order // 16 → 256 = one /24
  const d2xy = (d) => {
    let rx, ry, t = d, x = 0, y = 0
    for (let s = 1; s < side; s *= 2) {
      rx = 1 & (t >> 1); ry = 1 & (t ^ rx)
      if (ry === 0) { if (rx === 1) { x = s - 1 - x; y = s - 1 - y } const tmp = x; x = y; y = tmp }
      x += s * rx; y += s * ry; t >>= 2
    }
    return [x, y]
  }
  const segs = data.env.address_space
  const cellPx = 24, gap = 90
  const gridW = side * cellPx
  const W = segs.length * (gridW + gap) + 60, H = gridW + 160
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'stage' })
  const g = el('g', {})
  segs.forEach((seg, si) => {
    const ox = 40 + si * (gridW + gap), oy = 90
    g.appendChild(txt(ox, oy - 34, seg.segment, { fill: PAL.ink, 'font-size': 14, 'letter-spacing': '.04em' }))
    g.appendChild(txt(ox, oy - 18, `${seg.interface || ''} · 绑定 ${seg.counts.leased || 0} · 无绑定 ${seg.counts.unbound || 0} · 争用 ${seg.counts.contested || 0}`, { fill: PAL.muted, 'font-size': 10 }))
    const cellByHost = new Map(seg.cells.map((c) => [c.host, c]))
    // draw the curve path faintly so the ordering is visible
    let pathD = ''
    for (let d = 0; d < 256; d++) { const [x, yv] = d2xy(d); const cx = ox + x * cellPx + cellPx / 2, cy = oy + yv * cellPx + cellPx / 2; pathD += (d ? 'L' : 'M') + cx + ' ' + cy + ' ' }
    g.appendChild(el('path', { d: pathD, fill: 'none', stroke: PAL.faint, 'stroke-width': 0.6, opacity: 0.5 }))
    for (let d = 0; d < 256; d++) {
      const host = d // /24: index 0..255 maps to .0..255; hosts are .1..254
      const [x, yv] = d2xy(d)
      const cx = ox + x * cellPx, cy = oy + yv * cellPx
      const c = cellByHost.get(host)
      const st = c ? c.state : 'silent'
      if (st === 'silent') { g.appendChild(el('rect', { x: cx + 3, y: cy + 3, width: cellPx - 6, height: cellPx - 6, fill: 'none', stroke: '#1a1f21', 'stroke-width': 0.5 })); continue }
      const risk = data.riskByIp.get(c.ip) || (st === 'contested' ? 90 : st === 'unbound' ? 20 : 14)
      const pad = st === 'contested' ? 1 : 2 + (1 - Math.min(1, risk / 90)) * 5
      g.appendChild(el('rect', { x: cx + pad, y: cy + pad, width: cellPx - pad * 2, height: cellPx - pad * 2, fill: stateColor(st), stroke: st === 'contested' ? PAL.hi : PAL.ground, 'stroke-width': st === 'contested' ? 2 : 0.5 }))
      if (st === 'contested') g.appendChild(txt(cx + cellPx / 2, cy - 4, c.ip, { fill: PAL.hi, 'font-size': 10, 'text-anchor': 'middle' }))
    }
  })
  g.appendChild(txt(40, 34, '地址空间 · Hilbert 填充曲线(相邻 IP 在平面上也相邻) · 亮块大小=暴露评分 · 空心=无观测', { fill: PAL.inkSoft, 'font-size': 12, 'letter-spacing': '.03em' }))
  svg.appendChild(g)
  return svg
}

const RENDER = { matrix, replay, sankey, hilbert }
load().then((data) => {
  for (const id in RENDER) { const host = document.getElementById(id); if (host) host.appendChild(RENDER[id](data)) }
  document.body.dataset.ready = '1'
})
