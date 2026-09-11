#!/usr/bin/env node
/* Walks the console in English and reports any Chinese still on screen.
 *
 * Switching the language is not the same as being translated. Three separate
 * mechanisms put English on this console — per-component ternaries, the
 * `change-copy` dictionary and the display lexicon — and a string that none of
 * them covers does not fail loudly: it simply renders in Chinese, in a panel
 * nobody opened during review. So this opens them: every scenario, every view,
 * and every drill target inside each view, reading the real text nodes and the
 * attributes a screen reader would announce.
 *
 * Raw evidence is deliberately exempt. A device receipt, a command's output and
 * a stored audit record are quoted verbatim in both languages, so a <pre> block
 * is skipped rather than reported.
 *
 *   node script/rehearse-english-ui.mjs                 # against the dev server
 *   APP_URL=http://host:2026/ node script/…             # against a deployment
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'

const URL = process.env.APP_URL ?? 'http://127.0.0.1:5199/'
const OUT = process.env.EN_UI_OUT ?? '/tmp/en-ui'
mkdirSync(OUT, { recursive: true })

const HARVEST = (where) => {
  const HAN = /[㐀-鿿]/
  const rows = []
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let node
  while ((node = walker.nextNode())) {
    const text = (node.nodeValue || '').trim()
    if (!text || !HAN.test(text)) continue
    const el = node.parentElement
    if (!el || el.closest('pre, code')) continue
    const box = el.getBoundingClientRect()
    if (!box.width && !box.height) continue
    const style = getComputedStyle(el)
    if (style.display === 'none' || style.visibility === 'hidden') continue
    const path = []
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cls = typeof e.className === 'string' && e.className.trim()
        ? '.' + e.className.trim().split(/\s+/).slice(0, 3).join('.') : ''
      path.unshift(e.tagName.toLowerCase() + cls)
      if (path.length >= 3) break
    }
    rows.push({ where, text: text.replace(/\s+/g, ' ').slice(0, 240), sel: path.join('>').slice(0, 150) })
  }
  for (const el of document.querySelectorAll('[title],[aria-label],[placeholder]')) {
    for (const name of ['title', 'aria-label', 'placeholder']) {
      const value = el.getAttribute(name)
      if (value && HAN.test(value)) {
        rows.push({ where, text: `@${name}=${value.replace(/\s+/g, ' ').slice(0, 200)}`, sel: el.tagName.toLowerCase() })
      }
    }
  }
  return rows
}

const DRILL = 'main button, .sa-case, button.gm-record, tr[role="button"], summary, [role="tab"], aside button, table button, li button'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1680, height: 1200 } })
page.on('dialog', dialog => dialog.dismiss().catch(() => {}))
await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 })
await page.waitForTimeout(4000)
await page.evaluate(() => [...document.querySelectorAll('.lang button')]
  .find(button => button.textContent.trim() === 'EN')?.click())
await page.waitForTimeout(3000)

const clickByText = (selector, label) => page.evaluate(([selector, label]) => {
  const target = [...document.querySelectorAll(selector)].find(el => el.textContent.trim() === label)
  if (!target) throw new Error('missing: ' + label)
  target.click()
}, [selector, label])

const found = []
const scenarios = await page.$$eval('.scen button', buttons => buttons.map(b => b.textContent.trim()))
for (const scenario of scenarios) {
  await clickByText('.scen button', scenario).catch(() => {})
  await page.waitForTimeout(2000)
  const views = await page.$$eval('.pager button', buttons => buttons.map(b => b.textContent.trim())).catch(() => [])
  for (const view of views) {
    if (/3D/.test(view)) continue
    const open = async () => {
      await clickByText('.scen button', scenario); await page.waitForTimeout(1200)
      await clickByText('.pager button', view); await page.waitForTimeout(2500)
    }
    try { await open() } catch { continue }
    found.push(...await page.evaluate(HARVEST, `${scenario}/${view}`))
    const targets = await page.evaluate((selector) => {
      const labels = []
      const seen = new Set()
      for (const el of document.querySelectorAll(selector)) {
        if (el.closest('.pager') || el.closest('.scen') || el.closest('.lang')) continue
        const box = el.getBoundingClientRect()
        if (!box.width || !box.height) continue
        const label = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60)
        if (!label || seen.has(label)) continue
        seen.add(label); labels.push(label)
        if (labels.length >= 22) break
      }
      return labels
    }, DRILL)
    for (const target of targets) {
      try {
        await page.evaluate(([selector, label]) => {
          const el = [...document.querySelectorAll(selector)]
            .find(x => (x.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60) === label)
          if (!el) throw new Error('gone')
          el.click()
        }, [DRILL, target])
        await page.waitForTimeout(1800)
        found.push(...await page.evaluate(HARVEST, `${scenario}/${view} ▸ ${target.slice(0, 40)}`))
      } catch { /* the target moved or closed itself; the next one still runs */ }
    }
    await page.screenshot({ path: `${OUT}/${scenario}-${view}.png`.replace(/[^\w/.-]+/g, '_') }).catch(() => {})
    console.log(`${scenario}/${view}: ${targets.length} drill targets`)
    try { await open() } catch { /* fall through to the next view */ }
  }
}
await browser.close()

writeFileSync(`${OUT}/findings.json`, JSON.stringify(found, null, 1))
const unique = new Map()
for (const row of found) if (!unique.has(row.text)) unique.set(row.text, row)
console.log(`\n${unique.size} Chinese strings left in the English UI (${found.length} occurrences)`)
for (const [text, row] of unique) console.log(`  ${row.where}\n    ${text}\n    ${row.sel}`)
process.exit(unique.size ? 1 : 0)
