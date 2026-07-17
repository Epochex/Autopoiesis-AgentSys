#!/usr/bin/env node
/* Interaction driver — use the console the way a presenter would, and record
 * what each action actually did.
 *
 * The visual harness (vreview.mjs) answers "does it look right". This answers
 * "does it respond right": every step reads real DOM state before and after, so
 * a control that does nothing, or does something extra, shows up as a diff
 * rather than as a screenshot that looks fine.
 *
 *   node script/vdrive.mjs                # full script, writes shots + a log
 *   node script/vdrive.mjs --keep         # leave the browser state on disk
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const OUT = '/tmp/vdrive'
mkdirSync(OUT, { recursive: true })
const URL = 'http://192.168.1.27:2026/'

const log = []
const say = (s) => { log.push(s); console.log(s) }

/** Everything about the observatory's state that an interaction could move. */
const probe = (page) => page.evaluate(() => {
  const txt = (s) => document.querySelector(s)?.textContent?.trim().replace(/\s+/g, ' ') ?? null
  const seqRead = txt('.mt-read') ?? txt('.mt-side') ?? ''
  const play = document.querySelector('.mt-play')
  return {
    seq: (document.querySelector('.mt-seq-n')?.textContent ?? seqRead.match(/\d+/)?.[0] ?? '?').trim(),
    playPressed: play?.getAttribute('aria-pressed') ?? null,
    playLabel: play?.textContent?.trim() ?? null,
    inspectorId: txt('.mi-id'),
    inspectorTier: txt('.mi-tier'),
    changeOp: txt('.mi-chg-op') ?? txt('.mi-chg b') ?? null,
    acidNodes: document.querySelectorAll('.mg-node.on, .mg-node.touched').length,
    selectedNode: document.querySelector('.mg-node.sel')?.getAttribute('data-id') ?? null,
    // the replay below the fold — must NOT move when we drive the observatory
    replayStep: txt('.fx-tp-meta'),
  }
})

const diff = (a, b) => {
  const out = []
  for (const k of Object.keys(a)) if (String(a[k]) !== String(b[k])) out.push(`${k}: ${a[k]} → ${b[k]}`)
  return out.length ? out.join(' | ') : '(no state change)'
}

async function run() {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 } })
  const page = await ctx.newPage()
  const errs = []
  page.on('pageerror', (e) => errs.push(String(e)))
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })

  await page.goto(URL, { waitUntil: 'networkidle' })
  await page.locator('nav button, header button').filter({ hasText: /长轨迹/ }).first().click()
  await page.waitForTimeout(3000)

  say('\n════ 1 · PLAYBACK CONTROLS ════')
  let before = await probe(page)
  say(`  start: seq=${before.seq} play=${before.playPressed} inspector=${before.inspectorId}`)

  // does it autoplay?
  await page.waitForTimeout(1500)
  let after = await probe(page)
  say(`  after 1.5s idle → ${diff(before, after)}`)

  // pause
  before = await probe(page)
  await page.locator('.mt-play').click()
  await page.waitForTimeout(900)
  after = await probe(page)
  say(`  click PAUSE → ${diff(before, after)}`)
  const pausedSeq = after.seq
  await page.waitForTimeout(1200)
  const stillPaused = await probe(page)
  say(`  wait 1.2s while paused → seq ${pausedSeq} → ${stillPaused.seq} ${pausedSeq === stillPaused.seq ? '✓ held' : '✗ STILL MOVING'}`)

  // step forward / back
  before = await probe(page)
  await page.locator('.mt-ctl button[title], .mt-ctl button').last().click()
  after = await probe(page)
  say(`  click STEP FWD → ${diff(before, after)}`)
  before = after
  await page.locator('.mt-ctl button').nth(1).click()
  after = await probe(page)
  say(`  click STEP BACK → ${diff(before, after)}`)

  // reset
  before = await probe(page)
  await page.locator('.mt-ctl button').first().click()
  after = await probe(page)
  say(`  click RESET → ${diff(before, after)}`)

  say('\n════ 2 · SCRUB THE RAIL ════')
  const rail = page.locator('.mt-plot')
  const box = await rail.boundingBox()
  before = await probe(page)
  await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.6)
  await page.waitForTimeout(400)
  after = await probe(page)
  say(`  click rail @50% → ${diff(before, after)}`)

  before = after
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.6)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width * 0.85, box.y + box.height * 0.6, { steps: 12 })
  await page.mouse.up()
  await page.waitForTimeout(400)
  after = await probe(page)
  say(`  DRAG 50%→85% → ${diff(before, after)}`)

  say('\n════ 3 · CLICK THE RARE OPS (the story beats) ════')
  const insight = page.locator('.mt-op.insight').first()
  if (await insight.count()) {
    before = await probe(page)
    await insight.click()
    await page.waitForTimeout(500)
    after = await probe(page)
    say(`  click INSIGHT mark → ${diff(before, after)}`)
    await page.screenshot({ path: `${OUT}/03-insight.png` })
  } else say('  ✗ no .mt-op.insight found')

  const refresh = page.locator('.mt-op.refresh').first()
  if (await refresh.count()) {
    before = await probe(page)
    await refresh.click()
    await page.waitForTimeout(500)
    after = await probe(page)
    say(`  click INSIGHT_REFRESH mark → ${diff(before, after)}`)
  } else say('  ✗ no .mt-op.refresh found')

  const link = page.locator('.mt-op.link').first()
  if (await link.count()) {
    before = await probe(page)
    await link.click()
    await page.waitForTimeout(500)
    after = await probe(page)
    say(`  click LINK mark → ${diff(before, after)}`)
  } else say('  ✗ no .mt-op.link found')

  say('\n════ 4 · PIN / UNPIN A MEMORY NODE ════')
  const nodes = page.locator('.mg-node')
  say(`  clickable nodes: ${await nodes.count()}`)
  before = await probe(page)
  await nodes.nth(3).click()
  await page.waitForTimeout(400)
  after = await probe(page)
  say(`  click node #3 → ${diff(before, after)}`)
  const pinnedId = after.inspectorId

  // does the pin SURVIVE playback moving?
  await page.locator('.mt-play').click()
  await page.waitForTimeout(1400)
  const during = await probe(page)
  say(`  play 1.4s while pinned → seq ${after.seq}→${during.seq}, inspector ${pinnedId}→${during.inspectorId} ${pinnedId === during.inspectorId ? '✓ pin held' : '✗ PIN LOST'}`)
  await page.locator('.mt-play').click()

  before = await probe(page)
  await nodes.nth(3).click()
  await page.waitForTimeout(400)
  after = await probe(page)
  say(`  click SAME node again (unpin?) → ${diff(before, after)}`)

  say('\n════ 5 · KEYBOARD ════')
  await rail.focus()
  before = await probe(page)
  await page.keyboard.press('ArrowRight')
  await page.waitForTimeout(300)
  after = await probe(page)
  say(`  focus rail, ArrowRight → ${diff(before, after)}`)
  before = after
  await page.keyboard.press('ArrowLeft')
  await page.waitForTimeout(300)
  after = await probe(page)
  say(`  ArrowLeft → ${diff(before, after)}`)

  before = await probe(page)
  await page.locator('.mt-play').focus()
  await page.keyboard.press('Space')
  await page.waitForTimeout(500)
  after = await probe(page)
  say(`  focus PLAY button, press Space → ${diff(before, after)}`)

  say('\n════ 6 · PENTEST INTERACTION ════')
  await page.locator('nav button, header button').filter({ hasText: /渗透/ }).first().click()
  await page.waitForTimeout(3500)
  const hosts = page.locator('button.pt-host, .pt-host')
  say(`  host cards: ${await hosts.count()}`)
  const dossierBefore = await page.evaluate(() => document.querySelector('[class*="dos"]')?.textContent?.trim().replace(/\s+/g,' ').slice(0, 60) ?? null)
  if (await hosts.count() > 1) {
    await hosts.nth(1).click()
    await page.waitForTimeout(600)
    const dossierAfter = await page.evaluate(() => document.querySelector('[class*="dos"]')?.textContent?.trim().replace(/\s+/g,' ').slice(0, 60) ?? null)
    say(`  click host #1 → dossier: "${dossierBefore}" → "${dossierAfter}" ${dossierBefore !== dossierAfter ? '✓ changed' : '✗ NO CHANGE'}`)
    await page.screenshot({ path: `${OUT}/06-pentest-host.png` })
  }

  say(`\n════ CONSOLE ERRORS: ${errs.length ? errs.slice(0, 5).join(' | ') : 'none'} ════`)
  await browser.close()
}

run().catch((e) => { console.error(e); process.exit(1) })
