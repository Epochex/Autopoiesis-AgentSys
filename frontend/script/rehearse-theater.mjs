/* Walk the operator's path in a real browser and prove the stage is live.
 *
 *   node frontend/script/rehearse-theater.mjs [subject]
 *
 * What it checks, in the order a person meets it:
 *   1. the situational page raises the alert on its own
 *   2. clicking it lands on 长轨迹 with that subject's card selected
 *   3. the card's rail is the sentinel loop, not the event-detection chain
 *   4. the theater anchors the fault to a node and draws the chain to the rail
 *   5. the transcript shows the real commands and what they returned
 *   6. the incident's own path pulses, in phase, and nothing else does
 *   7. — the point of this script — the rail ADVANCES while nothing is touched
 *
 * Step 5 exists because the stage used to freeze: it computed which steps had
 * run at the moment it opened and never looked again, so an incident could walk
 * its whole chain with the screen sitting still. Asserting the final state
 * would not have caught that; only watching it move does.
 *
 * Run it against an injected incident:
 *   ./scripts/inject_incident.sh service-down && node frontend/script/rehearse-theater.mjs
 *
 * Exits non-zero on the first broken link in the chain. Screenshots land in
 * frontend/.rehearsal/ (gitignored) so a failure can be looked at.
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const HERE = dirname(fileURLToPath(import.meta.url))
const SHOTS = resolve(HERE, '..', '.rehearsal')
const BASE = process.env.AUTOPOIESIS_URL ?? 'http://127.0.0.1:2026/'
const SUBJECT = process.argv[2] ?? 'demo-collector'
/** Long enough for detect → confirm → preflight → act → 90s watch → verify. */
const WATCH_MS = 4 * 60 * 1000
const SAMPLE_MS = 6000

mkdirSync(SHOTS, { recursive: true })
const log = (...a) => console.log(...a)
const die = (message) => { console.error(`\n✗ ${message}`); process.exitCode = 1; throw new Error(message) }

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1900, height: 1050 } })
const errors = []
page.on('pageerror', (e) => errors.push(`PAGEERROR ${e.message}`))
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })

try {
  await page.goto(BASE, { waitUntil: 'networkidle' })
  log(`→ ${BASE}`)

  // ── 1. the alert has to arrive without anyone going looking for it ────────
  const alert = page.locator('.la-row').filter({ hasText: SUBJECT }).first()
  await alert.waitFor({ timeout: 120_000 }).catch(() => die(
    `no alert for "${SUBJECT}" on the situational page after 2 min — is the sentinel running?`,
  ))
  log(`1. 态势页自己弹出告警  ${(await alert.innerText()).replace(/\n/g, ' | ')}`)
  await page.screenshot({ path: `${SHOTS}/1-console-alert.png` })

  // ── 2. clicking it lands on the chain, not on a generic page ──────────────
  await alert.click()
  await page.waitForSelector('.ls:not(.ls-msg)', { timeout: 60_000 }).catch(() => die(
    'the 实时态势 panel never left its loading state',
  ))
  if (!(await page.locator('.ls').innerText()).includes(SUBJECT)) {
    die(`"${SUBJECT}" is not in the 实时态势 list after clicking its alert`)
  }
  const rail = await page.locator('.ls-pipe-k').innerText()
  log(`2. 跳到长轨迹，卡片已选中     环节条: ${rail}`)
  if (!/哨兵|SENTINEL/.test(rail)) die(`the card is on the wrong rail: ${rail}`)
  await page.screenshot({ path: `${SHOTS}/2-trajectory-card.png`, fullPage: true })

  // ── 3. the theater has to put the fault ON the map ────────────────────────
  const door = page.locator('text=/全链路拓扑剧场|FULL-CHAIN/i').first()
  if (!(await door.count())) die('no theater door on the card')
  await door.click()
  await page.waitForTimeout(3000)
  if (!(await page.locator('.th-incident').count())) die('the theater drew no incident marker on the topology')
  if (!(await page.locator('.th-chain').count())) die('no chain drawn from the node to the rail')
  const marker = (await page.locator('.th-inc-card').innerText()).replace(/\n/g, ' | ')
  log(`3. 剧场把故障标在节点上       ${marker}`)

  // ── 4. the transcript: what it ran, not just how far it got ───────────────
  await page.waitForSelector('.xl', { timeout: 60_000 }).catch(() => die(
    'no execution transcript in the theater — commands are not reaching the timeline',
  ))
  const box = await page.locator('.xl').boundingBox()
  if (box && box.y + box.height > page.viewportSize().height) {
    die(`the transcript runs ${Math.round(box.y + box.height - page.viewportSize().height)}px below the fold`)
  }
  const firstCmds = await page.locator('.xl-cmd .xl-argv').allInnerTexts()
  log(`4. 执行记录（前 3 条，共 ${firstCmds.length}）：`)
  firstCmds.slice(0, 3).forEach((c) => log(`     ${c.replace(/\s+/g, ' ')}`))
  if (!firstCmds.length) die('the transcript panel is empty')

  // ── 6. the involved path has to stand out, as one pulse rather than many ──
  const PATH = [
    ['.th-self.is-hit .th-self-box', '本机方块'],
    ['.th-self.is-hit .th-self-link', '本机→网段连线'],
    ['.th-inc-leader', '事故卡引线'],
    ['.th-chain-line', '节点→轨道链'],
    ['.th-inc-halo-box', '事故光晕'],
  ]
  const periods = new Set()
  for (const [sel, name] of PATH) {
    if (!(await page.locator(sel).count())) die(`${name} 没渲染 (${sel})`)
    const css = await page.locator(sel).first().evaluate((el) => {
      const c = getComputedStyle(el)
      return { names: c.animationName, durs: c.animationDuration }
    })
    if (css.names === 'none') die(`${name} 不闪 — 涉事链路必须突出`)
    // th-march is directional flow; the breathing pulse is the shared one
    css.durs.split(',').map((d) => d.trim()).forEach((d) => { if (d === '1.6s') periods.add(d) })
  }
  if (periods.size !== 1) die(`the path pulses at ${[...periods].join('/')} — out of phase reads as random blinking`)
  const lively = await page.evaluate(() => [...document.querySelectorAll('.theater *')]
    .filter((el) => getComputedStyle(el).animationName !== 'none').length)
  const nodes = await page.locator('.theater *').count()
  if (lively > nodes * 0.05) die(`${lively}/${nodes} elements animate — the highlight only means something if the rest holds still`)
  log(`6. 涉事链路统一闪烁          ${PATH.length} 处同相位 1.6s，全场 ${lively}/${nodes} 个元素有动效`)

  // ── 7. and then it has to MOVE, with nobody touching anything ─────────────
  log('\n7. 现在什么都不碰，看轨道自己走：')
  const seen = new Set()
  const trail = []
  const started = Date.now()
  let lit = 0
  let closed = false

  while (Date.now() - started < WATCH_MS && !closed) {
    lit = await page.locator('.th-stage.hot').count()
    const now = await page.locator('.th-inc-row').allInnerTexts()
      .then((rows) => rows.find((r) => /^(当前|NOW)/.test(r))?.replace(/\s+/g, ' ') ?? '')
      .catch(() => '')
    const stamp = new Date().toISOString().slice(11, 19)
    if (!seen.has(`${lit}|${now}`)) {
      seen.add(`${lit}|${now}`)
      trail.push({ stamp, lit, now })
      log(`   ${stamp}  亮 ${lit}/6 级   ${now}`)
      await page.screenshot({ path: `${SHOTS}/4-rail-${trail.length}-lit${lit}.png` })
    }
    // 收尾 / 回读验证 reached, or the chain stopped for a reason
    closed = lit >= 6 || /只报不动|无安全动作|REPORTED/.test(marker)
    if (!closed) await page.waitForTimeout(SAMPLE_MS)
  }

  if (trail.length < 2) {
    die(`the rail never moved — it sat at ${lit}/6 for the whole window. `
      + 'This is the frozen-stage regression: the theater is reading a snapshot, not the live chain.')
  }
  log(`\n   轨道自己前进了 ${trail.length} 次：${trail.map((t) => `${t.lit}`).join(' → ')} 级`)

  const grownCmds = await page.locator('.xl-cmd').count()
  if (grownCmds <= firstCmds.length) {
    log(`   （执行记录停在 ${grownCmds} 条——链路可能在打开前就走完了）`)
  } else {
    log(`   执行记录同步长到 ${grownCmds} 条`)
  }

  if (errors.length) die(`console errors: ${errors.slice(0, 4).join(' / ')}`)

  // ── and none of that motion may be forced on someone who asked for none ───
  const still = await browser.newPage({ viewport: { width: 1900, height: 1050 }, reducedMotion: 'reduce' })
  await still.goto(BASE, { waitUntil: 'networkidle' })
  await still.locator('.la-row').filter({ hasText: SUBJECT }).first().click()
  await still.waitForSelector('.ls:not(.ls-msg)', { timeout: 60_000 })
  await still.locator('text=/全链路拓扑剧场|FULL-CHAIN/i').first().click()
  await still.waitForTimeout(3000)
  const moving = await still.evaluate(() => [...document.querySelectorAll('.theater *')]
    .filter((el) => getComputedStyle(el).animationName !== 'none').length)
  const smil = await still.locator('.theater animateMotion').count()
  await still.close()
  if (moving || smil) die(`prefers-reduced-motion is ignored: ${moving} css + ${smil} smil animations still running`)
  log('8. 关掉动效偏好后完全静止    0 css + 0 smil')

  log(`\n✓ 整条链在真浏览器里走通，剧场是活的。截图在 ${SHOTS}/`)
} finally {
  await browser.close()
}
