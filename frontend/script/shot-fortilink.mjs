#!/usr/bin/env node
/* One-off: drill the SECOND subnet (fortilink) and screenshot the segment
 * board — the internet-egress panel only appears where egress was measured. */
import { chromium } from 'playwright'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1920, height: 1006 } })
await page.goto('http://192.168.1.27:2026/', { waitUntil: 'domcontentloaded' })
await page.locator('header button, nav button').filter({ hasText: /^实时图谱$/ }).first().click()
await page.waitForSelector('.flow-canvas', { timeout: 60000 })
await page.waitForTimeout(4000)
const subnets = page.locator('.if-node, .sub-node, g[class*="if-"]')
console.log('subnet-ish groups:', await subnets.count())
// interface nodes carry the per-subnet click; use the same selector the driver uses
const ifs = page.locator('.flow-canvas .n-v')
console.log('n-v count:', await ifs.count())
await ifs.nth(1).click()
await page.waitForTimeout(2500)
await page.screenshot({ path: '/tmp/vdrive-console/fortilink.png' })
// click an ego device that has egress: search for .27
await page.locator('.sg-node').first().waitFor({ timeout: 10000 }).catch(() => {})
await browser.close()
console.log('done')
