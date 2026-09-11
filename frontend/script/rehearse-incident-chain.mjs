#!/usr/bin/env node
/* Walks one controlled-demo incident through the whole chain and fails if any
 * step does not land on its surface:
 *   incident centre → demo row → 02 theater on the console (bar current)
 *   → 05 memory observatory → 01 case facts → 04 action & readback → 03 sources.
 * Then flips to English and reports Chinese left inside the step bar. */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
const URL = process.env.APP_URL ?? 'http://127.0.0.1:5199/'
const OUT = process.env.CHAIN_OUT ?? '/tmp/incident-chain'
mkdirSync(OUT, { recursive: true })
const failures = []
const check = (ok, what) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}`); if (!ok) failures.push(what) }
const b = await chromium.launch()
const page = await b.newPage({ viewport: { width: 1680, height: 1150 } })
page.on('dialog', d => d.dismiss().catch(() => {}))
const shot = (name) => page.screenshot({ path: `${OUT}/${name}.png` }).catch(() => {})
const clickNav = (label) => page.evaluate((label) => {
  const b = [...document.querySelectorAll('.pager button')].find(x => x.textContent.trim() === label); if (!b) throw new Error('nav ' + label); b.click()
}, label)
const clickStep = (n) => page.evaluate((n) => {
  const b = document.querySelectorAll('.ifb-steps button')[n - 1]; if (!b) throw new Error('step ' + n); b.click()
}, n)
const currentStep = () => page.evaluate(() => [...document.querySelectorAll('.ifb-steps button')].findIndex(b => b.classList.contains('is-current')) + 1)
const has = (sel) => page.waitForSelector(sel, { timeout: 30000 }).then(() => true).catch(() => false)

await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 })
await page.waitForTimeout(4000)
await clickNav('事件中心').catch(() => clickNav('待处理事件'))
await page.waitForTimeout(4000)
// A finished demo is a closed case; the queue opens on active ones.
await page.evaluate(() => [...document.querySelectorAll('.sa-queue-filters button')].find(x => /已关闭|Closed/.test(x.textContent))?.click())
await page.waitForTimeout(2500)
const demoRow = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('.sa-case-queue-list > button')]
  const row = rows.find(r => r.querySelector('.sa-case-demo')) ?? rows[0]
  if (!row) return null; const t = row.textContent.trim().slice(0, 90); row.click(); return t
})
check(Boolean(demoRow), `incident centre has a case row (${demoRow})`)
await page.waitForTimeout(6000)
check(await has('.ifb'), 'step bar is rendered after opening the case')
check(await has('.th-drill'), '02 theater opened on the console with the drilldown')
check((await currentStep()) === 2, `bar marks step 02 current (got ${await currentStep()})`)
await shot('02-theater')
await clickStep(5); await page.waitForTimeout(6000)
check(await has('.lmg'), '05 memory observatory (.lmg) mounted')
check(await page.evaluate(() => /记忆观测舱|MEMORY OBSERVATORY/.test(document.querySelector('.lmg h2')?.textContent ?? '')), '05 observatory header present')
await shot('05-memory')
await clickStep(1); await page.waitForTimeout(4000)
check(await has('.case-investigation'), '01 case facts page mounted')
await shot('01-case')
await clickStep(4); await page.waitForTimeout(4000)
check(await page.evaluate(() => Boolean(document.querySelector('.dx, .diagnose, [class*="dx-"]'))), '04 action & readback workspace mounted')
await shot('04-actions')
await clickStep(3); await page.waitForTimeout(4000)
check(await page.evaluate(() => Boolean(document.querySelector('[class*="rt-"], .retrieval, [class*="retrieval"]'))), '03 sources workspace mounted')
await shot('03-sources')
await clickStep(2); await page.waitForTimeout(6000)
check(await has('.th-drill'), '02 theater re-opens from the bar (focus auto-open)')
await shot('02-theater-again')
// English: nothing Chinese inside the bar
await page.evaluate(() => [...document.querySelectorAll('.lang button')].find(x => x.textContent.trim() === 'EN')?.click())
await page.waitForTimeout(2500)
const cjk = await page.evaluate(() => [...document.querySelectorAll('.ifb *')].map(e => e.childNodes).flatMap(n => [...n]).filter(n => n.nodeType === 3 && /[㐀-鿿]/.test(n.nodeValue)).map(n => n.nodeValue.trim()))
check(cjk.length === 0, `English bar has no Chinese (${cjk.join(' | ').slice(0, 120)})`)
await shot('02-theater-en')
await b.close()
console.log(failures.length ? `\n${failures.length} FAILED` : '\nALL STEPS PASSED')
process.exit(failures.length ? 1 : 0)
