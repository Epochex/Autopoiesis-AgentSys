import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { chromium, type Browser } from 'playwright'
import { createServer, type ViteDevServer } from 'vite'
import { matchCompletedEvent, type CompletedEvent } from './completed-events'

let server: ViteDevServer
let browser: Browser
let base: string
let cacheDir: string
beforeAll(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), 'completed-events-vite-'))
  server = await createServer({ cacheDir, server: {host:'127.0.0.1', port:0, watch: { ignored: ['**/*'] }}, plugins:[{
    name:'completed-event-test',
    resolveId(id) { if (id === 'virtual:completed-test') return '\0completed-test' },
    load(id) { if (id === '\0completed-test') return `import React from 'react'; import {createRoot} from 'react-dom/client'; import {LiveAlerts} from '/src/components/LiveAlerts.tsx'; window.opened=[]; createRoot(document.getElementById('root')).render(React.createElement(LiveAlerts,{lang:'en',onOpen:(...args)=>window.opened.push(args)}));` },
    configureServer(vite) { vite.middlewares.use('/completed-test',async (_req,res)=>{res.setHeader('Content-Type','text/html');res.end(await vite.transformIndexHtml('/completed-test','<div id="root"></div><script type="module" src="/@id/virtual:completed-test"></script>'))}) },
  }]})
  await server.listen(); base=server.resolvedUrls!.local[0]; browser=await chromium.launch({headless:true})
},30000)
afterAll(async()=>{ try { await browser?.close(); await server?.close() } finally { if (cacheDir) await rm(cacheDir,{recursive:true,force:true}) } })

describe('completed event actions',()=>{
  it('matches one cycle exactly, so archiving never hides a recurrence',()=>{
    const record={kind:'sentinel',subject:'service-one',cycleOpenedAt:'2026-09-08T10:00:00Z',archived:true} as CompletedEvent
    expect(matchCompletedEvent([record],'service-one',record.cycleOpenedAt)).toBe(record)
    expect(matchCompletedEvent([record],'service-one','2026-09-08T11:00:00Z')).toBeUndefined()
    expect(matchCompletedEvent([record],'service-one-child',record.cycleOpenedAt)).toBeUndefined()
  })
  it('writes memory only on explicit action, then removes the closed record with server persistence',async()=>{
    const page=await browser.newPage();page.setDefaultTimeout(7000);const writes:string[]=[];const errors:string[]=[]
    page.on('pageerror',e=>errors.push(e.message))
    let showReloadControl = false
    const start=new Date(Date.now()-20000).toISOString(), end=new Date(Date.now()-1000).toISOString()
    const record:CompletedEvent={recordId:'cycle-one',kind:'sentinel',subject:'service-one',cycleOpenedAt:start,completedAt:end,completionState:'verified_completed',canArchive:true,canReflect:true,archived:false,scope:'production_observed'}
    await page.route('**/api/rca/**',async route=>{
      const req=route.request(),url=new URL(req.url())
      if(req.method()==='POST'){
        writes.push(url.pathname)
        expect(req.postDataJSON()).toEqual({recordId:'cycle-one'})
        if(url.pathname.endsWith('/reflect'))record.memoryId='memory-one'
        else if(url.pathname.endsWith('/archive'))record.archived=true
        return route.fulfill({json:{ok:true,record,memoryId:record.memoryId,retrievalReady:true}})
      }
      if(url.pathname.endsWith('/completed-events'))return route.fulfill({json:{records:[record]}})
      if(url.pathname.endsWith('/timeline'))return route.fulfill({json:{events:[{subject:record.subject,kind:'detected',at:new Date(Date.parse(start)+5000).toISOString(),cycle_opened_at:start,summary:'Service unavailable'},{subject:record.subject,kind:'resolved',at:end,note:'Readback and observation passed'},...(showReloadControl ? [{subject:'reload-control',kind:'detected',at:end,summary:'Reloaded observation control'}] : [])]}})
      return route.fulfill({json:{suggestions:[{deviceKey:'service-one',impactLevel:'single-service',stageTelemetry:[{stageId:'preflight',detail:'old authorization pause'}]}]}})
    })
    await page.goto(base+'completed-test')
    await page.getByRole('button',{name:'Reflect & remember',exact:true}).waitFor()
    expect(writes).toHaveLength(0)
    await page.locator('.la-row').click()
    expect(await page.evaluate(() => (window as unknown as {opened: [string, {blastSummary: string}][]}).opened[0][1].blastSummary)).toBe('single-service')
    await page.getByRole('button',{name:'Reflect & remember',exact:true}).click()
    await page.getByRole('button',{name:'✓ Saved to long-term memory',exact:true}).waitFor()
    expect(await page.getByRole('button',{name:'✓ Saved to long-term memory',exact:true}).isDisabled()).toBe(true)
    await page.getByRole('button',{name:'Delete',exact:true}).click()
    await page.getByRole('status').filter({hasText:'Removed from this list'}).waitFor()
    expect(await page.locator('.la-row').count()).toBe(0)
    showReloadControl = true
    await page.reload()
    await page.locator('.la-row').filter({hasText:'reload-control'}).waitFor()
    expect(await page.locator('.la-row').filter({hasText:'service-one'}).count()).toBe(0)
    expect(writes).toEqual(['/api/rca/completed-events/reflect','/api/rca/completed-events/archive'])
    expect(errors).toEqual([])
    await page.close()
  },30000)
})
