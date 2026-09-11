#!/usr/bin/env node
/* Watches one defence usecase the way the audience will: console open, the
 * incident lands, the toast is clicked, the theater plays, then every step of
 * the bar. Screenshots go to $WALK_OUT; anything the page reports as an error
 * is collected. Run it, then start the usecase script ~15 s later. */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
const URL = process.env.APP_URL ?? 'http://127.0.0.1:8026/'
const OUT = process.env.WALK_OUT ?? '/tmp/walk'
mkdirSync(OUT, { recursive: true })
const b = await chromium.launch(); const page = await b.newPage({ viewport: { width: 1680, height: 1150 } })
const errors = []; page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 140)) })
const alerts = async (tag) => page.evaluate(() => [...document.querySelectorAll('[role=alert], .change-error, .ci-error, .dx-state')].map(e => e.textContent.trim().replace(/\s+/g, ' ').slice(0, 140)).filter(Boolean)).then(a => a.map(t => `${tag}: ${t}`))
const notes = []; const shot = (n) => page.screenshot({ path: `${OUT}/${n}.png` })
await page.goto(URL, { waitUntil: 'domcontentloaded' }); await page.waitForTimeout(4000)
const toast = await page.waitForSelector('.la-toast', { timeout: 240000 }).catch(() => null)
if (!toast) { console.log('NO TOAST'); await shot('no-toast'); await b.close(); process.exit(1) }
await shot('00-toast'); notes.push(...await alerts('toast'))
await toast.click()
for (const t of [2, 15, 30, 45, 60, 80, 100]) {
  await page.waitForTimeout(t === 2 ? 2000 : 15000 + (t === 80 || t === 100 ? 5000 : 0))
  const rail = await page.evaluate(() => [...document.querySelectorAll('.th-drill-stages *')].filter(x => !x.children.length).map(x => x.textContent.trim()).filter(Boolean).slice(0, 8).join(' | '))
  const exec = await page.evaluate(() => document.querySelector('[data-execution-state]')?.getAttribute('data-execution-state'))
  console.log(`theater +${t}s exec=${exec} rail=${rail}`)
  await shot(`01-theater-${String(t).padStart(3, '0')}s`); notes.push(...await alerts(`theater+${t}`))
}
for (const [n, label] of [[1, '01-facts'], [3, '03-sources'], [4, '04-actions'], [5, '05-memory']]) {
  await page.evaluate((n) => document.querySelectorAll('.ifb-steps button')[n - 1]?.click(), n)
  await page.waitForTimeout(n === 5 ? 9000 : 6000)
  await shot(`02-step-${label}`); notes.push(...await alerts(label))
}
await page.evaluate(() => document.querySelectorAll('.ifb-steps button')[1]?.click()); await page.waitForTimeout(8000); await shot('03-theater-final')
writeFileSync(`${OUT}/notes.json`, JSON.stringify({ errors, notes }, null, 1))
console.log('console errors:', errors.length, errors.slice(0, 5)); console.log('on-screen alerts:', notes.length, notes.slice(0, 8))
await b.close()
