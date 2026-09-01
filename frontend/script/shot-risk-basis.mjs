#!/usr/bin/env node
/* One-off: search a flagged asset, open its ego view, screenshot the risk
 * basis blocks in both the ego panel and the traffic portrait. */
import { chromium } from 'playwright'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1920, height: 1006 } })
await page.goto('http://192.168.1.27:2026/', { waitUntil: 'domcontentloaded' })
await page.locator('header button, nav button').filter({ hasText: /^实时图谱$/ }).first().click()
await page.waitForSelector('.flow-canvas', { timeout: 60000 })
await page.waitForTimeout(3000)
await page.locator('.ts-input').fill(process.argv[2] ?? '192.168.16.69')
await page.waitForTimeout(800)
await page.locator('.ts-hit').first().click()
await page.waitForTimeout(2500)
await page.locator('.sg-node.sel').first().click()
await page.waitForTimeout(3000)
await page.screenshot({ path: '/tmp/vdrive-console/risk-basis.png' })
const egoRisk = await page.locator('.sg-ego-risk').count()
const dpRisk = await page.locator('.dp-risk').count()
console.log(`sg-ego-risk blocks: ${egoRisk} · dp-risk blocks: ${dpRisk}`)
await browser.close()
