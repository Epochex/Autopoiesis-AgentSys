#!/usr/bin/env node
/* The defence-demo entry: the console is open, a controlled incident lands, the
 * toast appears, the operator clicks it. It must open the theater ON the
 * console and leave it there with the step bar at 02, then reach the memory
 * observatory from the bar. Run alongside `./scripts/demo_self_healing.py`. */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
const URL = process.env.APP_URL ?? 'http://127.0.0.1:5199/'
const OUT = process.env.CHAIN_OUT ?? '/tmp/incident-chain'
mkdirSync(OUT, { recursive: true })
const failures = []
const check = (ok, what) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}`); if (!ok) failures.push(what) }
const b = await chromium.launch()
const page = await b.newPage({ viewport: { width: 1680, height: 1150 } })
await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 })
await page.waitForTimeout(3000)
const toast = await page.waitForSelector('.la-toast', { timeout: 150000 }).catch(() => null)
check(Boolean(toast), 'toast appeared on the console for the controlled incident')
if (toast) {
  const text = await toast.textContent()
  console.log('     toast:', text.trim().replace(/\s+/g, ' ').slice(0, 120))
  await page.screenshot({ path: `${OUT}/toast.png` })
  await toast.click()
  await page.waitForTimeout(5000)
  check(await page.$('.th-drill') !== null, 'theater opened on the console after clicking the toast')
  check(await page.$('.ifb') !== null, 'step bar present')
  const cur = await page.evaluate(() => [...document.querySelectorAll('.ifb-steps button')].findIndex(x => x.classList.contains('is-current')) + 1)
  check(cur === 2, `bar marks step 02 current (got ${cur})`)
  check(await page.$('.case-investigation') === null, 'box card is NOT what opened')
  await page.screenshot({ path: `${OUT}/toast-theater.png` })
  await page.waitForTimeout(12000)
  check(await page.$('.th-drill') !== null, 'theater still on screen 12 s later (chain playing)')
  const stages = await page.evaluate(() => [...document.querySelectorAll('.th-drill-stages *')].filter(x => x.children.length === 0).map(x => x.textContent.trim().replace(/\s+/g, ' ')).filter(Boolean).slice(0, 14))
  console.log('     stages:', stages.join(' | '))
  await page.screenshot({ path: `${OUT}/toast-theater-later.png` })
  await page.evaluate(() => document.querySelectorAll('.ifb-steps button')[4]?.click())
  await page.waitForTimeout(6000)
  check(await page.$('.lmg') !== null, '05 memory observatory reached from the bar')
  await page.screenshot({ path: `${OUT}/toast-memory.png` })
  await page.evaluate(() => document.querySelectorAll('.ifb-steps button')[1]?.click())
  // The console re-loads its projection first; under a busy gateway that can
  // take a while, so wait for the stage rather than sampling once.
  check(await page.waitForSelector('.th-drill', { timeout: 30000 }).then(() => true).catch(() => false), '02 theater re-opens from the bar for the demo subject')
  await page.screenshot({ path: `${OUT}/toast-theater-back.png` })
}
await b.close()
console.log(failures.length ? `\n${failures.length} FAILED` : '\nALL STEPS PASSED')
process.exit(failures.length ? 1 : 0)
