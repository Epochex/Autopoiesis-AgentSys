import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { chromium, type Browser } from 'playwright'
import { createServer, type ViteDevServer } from 'vite'

let server: ViteDevServer
let browser: Browser
let base: string
let cacheDir: string
beforeAll(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), 'ip-conflict-vite-'))
  server = await createServer({ cacheDir, server: { host: '127.0.0.1', port: 0, watch: { ignored: ['**/*'] } }, plugins: [{
    name: 'conflict-test-harness',
    resolveId(id) { if (id === 'virtual:conflict-test') return '\0conflict-test' },
    load(id) { if (id === '\0conflict-test') return `import React from 'react'; import {createRoot} from 'react-dom/client'; import {IpConflictResolution} from '/src/components/IpConflictResolution.tsx'; import {ChangeWorkbench} from '/src/components/ChangeWorkbench.tsx'; window.prepared=[]; function Harness(){const [id,setId]=React.useState('');return React.createElement(React.Fragment,null,React.createElement(IpConflictResolution,{ip:'192.168.1.110',caseId:'case-one',lang:'en',onPrepared:value=>{window.prepared.push(value);setId(value)}}),location.search?React.createElement(ChangeWorkbench,{ip:'192.168.1.110',caseId:'case-one',lang:'en',selectedChangeId:id}):null)} createRoot(document.getElementById('root')).render(React.createElement(Harness));` },
    configureServer(vite) { vite.middlewares.use('/conflict-test', async (_req, res) => { res.setHeader('Content-Type', 'text/html'); res.end(await vite.transformIndexHtml('/conflict-test', '<div id="root"></div><script type="module" src="/@id/virtual:conflict-test"></script>')) }) },
  }] })
  await server.listen()
  base = server.resolvedUrls!.local[0]
  browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] })
}, 30000)
afterAll(async () => { try { await browser?.close(); await server?.close() } finally { if (cacheDir) await rm(cacheDir, {recursive:true,force:true}) } })

describe('IP conflict operator flow', () => {
  it.each([true, false])('requires owner choice and prepares without execution (success=%s)', async success => {
    const page = await browser.newPage()
    const writes: { url: string; body: Record<string, unknown> }[] = []
    await page.route('**/api/rca/**', async route => {
      const request = route.request()
      if (request.method() === 'POST') {
        writes.push({ url: request.url(), body: request.postDataJSON() })
        await route.fulfill({ status: success ? 200 : 409, contentType: 'application/json', body: JSON.stringify(success ? { id: 'prepared-one', plan: { playbook: { scope: 'dhcp_reservation', requires_client_renewal: true, static_conflict_requires_endpoint_or_port_change: true, apply_commands: ['config system dhcp server'] } } } : { detail: 'ownership_revision_changed_refresh_required' }) })
      } else await route.fulfill({ json: { state: { revision: 3, expected_mac: null }, observation_stale: false, observation_at: '2026-09-08T10:00:00Z', claimants: [{ mac: '00:11:22:33:44:66', name: 'Verified workstation', retired: false, samples: 18, in_arp_snapshot: true }] } })
    })
    await page.goto(base + 'conflict-test')
    const prepare = page.getByRole('button', { name: 'Prepare approval plan and commands' })
    await prepare.waitFor()
    expect(await prepare.isDisabled()).toBe(true)
    await page.getByRole('textbox', { name: 'Assignment reason' }).fill('Verified label and dedicated access port')
    expect(await prepare.isDisabled()).toBe(true)
    expect(writes).toHaveLength(0)
    await page.getByRole('radio').check()
    expect(await prepare.isEnabled()).toBe(true)
    await prepare.click()
    if (success) await page.waitForFunction(() => (window as unknown as { prepared: string[] }).prepared.length === 1)
    else await page.getByRole('alert').filter({ hasText: 'ownership_revision_changed_refresh_required' }).waitFor()
    expect(await page.evaluate(() => (window as unknown as { prepared: string[] }).prepared)).toEqual(success ? ['prepared-one'] : [])
    expect(writes).toHaveLength(1)
    expect(writes[0].url).toContain('/api/rca/changes/ip-conflict/prepare')
    expect(writes[0].body).toMatchObject({ ip: '192.168.1.110', mac: '00:11:22:33:44:66', revision: 3, case_id: 'case-one' })
    expect(writes.some(write => /\/execute|\/approve|\/decision/.test(write.url))).toBe(false)
    await page.close()
  }, 30000)
})


describe('conflict plan approval and execution', () => {
  it('renders command arrays alongside scope metadata and submits only explicit approved execution', async () => {
    const page = await browser.newPage()
    const posts: string[] = []
    let prepared = false
    const plan = { id: 'prepared-one', status: 'awaiting_approval', digest: 'a'.repeat(64), device_id: 'fortigate', action: 'dhcp_reserve', reason: 'Verified workstation', case_id: 'case-one', created_at: 1700000000, expires_at: 2000000000, identity: { serial: 'TEST-DEVICE' }, target: { ip: '192.168.1.110', mac: '00:11:22:33:44:66' }, events: [], plan: { title: 'Reserve verified address', operations: [{ key: 'reservation/9/27', before: null, after: { ip: '192.168.1.110', mac: '00:11:22:33:44:66' } }], verification_scope: 'DHCP configuration and client observation', manual_restore: true, observation_interval: 15, observation_samples: 12, playbook: { scope: 'dhcp_reservation', requires_client_renewal: true, static_conflict_requires_endpoint_or_port_change: true, precheck_commands: ['get system arp'], apply_commands: ['config system dhcp server\n    edit 9\nend'], rollback_commands: ['delete 27'], verify_commands: ['execute dhcp lease-list'] } } }
    const pageErrors: string[] = []
    page.on('pageerror', error => pageErrors.push(error.message))
    await page.route('**/api/rca/**', async route => {
      const r = route.request(), url = new URL(r.url())
      let value: unknown = {}
      if (r.method() === 'POST') {
        posts.push(url.pathname)
        if (url.pathname.endsWith('/prepare')) { prepared = true; value = plan }
        else if (url.pathname.endsWith('/approve')) { expect(r.postDataJSON().digest).toBe(plan.digest); plan.status = 'approved'; value = plan }
        else if (url.pathname.endsWith('/execute')) { expect(plan.status).toBe('approved'); plan.status = 'succeeded'; value = { accepted: true } }
        else throw new Error('Unexpected mutation ' + url.pathname)
      } else if (url.pathname.endsWith('/ip-conflict')) value = { state: { revision: 3 }, claimants: [{ mac: '00:11:22:33:44:66', name: 'Verified workstation', samples: 18 }] }
      else if (url.pathname.endsWith('/catalog')) value = { devices: [{ id: 'fortigate', label: 'FortiGate', kind: 'fortigate', configured: true }], actions: [] }
      else if (url.pathname.endsWith('/control')) value = { paused: false }
      else if (url.pathname.endsWith('/assessment')) value = { headline: 'Assignment review', pending_count: prepared ? 1 : 0, decision: {} }
      else if (url.pathname.endsWith('/prepared-one')) value = plan
      else if (url.pathname.endsWith('/changes')) value = { changes: prepared ? [plan] : [] }
      await route.fulfill({ json: value })
    })
    await page.goto(base + 'conflict-test?integrated=1')
    await page.getByRole('radio').check()
    await page.getByRole('textbox', { name: 'Assignment reason' }).fill('Verified label and access point')
    await page.getByRole('button', { name: 'Prepare approval plan and commands' }).click()
    await page.getByText('Playbook and runnable commands').waitFor()
    expect(await page.locator('.change-playbook').innerText()).toContain('edit 9')
    expect(await page.locator('.change-playbook').innerText()).toContain('delete 27')
    expect(posts).toEqual(['/api/rca/changes/ip-conflict/prepare'])
    await page.getByRole('textbox', { name: 'Approval rationale' }).fill('Verified exact target and rollback')
    await page.getByRole('button', { name: 'Approve this diff', exact: true }).click()
    await page.getByRole('button', { name: 'Execute approved change →', exact: true }).waitFor()
    expect(posts).toHaveLength(2)
    await page.getByRole('button', { name: 'Execute approved change →', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.change-observation') !== null)
    expect(posts).toEqual(['/api/rca/changes/ip-conflict/prepare', '/api/rca/changes/prepared-one/approve', '/api/rca/changes/prepared-one/execute'])
    expect(pageErrors).toEqual([])
    await page.close()
  }, 30000)
})
