#!/usr/bin/env node
/* Visual review harness — drive the real console in a real browser and report
 * what is objectively wrong, so a change can be verified without a human
 * eyeballing a screenshot.
 *
 * Checks, in order of how often they actually bite this repo:
 *   overflow   an element whose content is wider/taller than its box. Catches
 *              clipped labels ("NO TEXT REWRIT") and panels escaping the page.
 *   contrast   axe-core color-contrast. The light-editorial palette puts gray on
 *              paper constantly; this is the difference between "restrained" and
 *              "illegible", and it is measurable.
 *   fold       does the first screen resolve without scrolling.
 *   console    page errors / failed requests.
 *   pixels     screenshot per (view × lang × viewport), plus an optional diff
 *              against a baseline so an iteration shows what it moved.
 *
 * Usage:
 *   node script/vreview.mjs                          # all views, zh+en, 1920+1440
 *   node script/vreview.mjs --view=trajectory        # one view
 *   node script/vreview.mjs --lang=en --w=1440 --h=900
 *   node script/vreview.mjs --base=<dir>             # diff against a baseline
 *   node script/vreview.mjs --out=<dir>              # where shots go
 *
 * Exit code is the number of hard failures (overflow / contrast / console), so
 * it drops straight into a verify loop.
 */
import { chromium } from 'playwright'
import AxeBuilder from '@axe-core/playwright'
import { PNG } from 'pngjs'
import pixelmatch from 'pixelmatch'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const arg = (k, d) => {
  const hit = process.argv.find((a) => a.startsWith(`--${k}=`))
  return hit ? hit.slice(k.length + 3) : d
}

const URL_BASE = arg('url', 'http://192.168.1.27:2026/')
const OUT = arg('out', '/tmp/vreview')
const BASE = arg('base', '')
const ONLY_VIEW = arg('view', '')
const ONLY_LANG = arg('lang', '')
const W = Number(arg('w', 0))
const H = Number(arg('h', 0))
/* Playback settles ~15s (257 events x 60ms); wait it out so shots are stable. */
const SETTLE = Number(arg('settle', 18000))

/* nav button label per view, zh + en */
const VIEWS = {
  console: [/^态势$/, /^CONSOLE$/],
  trajectory: [/^长轨迹$/, /^TRAJECTORY$/],
  pentest: [/^渗透$/, /^PENTEST$/],
}
const VIEWPORTS = W && H ? [[W, H]] : [[1920, 1080], [1440, 900]]
const LANGS = ONLY_LANG ? [ONLY_LANG] : ['zh', 'en']
const VIEW_KEYS = ONLY_VIEW ? [ONLY_VIEW] : Object.keys(VIEWS)

mkdirSync(OUT, { recursive: true })

/** Content that is actually being CUT OFF.
 *
 * Deliberately narrow. scrollWidth/scrollHeight are meaningless on SVG nodes
 * (every <text> in the graph reports a bogus overflow) and routinely run a few
 * px over on HTML from line-height rounding — so a naive check drowns the real
 * defects in ~40 false positives. Content only truly disappears when the box
 * clips it, so require overflow:hidden|clip. Elements that scroll, or that
 * ellipsis, are reported separately: both degrade honestly. */
async function overflows(page) {
  return page.evaluate(() => {
    const out = []
    for (const el of document.querySelectorAll('body *')) {
      if (el instanceof SVGElement) continue
      const cs = getComputedStyle(el)
      if (cs.display === 'none' || cs.visibility === 'hidden') continue
      if (el.clientWidth === 0 && el.clientHeight === 0) continue
      if (/(auto|scroll)/.test(cs.overflowY + cs.overflowX)) continue

      const clips = /(hidden|clip)/.test(cs.overflowX) || /(hidden|clip)/.test(cs.overflowY)
      if (!clips) continue

      const dx = el.scrollWidth - el.clientWidth
      const dy = el.scrollHeight - el.clientHeight
      if (dx <= 1 && dy <= 1) continue

      out.push({
        sel: el.className && typeof el.className === 'string'
          ? `${el.tagName.toLowerCase()}.${el.className.trim().split(/\s+/).join('.')}`
          : el.tagName.toLowerCase(),
        dx, dy,
        ellipsis: cs.textOverflow === 'ellipsis',
        text: (el.textContent || '').trim().slice(0, 54),
      })
    }
    return out
  })
}

async function run() {
  const browser = await chromium.launch()
  let fails = 0
  const lines = []

  for (const view of VIEW_KEYS) {
    for (const lang of LANGS) {
      for (const [w, h] of VIEWPORTS) {
        const tag = `${view}-${lang}-${w}x${h}`
        // axe requires a context-owned page, not browser.newPage()
        const ctx = await browser.newContext({ viewport: { width: w, height: h } })
        const page = await ctx.newPage()
        const errs = []
        page.on('pageerror', (e) => errs.push(String(e)))
        page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })
        page.on('requestfailed', (r) => errs.push(`REQ ${r.url()} ${r.failure()?.errorText}`))

        await page.goto(URL_BASE, { waitUntil: 'networkidle' })

        const langBtn = page.locator('header button, nav button')
          .filter({ hasText: lang === 'zh' ? /^中$/ : /^EN$/ })
        if (await langBtn.count()) await langBtn.first().click()

        const nav = page.locator('nav button, header button').filter({ hasText: VIEWS[view][lang === 'zh' ? 0 : 1] })
        if (await nav.count()) await nav.first().click()
        await page.waitForTimeout(SETTLE)

        /* --- overflow --- */
        const ov = await overflows(page)
        const hard = ov.filter((o) => !o.ellipsis)
        const soft = ov.filter((o) => o.ellipsis)

        /* --- contrast (axe) --- */
        let contrast = []
        try {
          const res = await new AxeBuilder({ page }).withRules(['color-contrast']).analyze()
          contrast = res.violations.flatMap((v) => v.nodes.map((n) => ({
            impact: n.impact,
            msg: (n.any?.[0]?.message || v.help).replace(/\s+/g, ' ').slice(0, 120),
            target: String(n.target?.[0] ?? '').slice(0, 70),
          })))
        } catch (e) {
          contrast = [{ impact: 'error', msg: `axe failed: ${e}`, target: '' }]
        }

        /* --- fold --- */
        const fold = await page.evaluate(() => ({
          scrollH: document.body.scrollHeight,
          scrollW: document.documentElement.scrollWidth,
          clientW: document.documentElement.clientWidth,
        }))
        const hScroll = fold.scrollW > fold.clientW + 1

        /* --- pixels --- */
        const shot = join(OUT, `${tag}.png`)
        await page.screenshot({ path: shot })

        let diff = ''
        if (BASE) {
          const basePath = join(BASE, `${tag}.png`)
          if (existsSync(basePath)) {
            const a = PNG.sync.read(readFileSync(basePath))
            const b = PNG.sync.read(readFileSync(shot))
            if (a.width === b.width && a.height === b.height) {
              const out = new PNG({ width: a.width, height: a.height })
              const n = pixelmatch(a.data, b.data, out.data, a.width, a.height, { threshold: 0.1 })
              const pct = ((n / (a.width * a.height)) * 100).toFixed(2)
              writeFileSync(join(OUT, `${tag}.diff.png`), PNG.sync.write(out))
              diff = ` | diff ${pct}% (${n}px)`
            } else diff = ' | diff: size changed'
          }
        }

        const bad = hard.length + contrast.length + errs.length + (hScroll ? 1 : 0)
        fails += bad
        lines.push(`\n=== ${tag} ${bad ? '✗ ' + bad : '✓'}${diff}`)
        lines.push(`    fold: pageH=${fold.scrollH} vs viewportH=${h}${hScroll ? '  ⚠ HORIZONTAL SCROLL' : ''}`)
        if (errs.length) lines.push(`    console (${errs.length}):\n${errs.slice(0, 4).map((e) => '      ' + e.slice(0, 110)).join('\n')}`)
        if (hard.length) lines.push(`    overflow HARD (${hard.length}):\n${hard.slice(0, 8).map((o) => `      ${o.sel} +${o.dx}x${o.dy} :: ${o.text}`).join('\n')}`)
        if (soft.length) lines.push(`    overflow ellipsis (${soft.length}, degrades ok):\n${soft.slice(0, 4).map((o) => `      ${o.sel} +${o.dx} :: ${o.text}`).join('\n')}`)
        if (contrast.length) lines.push(`    contrast (${contrast.length}):\n${contrast.slice(0, 8).map((c) => `      [${c.impact}] ${c.target} :: ${c.msg}`).join('\n')}`)
        await ctx.close()
      }
    }
  }

  await browser.close()
  console.log(lines.join('\n'))
  console.log(`\nshots: ${OUT}`)
  console.log(fails ? `\nFAIL · ${fails} issue(s)` : '\nPASS · clean')
  process.exit(Math.min(fails, 250))
}

run().catch((e) => { console.error(e); process.exit(255) })
