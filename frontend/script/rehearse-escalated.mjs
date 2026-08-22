/* Prove the refusal is visible — in a real browser, on the real console.
 *
 *   node frontend/script/rehearse-escalated.mjs
 *
 * The `escalated` branch cannot be reached by waiting for it: it needs the same
 * fault fixed and broken again three times inside the window. So this feeds the
 * console a recorded chain — the timeline events exactly as core/remediate
 * writes them, per docs/recurrence-contract.md — and projects the card with the
 * real sentinel_projection. Everything downstream of the event is shipping code.
 *
 * What it checks, in the order a person meets it:
 *   1. the strip puts 不再自动修 at the top, ahead of everything still moving
 *   2. the card reads escalated: gated, blocked, P1
 *   3. the theater marks the incident as harder than an ordinary one
 *   4. the citation chain is there, complete, and inside the card
 *   5. nothing on the stage claims to still be working on it
 *   6. the transcript carries the divider
 *   7. English says the same things
 *   8. someone who asked for no motion gets none
 *
 * Step 4 is the reason this exists. "凭什么第三次就不修了" has to be answerable
 * on screen; a card that clips its last cycle off the bottom answers nothing.
 *
 * Nothing is written to the live timeline and no service is touched — the two
 * sentinel endpoints are answered in the browser.
 */
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..', '..')
const SHOTS = resolve(HERE, '..', '.rehearsal')
const BASE = process.env.AUTOPOIESIS_URL ?? 'http://127.0.0.1:2026/'
const SUBJECT = 'demo-collector.service'
const VIEW = { width: 1900, height: 1050 }
const REASON = '同一处置在 24 小时内已经生效过 3 次又复发。重启治不好它——反复被弄坏说明另有原因，转人工。'

mkdirSync(SHOTS, { recursive: true })
const log = (...a) => console.log(...a)
const die = (m) => { console.error(`\n✗ ${m}`); process.exitCode = 1; throw new Error(m) }

/* ── the recorded chain ───────────────────────────────────────────────────
 * Timestamped from now, because both the strip (30 min) and the projection
 * (6 h) drop anything older — a fixture pinned to a date would simply vanish. */
const iso = (secAgo) => new Date(Date.now() - secAgo * 1000).toISOString().replace('Z', '+00:00')
const FIXED_AT = [1800, 1200, 600]

const events = []
for (const ago of FIXED_AT) {
  events.push({ kind: 'detected', at: iso(ago + 40), subject: SUBJECT, severity: 'high',
    detector: 'failed_units', action: 'restart_unit', family: 'fam-perception-selfheal',
    summary: `${SUBJECT} 挂了。`, evidence: { line: `${SUBJECT} loaded failed` }, streak: 2 })
  events.push({ kind: 'preflight', at: iso(ago + 35), subject: SUBJECT, action: 'restart_unit',
    eligible: true, reason: 'unit is failed',
    blast_radius: { scope: 'single-service', summary: `只影响 ${SUBJECT}，无其他单元依赖它。` } })
  events.push({ kind: 'command', at: iso(ago + 34), subject: SUBJECT,
    argv: ['systemctl', 'restart', SUBJECT], rc: 0, out: '' })
  events.push({ kind: 'remediated', at: iso(ago + 2), subject: SUBJECT, action: 'restart_unit',
    outcome: 'passed', needs_human: false, samples: 12, detail: 'no probe regressed' })
  events.push({ kind: 'resolved', at: iso(ago), subject: SUBJECT, detector: 'failed_units',
    action: 'restart_unit', outcome: 'passed', samples: 12, note: '回读通过' })
}
const stillBroken = (ago) => ({ kind: 'detected', at: iso(ago), subject: SUBJECT, severity: 'high',
  detector: 'failed_units', action: 'restart_unit', family: 'fam-perception-selfheal',
  summary: `${SUBJECT} 又挂了。`, evidence: { line: `${SUBJECT} loaded failed` }, streak: 2 })
events.push(stillBroken(80))
events.push({ kind: 'escalated', at: iso(75), subject: SUBJECT, detector: 'failed_units',
  action: 'restart_unit', recurrences: 3, window_hours: 24,
  prior_cycles: FIXED_AT.map((ago) => ({ at: iso(ago), outcome: 'passed', samples: 12 })),
  reason: REASON })
// The refusal is announced once per key and the unit stays down, so the detector
// keeps firing every poll. The decision scrolls off the end of the chain — every
// surface has to keep reading it anyway.
events.push(stillBroken(55), stillBroken(35), stillBroken(15))

/* ── the cards, from the real projection ─────────────────────────────────── */
const scratch = mkdtempSync(join(tmpdir(), 'rehearse-escalated-'))
let recorded = 0
/** Write a chain to disk and project it exactly as the gateway would. */
const record = (chainEvents) => {
  const path = join(scratch, `timeline-${recorded++}.jsonl`)
  writeFileSync(path, `${chainEvents.map((e) => JSON.stringify(e)).join('\n')}\n`, 'utf8')
  const project = (lang) => JSON.parse(execFileSync('python3', ['-c',
    'import json,sys;from frontend.gateway.app.sentinel_projection import sentinel_cards;'
    + 'print(json.dumps(sentinel_cards(sys.argv[1]), ensure_ascii=False))', lang],
  { cwd: REPO, encoding: 'utf8', env: { ...process.env, AUTOPOIESIS_SENTINEL_TIMELINE: path } }))
  const cards = { zh: project('zh'), en: project('en') }
  if (!cards.zh.length) die('the projection produced no card for a recorded chain')
  return { events: chainEvents, cards }
}

const REFUSED = record(events)
// The same subject, stopped one round earlier: the ordinary card, to prove the
// escalation branch did not move the floor under everything else.
const HEALED = record(events.slice(0, FIXED_AT.length * 5))
log(`0. 投影出卡片  ${REFUSED.cards.zh[0].reviewVerdict.verdictStatus} · ${REFUSED.cards.zh[0].priority} · `
  + `${REFUSED.cards.zh[0].recurrences} 次复发 · ${REFUSED.cards.zh[0].priorCycles.length} 条引用`
  + `  |  对照组 ${HEALED.cards.zh[0].reviewVerdict.verdictStatus} · ${HEALED.cards.zh[0].priority}`)

const browser = await chromium.launch()

/** A page that sees the recorded chain instead of whatever the host is doing. */
const staged = async (opts = {}, chain = REFUSED) => {
  const page = await browser.newPage({ viewport: VIEW, ...opts })
  await page.route('**/api/rca/sentinel/timeline*', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ ok: true, events: chain.events, count: chain.events.length }),
  }))
  await page.route('**/api/rca/live-situation*', async (route) => {
    const snapshot = await (await route.fetch()).json()
    const lang = new URL(route.request().url()).searchParams.get('lang') === 'en' ? 'en' : 'zh'
    const mine = chain.cards[lang]
    snapshot.suggestions = [...mine, ...(snapshot.suggestions ?? []).filter((s) => s.scope !== 'sentinel')]
    snapshot.feed = [
      ...mine.map((c) => ({
        id: `feed-suggestion-${c.id}`, kind: 'suggestion', scope: c.scope, ts: c.ts,
        severity: c.severity, priority: c.priority, device: c.device, deviceKey: c.deviceKey,
        summary: c.summary,
      })),
      ...(snapshot.feed ?? []).filter((f) => f.scope !== 'sentinel'),
    ]
    snapshot.ready = true
    snapshot.defaultSuggestionId = mine[0].id
    await route.fulfill({ json: snapshot })
  })
  return page
}

/** situational strip → card → theater, the path the operator walks.
 *
 * The language lives in React state, not storage, so a reload puts it back to
 * 中 — it has to be switched after the page loads and before the theater opens,
 * where the nav is no longer reachable. */
const openTheater = async (page, { english = false } = {}) => {
  await page.goto(BASE, { waitUntil: 'networkidle' })
  if (english) {
    for (let i = 0; i < 8; i++) {
      if (await page.locator('nav button, header button').filter({ hasText: /^TRAJECTORY$/ }).count()) break
      const btn = page.locator('header button, nav button').filter({ hasText: /^EN$/ })
      if (await btn.count()) await btn.first().click().catch(() => {})
      await page.waitForTimeout(250)
    }
  }
  const row = page.locator('.la-row').filter({ hasText: SUBJECT }).first()
  await row.waitFor({ timeout: 60_000 }).catch(() => die('no alert row for the recorded chain'))
  await row.click()
  await page.waitForSelector('.ls:not(.ls-msg)', { timeout: 60_000 })
    .catch(() => die('the 实时态势 panel never left its loading state'))
  const door = page.locator('text=/全链路拓扑剧场|TOPOLOGY THEATER/i').first()
  if (!(await door.count())) die('no theater door on the card')
  await door.click()
  await page.waitForSelector('.th-incident', { timeout: 30_000 })
    .catch(() => die('the theater drew no incident marker'))
  await page.waitForTimeout(2500)
}

/** Does the whole card fit its box, and the box fit the plate?
 *
 * offsetHeight is in the foreignObject's own units, so it compares directly with
 * the height attribute — that is the clip. The client rect is real screen pixels
 * after the plate is scaled — that is the fold. */
const measure = async (page) => {
  const fit = await page.locator('.th-incident foreignObject').first().evaluate((fo) => {
    const card = fo.querySelector('.th-inc-card')
    return { budget: Number(fo.getAttribute('height')), needs: card.offsetHeight, box: card.getBoundingClientRect() }
  })
  if (fit.needs > fit.budget) {
    die(`the card needs ${fit.needs}px inside a ${fit.budget}px box — the bottom of the citation chain is clipped`)
  }
  if (fit.box.y + fit.box.height > VIEW.height || fit.box.x + fit.box.width > VIEW.width || fit.box.x < 0) {
    die(`the card runs off the plate at ${VIEW.width}x${VIEW.height}: `
      + `${Math.round(fit.box.x)},${Math.round(fit.box.y)} ${Math.round(fit.box.width)}x${Math.round(fit.box.height)}`)
  }
  return fit
}

const page = await staged()
const errors = []
page.on('pageerror', (e) => errors.push(`PAGEERROR ${e.message}`))
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })

try {
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await page.waitForSelector('.la-row', { timeout: 60_000 })

  // ── 1. the worst thing on the page has to be the first thing on the page ──
  const phases = await page.locator('.la-row').evaluateAll(
    (rows) => rows.map((r) => `${[...r.classList].find((c) => c.startsWith('p-'))}:${r.querySelector('.la-phase').textContent}`),
  )
  if (!phases[0]?.startsWith('p-escalated:')) {
    die(`the escalated row is not first — rows read ${phases.join(', ')}`)
  }
  // 不再自动修, not 要人工 — needs_human means a revert could not be verified,
  // which is a different thing from the system deciding to stop repairing.
  if (!phases[0].endsWith('不再自动修')) die(`wrong label on the escalated row: ${phases[0]}`)
  const headline = await page.locator('.la-count').innerText()
  if (!/要人工/.test(headline)) die(`the strip headline hides the refusal: "${headline}"`)
  log(`1. 告警条把它排在最前        ${phases[0]} · 抬头「${headline}」`)
  await page.screenshot({ path: `${SHOTS}/e1-strip.png` })

  // ── 2. the card the click lands on has to agree ───────────────────────────
  await page.locator('.la-row.p-escalated').first().click()
  await page.waitForSelector('.ls:not(.ls-msg)', { timeout: 60_000 })
  const stages = await page.locator('.ls-st-id').allInnerTexts()
  if (!stages.includes('escalated')) die(`no escalated stage in the telemetry: ${stages.join(', ')}`)
  const gate = await page.locator('.ls-gate-risk').innerText().catch(() => '')
  if (!/gated/i.test(gate)) die(`the card does not read as gated: "${gate}"`)
  log(`2. 卡片口径一致              阶段 ${stages.join(' / ')} · ${gate.replace(/\s+/g, ' ')}`)
  await page.screenshot({ path: `${SHOTS}/e2-card.png`, fullPage: true })

  // ── 3-5. the stage ────────────────────────────────────────────────────────
  await openTheater(page)
  if (!(await page.locator('.th-incident.is-escalated').count())) {
    die('the incident marker does not read as escalated')
  }
  const header = await page.locator('.th-inc-k').innerText()
  if (header !== '已升级 · 需要人工') die(`wrong card header: "${header}"`)

  const cite = await page.locator('.th-inc-cite-i').allInnerTexts()
  if (cite.length !== FIXED_AT.length) {
    die(`the citation chain shows ${cite.length} of ${FIXED_AT.length} rounds`)
  }
  for (const line of cite) {
    if (!/^修好于 \d{2}:\d{2}:\d{2} → 又复发$/.test(line)) die(`malformed citation line: "${line}"`)
  }
  const recur = (await page.locator('.th-inc-row').allInnerTexts()).find((r) => /^复发/.test(r))
  if (!/^复发\s*3 次 \/ 24 小时$/.test(recur?.replace(/\s+/g, ' ') ?? '')) {
    die(`the recurrence row does not read right: "${recur}"`)
  }
  log(`3. 事故卡说清楚了            ${header} · ${recur?.replace(/\s+/g, ' ')}`)
  cite.forEach((c) => log(`     ${c}`))

  // ── 4. and all of it has to actually be on the screen ─────────────────────
  const fit = await measure(page)
  log(`4. 卡片完整在屏内            用 ${fit.needs}/${fit.budget}px，`
    + `落在 ${Math.round(fit.box.y)}–${Math.round(fit.box.y + fit.box.height)} / ${VIEW.height}`)

  // ── 5. nothing may claim the system is still on it ────────────────────────
  const nowMark = await page.locator('.th-stage-now').count()
  if (nowMark) die(`${nowMark} rail stages still say 正在进行 on a chain that stopped`)
  const terminal = await page.locator('.rp-terminal').innerText()
  if (!/转人工|ESCALATED/.test(terminal)) die(`the progress rail's verdict reads "${terminal}"`)
  if (await page.locator('.rp-phase.is-now').count()) die('the progress rail still marks a phase as running')
  // The earlier rounds of this same fault DID reach preflight, act and watch.
  // They are in the same timeline, and lighting them here would claim this
  // detection got the repair it was refused.
  // textContent, not innerText: these are SVG <text> nodes and innerText is empty on them
  const hot = await page.locator('.th-stage.hot .th-stage-l').evaluateAll((n) => n.map((e) => e.textContent))
  if (hot.join() !== ['巡检发现', '二次确认'].join()) {
    die(`the theater rail lights ${hot.join(' → ')} — an escalated round never got past 二次确认`)
  }
  const done = await page.locator('.rp-phase.is-done .rp-lab').allInnerTexts()
  if (done.join() !== ['发现', '已确认', '收尾'].join()) {
    die(`the progress rail lights ${done.join(' → ')} on a round that was refused`)
  }
  log(`5. 没有任何地方说还在处置    终态「${terminal}」· 轨道 ${hot.join('→')} · 进度 ${done.join('→')}`)

  // ── 6. the transcript's divider ───────────────────────────────────────────
  const marks = await page.locator('.xl-mark').allInnerTexts()
  if (!marks.includes('不再自动修，转人工')) die(`no escalation divider in the transcript: ${marks.join(' / ')}`)
  log(`6. 执行记录有分隔线          ${marks.join(' → ')}`)
  await page.screenshot({ path: `${SHOTS}/e3-theater.png` })
  await page.locator('.th-inc-card').screenshot({ path: `${SHOTS}/e3b-incident-card.png` })

  if (errors.length) die(`console errors: ${errors.slice(0, 4).join(' / ')}`)

  // ── 7. English says the same things ───────────────────────────────────────
  const en = await staged()
  await openTheater(en, { english: true })
  const enHeader = await en.locator('.th-inc-k').innerText()
  if (enHeader !== 'ESCALATED — NEEDS A PERSON') die(`wrong EN card header: "${enHeader}"`)
  const enCite = await en.locator('.th-inc-cite-i').allInnerTexts()
  if (enCite.length !== FIXED_AT.length) die(`EN citation chain shows ${enCite.length} rounds`)
  // Only what escalation added is graded here. 影响面 is a Chinese sentence the
  // sentinel recorded itself — a pre-existing gap in the timeline's own strings,
  // and not something an English label in this component can fix.
  const enBlock = [enHeader, ...enCite,
    ...(await en.locator('.th-inc-row').allInnerTexts()).filter((r) => /^RECURRED/.test(r))].join(' | ')
  if (/[一-鿿]/.test(enBlock)) die(`Chinese left in the English escalation block: ${enBlock}`)
  const leaked = (await en.locator('.th-inc-card').innerText()).match(/[一-鿿][^|\n]*/g)
  // English is the longer copy — the header alone wraps to two lines — so the
  // budget has to be checked again rather than assumed from the Chinese pass.
  const enFit = await measure(en)
  log(`7. 英文一致                  ${enHeader} · ${enCite[0]} · 用 ${enFit.needs}/${enFit.budget}px`)
  if (leaked) log(`     （既有问题，不是这次引入：卡片里仍有后端写死的中文 ${leaked[0].slice(0, 24)}…）`)
  await en.screenshot({ path: `${SHOTS}/e4-theater-en.png` })
  await en.close()

  // ── 8. and none of it is forced on someone who asked for stillness ────────
  const still = await staged({ reducedMotion: 'reduce' })
  await openTheater(still)
  const moving = await still.evaluate(() => [...document.querySelectorAll('.theater *')]
    .filter((el) => getComputedStyle(el).animationName !== 'none').length)
  const smil = await still.locator('.theater animateMotion').count()
  if (moving || smil) die(`prefers-reduced-motion ignored: ${moving} css + ${smil} smil`)
  if (!(await still.locator('.th-inc-cite-i').count())) die('the citation chain vanished under reduced motion')
  await still.close()
  log('8. 关掉动效后完全静止        0 css + 0 smil，引用链照旧')

  // ── 9. and the ordinary incident has to be exactly where it was ───────────
  const plain = await staged({}, HEALED)
  await openTheater(plain)
  if (await plain.locator('.th-incident.is-escalated').count()) die('a healed chain is being drawn as escalated')
  if (await plain.locator('.th-inc-cite').count()) die('a citation chain on a card that never escalated')
  const plainRows = await plain.locator('.th-inc-row').allInnerTexts()
  if (!plainRows.some((r) => /^当前/.test(r))) die('the ordinary card lost its 当前 row')
  if (!plainRows.some((r) => /^影响面/.test(r))) die('the ordinary card lost its 影响面 row')
  const plainHot = await plain.locator('.th-stage.hot').count()
  if (plainHot !== 6) die(`a healed chain lights ${plainHot}/6 rail stages`)
  await measure(plain)
  await plain.close()
  log(`9. 普通事故卡没被动过        6/6 环节 · ${plainRows.map((r) => r.split(/\s+/)[0]).join(' / ')}`)

  log(`\n✓ 拒绝执行这件事在真浏览器里从告警条一路可见到引用链。截图在 ${SHOTS}/`)
} finally {
  await browser.close()
}
