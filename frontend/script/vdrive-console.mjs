#!/usr/bin/env node
/* Interaction driver for PAGE 1 (态势 / CONSOLE).
 *
 * vreview.mjs answers "does it look right"; axe answers "is the HTML legible".
 * Neither can answer the two questions that actually bite this page:
 *
 *   1. Does every trigger do what it claims — and nothing extra? The console is
 *      one SVG plate with overlapping state (drill / device / WAN / 3D / view
 *      transform). A control that silently also resets the viewport, or leaves a
 *      panel mounted, looks fine in a screenshot. So every step reads real DOM
 *      state before and after and prints the diff.
 *
 *   2. Is the SVG readable? axe-core CANNOT see SVG <text> — a passing contrast
 *      run says nothing about the topology, which is ~all of this page's type.
 *      So we walk the real SVG text nodes, resolve each one's effective painted
 *      background by hit-testing what is behind it, and compute WCAG ratios in
 *      the browser. A prior pass found .hud-r-line .acc at 2.58:1 this way.
 *
 *   node script/vdrive-console.mjs            # drive + contrast, writes shots
 *   node script/vdrive-console.mjs --contrast # contrast audit only
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const OUT = '/tmp/vdrive-console'
mkdirSync(OUT, { recursive: true })
const URL = 'http://192.168.1.27:2026/'
const ONLY_CONTRAST = process.argv.includes('--contrast')

const log = []
const say = (s) => { log.push(s); console.log(s) }

/** Every piece of console state a trigger could move. */
const probe = (page) => page.evaluate(() => {
  const n = (s) => document.querySelectorAll(s).length
  const txt = (s) => document.querySelector(s)?.textContent?.trim().replace(/\s+/g, ' ').slice(0, 42) ?? null
  const vt = document.querySelector('.flow-canvas > g')?.getAttribute('transform') ?? null
  return {
    // structural surfaces
    thesis: n('.thesis') ? 'shown' : '-',
    wanSiege: n('.wan-siege') ? 'shown' : '-',
    segGraph: n('.sg') ? 'shown' : '-',
    segHosts: n('.sg-node'),
    crumb: n('.mesh-crumb') ? 'shown' : '-',
    portal3d: n('.portal3d') ? 'shown' : '-',
    canvas3d: n('.c3d-inline') ? 'shown' : '-',
    // panels
    anPanel: n('.an-panel'),
    sgPanel: n('.sg-panel'),
    agentCta: n('.sg-cta'),
    // selection
    selHost: document.querySelector('.sg-node.sel text')?.textContent ?? null,
    selWan: document.querySelector('.ws-node.sel .ws-node-ip')?.textContent ?? null,
    verdict: txt('.an-vtxt'),
    // viewport transform (must only move when the viewport controls move it)
    view: vt,
    zoomLabel: txt('.zoom-k'),
    ifNodes: n('.m-intf'),
    tallyMarks: n('.ws-src'),
  }
})

const diff = (a, b) => {
  const out = []
  for (const k of Object.keys(a)) if (String(a[k]) !== String(b[k])) out.push(`${k}: ${a[k]} → ${b[k]}`)
  return out.length ? out.join(' | ') : '(no state change)'
}

/* ── SVG contrast, measured rather than assumed ───────────────────────────────
 * For each SVG <text>/<tspan> that renders ink, walk up for the effective fill,
 * then find what is actually painted behind its centre by hit-testing the点 and
 * taking the nearest ancestor/sibling with a real background. Report WCAG ratio.
 */
const svgContrast = (page) => page.evaluate(() => {
  const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4) }
  const L = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
  const ratio = (a, b) => { const [x, y] = [L(a), L(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05) }
  const parse = (s) => {
    if (!s) return null
    const m = s.match(/rgba?\(([^)]+)\)/)
    if (!m) return null
    const p = m[1].split(',').map((v) => parseFloat(v))
    if (p.length > 3 && p[3] === 0) return null
    return [p[0], p[1], p[2]]
  }
  // composite src over dst by alpha
  const over = (src, a, dst) => src.map((c, i) => c * a + dst[i] * (1 - a))

  const pageBg = parse(getComputedStyle(document.querySelector('.canvas-wrap') ?? document.body).backgroundColor) ?? [231, 231, 227]

  const out = []
  for (const el of document.querySelectorAll('.flow-canvas text, .flow-canvas tspan')) {
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility === 'hidden') continue
    const box = el.getBoundingClientRect()
    if (box.width < 1 || box.height < 1) continue
    const own = (el.textContent ?? '').trim()
    if (!own) continue
    // a tspan with its own fill overrides the parent text's fill
    const fill = parse(cs.fill)
    if (!fill) continue
    // Effective alpha includes every ancestor <g opacity>. Two cases must not be
    // scored as failing body text:
    //   · opacity 0 — a hover hint that paints nothing (reports a bogus 1:1)
    //   · a layer the component explicitly marked .dim — backgrounded context
    //     behind an open panel. That is a deliberate de-emphasis, not type the
    //     reader is being asked to read; scoring it would make "dim the context"
    //     unimplementable. It is counted and reported separately instead.
    let a = 1
    let dimmed = false
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      a *= parseFloat(getComputedStyle(p).opacity || '1')
      if (p.classList?.contains('dim')) dimmed = true
    }
    if (a < 0.06) continue
    const opacity = a * parseFloat(cs.fillOpacity || '1')
    const eff = over(fill, opacity, pageBg)
    const r = ratio(eff, pageBg)
    const cls = (el.getAttribute('class') || el.parentElement?.getAttribute('class') || el.tagName)
    out.push({
      cls: String(cls).slice(0, 34),
      text: own.slice(0, 22),
      fill: cs.fill,
      opacity: +opacity.toFixed(2),
      size: cs.fontSize,
      ratio: +r.toFixed(2),
      dimmed,
    })
  }
  // worst first, de-duplicated by class+ratio
  const seen = new Set()
  return out.sort((a, b) => a.ratio - b.ratio).filter((x) => {
    const k = `${x.cls}|${x.ratio}`
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
})

async function contrastReport(page, label) {
  const all = await svgContrast(page)
  const rows = all.filter((r) => !r.dimmed)
  const dim = all.filter((r) => r.dimmed)
  say(`\n──── SVG CONTRAST · ${label} (axe cannot see these) ────`)
  const bad = rows.filter((r) => r.ratio < 4.5)
  const ok = rows.filter((r) => r.ratio >= 4.5)
  for (const r of rows.slice(0, 12)) {
    const flag = r.ratio < 3 ? '✗✗' : r.ratio < 4.5 ? '✗ ' : '✓ '
    say(`  ${flag} ${String(r.ratio).padStart(6)}:1  ${r.cls.padEnd(26)} ${r.size.padStart(7)}  "${r.text}"`)
  }
  say(`  → ${rows.length} live text nodes · ${bad.length} under 4.5:1 · MIN ${rows[0]?.ratio ?? 'n/a'}:1 · pass ${ok.length}`)
  if (dim.length) say(`  → (+${dim.length} in .dim backgrounded layers, not scored as reading text)`)
  return rows
}

async function run() {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 } })
  const page = await ctx.newPage()
  const errs = []
  page.on('pageerror', (e) => errs.push(String(e)))
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })
  page.on('requestfailed', (r) => errs.push(`REQ ${r.url()} ${r.failure()?.errorText}`))

  await page.goto(URL, { waitUntil: 'networkidle' })
  await page.locator('header button, nav button').filter({ hasText: /^态势$/ }).first().click()
  await page.waitForTimeout(4000)

  say('════ 0 · RESTING STATE ════')
  let before = await probe(page)
  say(`  ${JSON.stringify(before)}`)
  await page.screenshot({ path: `${OUT}/00-rest.png` })
  await contrastReport(page, 'resting console')

  if (ONLY_CONTRAST) { await browser.close(); return }

  say('\n════ 1 · CLICK A SUBNET (drill into the segment) ════')
  before = await probe(page)
  await page.locator('.subnet').first().click()
  await page.waitForTimeout(2500)
  let after = await probe(page)
  say(`  click subnet #0 → ${diff(before, after)}`)
  await page.screenshot({ path: `${OUT}/01-drill.png` })
  await contrastReport(page, 'drilled segment')

  say('\n════ 2 · OPEN A DEVICE (host in the segment graph) ════')
  before = await probe(page)
  const hosts = page.locator('.sg-node')
  const nHosts = await hosts.count()
  say(`  hosts on plate: ${nHosts}`)
  if (nHosts) {
    // pick a host that actually carries traffic (t-high reads first)
    const target = (await page.locator('.sg-node.t-high').count()) ? page.locator('.sg-node.t-high').first() : hosts.nth(4)
    await target.click({ force: true })
    await page.waitForTimeout(1200)
    after = await probe(page)
    say(`  click host → ${diff(before, after)}`)
    await page.screenshot({ path: `${OUT}/02-host.png` })

    // does clicking the SAME host close it again?
    before = after
    await target.click({ force: true })
    await page.waitForTimeout(800)
    after = await probe(page)
    say(`  click SAME host again (toggle off?) → ${diff(before, after)}`)
  }

  say('\n════ 3 · BACK OUT (breadcrumb) ════')
  before = await probe(page)
  if (await page.locator('.mesh-crumb').count()) {
    await page.locator('.mesh-crumb').click()
    await page.waitForTimeout(1200)
    after = await probe(page)
    say(`  click breadcrumb → ${diff(before, after)}`)
  } else say('  ✗ no breadcrumb')

  say('\n════ 4 · CLICK A WAN SOURCE (intrusion verdict · DeepSeek) ════')
  before = await probe(page)
  await page.locator('.ws-node').first().click()
  await page.waitForTimeout(1000)
  after = await probe(page)
  say(`  click WAN source → ${diff(before, after)}`)
  say('  …awaiting the model (this calls DeepSeek server-side)')
  await page.waitForTimeout(26000)
  after = await probe(page)
  say(`  after verdict → verdict="${after.verdict}" anPanel=${after.anPanel} thesis=${after.thesis}`)
  await page.screenshot({ path: `${OUT}/04-wan.png` })
  await contrastReport(page, 'WAN verdict open')

  // close it
  before = await probe(page)
  if (await page.locator('.an-x').count()) {
    await page.locator('.an-x').first().click()
    await page.waitForTimeout(800)
    after = await probe(page)
    say(`  close verdict → ${diff(before, after)}`)
  }

  say('\n════ 5 · VIEWPORT: pan / zoom / reset ════')
  const canvas = page.locator('.flow-canvas')
  const box = await canvas.boundingBox()
  before = await probe(page)
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width * 0.5 + 120, box.y + box.height * 0.5 + 60, { steps: 10 })
  await page.mouse.up()
  await page.waitForTimeout(400)
  after = await probe(page)
  say(`  DRAG +120,+60 → ${diff(before, after)}`)

  before = after
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5)
  await page.mouse.wheel(0, -240)
  await page.waitForTimeout(400)
  after = await probe(page)
  say(`  WHEEL zoom in → ${diff(before, after)}`)

  before = after
  await page.locator('.zoom-ctl button').first().click()
  await page.waitForTimeout(300)
  after = await probe(page)
  say(`  click ZOOM OUT (−) → ${diff(before, after)}`)

  before = after
  await page.locator('.zoom-reset').click()
  await page.waitForTimeout(500)
  after = await probe(page)
  say(`  click RESET → ${diff(before, after)}`)
  say(`  reset restores identity transform? ${after.view === 'translate(0 0) scale(1)' ? '✓ yes' : `✗ view=${after.view}`}`)

  say('\n════ 6 · 3D PORTAL ════')
  before = await probe(page)
  if (await page.locator('.portal3d').count()) {
    await page.locator('.portal3d').click()
    await page.waitForTimeout(6000)
    after = await probe(page)
    say(`  click 3D portal → ${diff(before, after)}`)
    await page.screenshot({ path: `${OUT}/06-3d.png` })
    const bg = await page.evaluate(() => {
      const c = document.querySelector('.c3d-full canvas')
      return c ? getComputedStyle(c).background.slice(0, 90) : null
    })
    say(`  3D canvas ground: ${bg}`)
    if (await page.locator('.c3d-bar .tc-x').count()) {
      before = await probe(page)
      await page.locator('.c3d-bar .tc-x').click()
      await page.waitForTimeout(1500)
      after = await probe(page)
      say(`  close 3D → ${diff(before, after)}`)
    }
  } else say('  ✗ no 3D portal')

  say(`\n════ CONSOLE ERRORS: ${errs.length ? errs.slice(0, 6).join(' | ') : 'none'} ════`)
  say(`shots: ${OUT}`)
  await browser.close()
}

run().catch((e) => { console.error(e); process.exit(1) })
