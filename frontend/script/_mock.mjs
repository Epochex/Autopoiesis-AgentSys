import { chromium } from 'playwright'
const out = process.argv[2]
const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 1560, height: 1400 }, deviceScaleFactor: 1.4 })
const errs = []
p.on('pageerror', e => errs.push(String(e)))
p.on('console', m => { if (m.type()==='error') errs.push(m.text()) })
await p.goto('http://127.0.0.1:8099/round2.html', { waitUntil: 'networkidle' })
await p.waitForFunction(() => document.body.dataset.ready === '1', { timeout: 8000 }).catch(()=>{})
await p.waitForTimeout(800)
for (const [id, name] of [['matrix','5-matrix'],['replay','6-replay'],['sankey','7-sankey'],['hilbert','8-hilbert']]) {
  const el = p.locator(`#${id}`).first()
  if (await el.count() && await el.locator('svg').count()) await el.screenshot({ path: `${out}/${name}.png` })
  else errs.push(`${id}: no svg`)
}
console.log('errors:', errs.length ? errs.slice(0,5) : 'none')
await b.close()
