import './diagnose.css'
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Lang } from '../i18n'
import type { EnvFinding, EnvironmentReport, IncidentSummary, OwnershipProbeResult, SubnetGraph } from '../types'
import type { Finding, ScanResp } from './scan-data'
import { isDocRange, kindLabel, portsOf } from './scan-data'
import { AttackSurfaceGraph } from './surface-graph'
import { AddressSpace, CoverageRail, OwnershipContention, OwnershipStable, ScopeStrip } from './environment-blocks'
import { FAULT_LABEL, VERIFY_LABEL, pick } from './environment-labels'
import {
  AUTOMATION_LABEL,
  FAULT_CATALOG,
  INCIDENT_FAMILY,
  STEP_LABEL,
  TAILSCALE_GUARD,
  type FaultFamily,
} from './fault-catalog'

/* ── 诊断处置 · the one page that answers "what is wrong on this network, and
 * who is allowed to fix it" ───────────────────────────────────────────────────
 *
 * The list is the fault-family catalog, ranked by how badly each family hurts.
 * Everything the sources produce — the environment sweep, the exposure scan,
 * the archived incidents — joins into the family it belongs to instead of
 * standing as its own scored list. Three parallel findings lists with three
 * verdict scales used to live here; the reader had to reconcile them to learn
 * one thing, so they are one ledger now.
 *
 * A family with no live hit still shows, on standby: the catalog is the fault
 * space of this network, and the live sources only decide which families are
 * firing right now — never which ones exist. */

type St = { s: 'load' } | { s: 'err'; m: string } | { s: 'ok'; d: ScanResp }

/** One ledger row: a catalog family joined with whatever the sources currently
 * confirm inside it. */
type CatRow = {
  fam: FaultFamily
  hits: EnvFinding[]
  exposures: Finding[]
  archived: IncidentSummary[]
  live: boolean
}

// Steps the console can actually execute in-process: the recon probes, not the
// FortiGate-CLI verify steps (which need a device login).
const RUNNABLE = /^(nc|nmap|curl|openssl)\s/

const T = (zh: boolean) => ({
  codeMock: zh ? '模拟靶场 · 只读扫描' : 'MOCK RANGE · READ-ONLY SCAN',
  codeReal: zh ? '内网真实资产 · 实时 + 全历史 · 只读' : 'REAL INTERNAL ASSETS · LIVE + FULL HISTORY · READ-ONLY',
  codeBench: zh ? 'ITBench · CISO 合规基准 · 公开定义' : 'ITBench · CISO COMPLIANCE · PUBLISHED DEFS',
  loading: zh ? '读取报告…' : 'READING REPORT…',
  offline: zh ? '报告不可用' : 'REPORT UNAVAILABLE',
  thesis: zh
    ? '这个网络会出的问题分成九族,按严重程度排下来。每族写清楚三件事:怎么确认它真的发生了、谁来修(系统自己修 / 有条件自动 / 必须人工)、以及具体每一步跑什么。系统只跑过只读的检查;所有会改状态的动作都停在审批之前。'
    : 'Every fault this network can have, folded into nine families and ranked by how badly each one hurts. Each says three things: how a hit gets confirmed, who is allowed to fix it (the system, the system under conditions, or a person), and the exact steps. Only read-only checks have ever run; everything that changes state stops before approval.',
  rfc: zh ? 'RFC 5737 文档保留网段 · 不可路由' : 'RFC 5737 DOC RANGE · NOT ROUTABLE',
  assets: zh ? '资产' : 'ASSETS',
  finds: zh ? '结论' : 'FINDINGS',
  confirmed: zh ? '实时确认' : 'CONFIRMED LIVE',
  retired: zh ? '已消失·移除' : 'RETIRED',
  blind: zh ? '盲区故障类' : 'BLIND CLASSES',
  surface: zh ? '开着哪些端口 · 资产' : 'OPEN PORTS BY ASSET',
  space: zh ? '地址空间占用' : 'ADDRESS-SPACE OCCUPANCY',
  ledger: zh ? '故障族 · 按严重程度排 · 点开看怎么确认、谁来修' : 'FAULT FAMILIES · WORST FIRST · EXPAND FOR HOW TO CONFIRM AND WHO FIXES IT',
  cov: zh ? '传感器覆盖 · 检测不到的必须点名' : 'SENSOR COVERAGE · WHAT CANNOT BE DETECTED IS NAMED',
  standby: zh ? '目前没发生' : 'NOT HAPPENING NOW',
  liveHits: zh ? '现在查到' : 'FOUND NOW',
  exposureHits: zh ? '扫到敞开的' : 'FOUND OPEN',
  archivedHits: zh ? '以前出过' : 'HAPPENED BEFORE',
  rationale: zh ? '为什么这么定' : 'WHY',
  protectedK: zh ? '不许动' : 'DO NOT TOUCH',
  probe: zh ? '运行归属探测' : 'RUN OWNERSHIP PROBE',
  probing: zh ? '探测中…' : 'PROBING…',
  run: zh ? '执行' : 'RUN',
  running: zh ? '执行中…' : 'RUNNING…',
  retiredNote: (n: number) =>
    zh ? `${n} 条以前的判定,这次复查已经不在了,从列表里去掉` : `${n} FINDINGS GONE ON RE-CHECK, DROPPED FROM THE LIST`,
})

/** Join the catalog with the sweep, the exposure scan and the archive. The
 * catalog is the full fault space; the sources only decide which families are
 * firing right now, never which exist. */
const joinCatalog = (
  envFindings: EnvFinding[],
  exposures: Finding[],
  incidents: IncidentSummary[],
): CatRow[] => {
  const rows = FAULT_CATALOG.map((fam) => {
    const hits = envFindings.filter((f) => fam.faultClasses.includes(f.fault_class))
    const exp = exposures.filter((f) => (fam.reconKinds ?? []).includes(f.kind))
    const archived = incidents.filter((i) => INCIDENT_FAMILY[i.id] === fam.id)
    return {
      fam,
      hits,
      exposures: exp,
      archived,
      live: hits.some((h) => h.verification.state === 'confirmed') || exp.length > 0,
    }
  })
  rows.sort((a, b) => Number(b.live) - Number(a.live) || a.fam.prio - b.fam.prio)
  return rows
}

export function DiagnosePage({ lang, scenario = 'live' }: { lang: Lang; scenario?: 'live' | 'bench' }) {
  const zh = lang === 'zh'
  const tx = T(zh)
  const [st, setSt] = useState<St>({ s: 'load' })
  const [envData, setEnv] = useState<EnvironmentReport | null>(null)
  const [incidentData, setIncidents] = useState<IncidentSummary[]>([])
  const [openRow, setOpenRow] = useState<string | null>(null)
  const [focusIp, setFocusIp] = useState<string | null>(null)
  const [relGraph, setRelGraph] = useState<SubnetGraph | null>(null)
  const [ran, setRan] = useState<Record<string, { state: 'run' | 'done' | 'err'; text: string }>>({})
  const [probe, setProbe] = useState<{ ip: string; state: 'run' | 'done' | 'err'; data?: OwnershipProbeResult; m?: string } | null>(null)

  useEffect(() => {
    let alive = true
    setSt({ s: 'load' })
    fetch(`/api/rca/pentest?lang=${lang}&scenario=${scenario}`)
      .then((r) => r.json())
      .then((d: ScanResp) => { if (alive) setSt(d && d.ok ? { s: 'ok', d } : { s: 'err', m: 'NO DATA' }) })
      .catch((e) => { if (alive) setSt({ s: 'err', m: e instanceof Error ? e.message : String(e) }) })
    return () => { alive = false }
  }, [lang, scenario])

  // The environment sweep and the archived incidents describe the real network,
  // so they are only meaningful next to the live scenario.
  useEffect(() => {
    if (scenario !== 'live') return
    let alive = true
    fetch('/api/rca/environment', { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: EnvironmentReport | null) => { if (alive) setEnv(d) })
      .catch(() => { if (alive) setEnv(null) })
    fetch('/api/rca/incidents', { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d?.incidents) setIncidents(d.incidents) })
      .catch(() => { if (alive) setIncidents([]) })
    return () => { alive = false }
  }, [scenario])

  // The sweep and the archived incidents describe the real network, so they are
  // only read next to the live scenario — gated here rather than cleared in the
  // effect, which would set state synchronously on every scenario change.
  const env = scenario === 'live' ? envData : null
  const incidents = useMemo(() => (scenario === 'live' ? incidentData : []), [scenario, incidentData])

  const d = st.s === 'ok' ? st.d : null
  const surface = useMemo(() => d?.surface ?? [], [d])
  const targetCidr = d?.target?.cidr ?? ''
  const findings = useMemo(() => d?.findings ?? [], [d])
  const docRange = isDocRange(d?.target?.cidr)

  // The relations are already mined per subnet; the graph just draws them.
  useEffect(() => {
    if (!targetCidr || !/^\d+\.\d+\.\d+\.\d+\/\d+$/.test(targetCidr)) return
    let alive = true
    fetch(`/api/rca/subnet_graph?cidr=${encodeURIComponent(targetCidr)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((g) => { if (alive && g?.ok) setRelGraph(g as SubnetGraph) })
      .catch(() => {})
    return () => { alive = false }
  }, [targetCidr])

  const rows = useMemo(() => {
    const joined = joinCatalog(env?.findings ?? [], findings, incidents)
    if (!focusIp) return joined
    const hostIp = (f: Finding) => surface.find((h) => h.host === f.host)?.ip
    return joined.filter((row) =>
      row.hits.some((h) => h.subject === focusIp)
      || row.exposures.some((f) => hostIp(f) === focusIp)
      || row.archived.some((i) => i.asset?.ip === focusIp))
  }, [env, findings, incidents, surface, focusIp])

  const contested = useMemo(
    () => (env?.findings ?? []).filter((f) => f.fault_class === 'duplicate_ip_static'),
    [env],
  )

  // Every address, by ip, so a clicked cell/node can drive the ownership panel
  // even when it has no drift history of its own.
  const cellOf = useMemo(() => {
    const map = new Map<string, import('../types').EnvCell>()
    for (const seg of env?.address_space ?? []) for (const c of seg.cells) map.set(c.ip, c)
    return map
  }, [env])
  const focusDrift = focusIp ? contested.find((f) => f.subject === focusIp) ?? null : null
  const focusCell = focusIp ? cellOf.get(focusIp) ?? null : null

  const m = useMemo(() => ({
    assets: surface.length,
    finds: findings.length + (env?.totals.findings ?? 0) + incidents.length,
    confirmed: env?.totals.by_verification?.confirmed ?? 0,
    retired: env?.totals.resolved_dropped ?? 0,
    blind: env?.totals.blind_classes ?? 0,
  }), [surface, findings, env, incidents])

  const runStep = useCallback((key: string, command: string) => {
    setRan((cur) => ({ ...cur, [key]: { state: 'run', text: '' } }))
    fetch('/api/rca/environment/run-step', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command }),
    })
      .then(async (r) => ({ ok: r.ok, body: await r.json() }))
      .then(({ ok, body }) => {
        const text = ok
          ? `$ ${command}\n${body.output ?? body.reason ?? ''}`.trim()
          : `${body.detail ?? body.reason ?? 'refused'}`
        setRan((cur) => ({ ...cur, [key]: { state: ok ? 'done' : 'err', text } }))
      })
      .catch((e: unknown) =>
        setRan((cur) => ({ ...cur, [key]: { state: 'err', text: e instanceof Error ? e.message : String(e) } })),
      )
  }, [])

  const runProbe = useCallback((ip: string) => {
    setProbe({ ip, state: 'run' })
    fetch('/api/rca/environment/probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip, samples: 16 }),
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`gateway ${r.status}`))))
      .then((data: OwnershipProbeResult) => setProbe({ ip, state: 'done', data }))
      .catch((e: unknown) => setProbe({ ip, state: 'err', m: e instanceof Error ? e.message : String(e) }))
  }, [])

  return (
    <div className="dx">
      <header className="dx-head">
        <div className="dx-head-l">
          <div className="dx-head-code"><span>{d?.target?.cidr?.startsWith('itbench') ? tx.codeBench : docRange ? tx.codeMock : tx.codeReal}</span></div>
          <h1 className="dx-head-title">{zh ? <>诊断<mark>处置</mark></> : <>DIAGNOSE &amp; <mark>FIX</mark></>}</h1>
          <p className="dx-thesis">{tx.thesis}</p>
          <div className="dx-target">
            <b>{d?.target?.label ?? '—'}</b><span>{d?.target?.cidr ?? '—'}</span>
            {docRange ? <span className="dx-rfc"><span className="dx-lock" />{tx.rfc}</span> : null}
          </div>
        </div>
        {d ? (
          <div className="dx-metrics">
            <div className="dx-metric"><b>{m.assets}</b><span>{tx.assets}</span></div>
            <div className="dx-metric"><b>{m.finds}</b><span>{tx.finds}</span></div>
            <div className="dx-metric crit"><b>{m.confirmed}</b><span>{tx.confirmed}</span></div>
            <div className="dx-metric gate"><b>{m.blind}</b><span>{tx.blind}</span></div>
          </div>
        ) : null}
      </header>

      {st.s === 'load' ? <div className="dx-state">{tx.loading}</div>
        : st.s === 'err' ? <div className="dx-state">{tx.offline} · {st.m}</div>
        : (
          <>
            {env ? <ScopeStrip sources={env.sources} zh={zh} /> : null}
            {env && m.retired ? <div className="dx-retired">{tx.retiredNote(m.retired)}</div> : null}

            {surface.length ? (
              <section className="dx-sec">
                <div className="dx-sec-k">{tx.surface}</div>
                <AttackSurfaceGraph
                  graph={relGraph}
                  surface={surface}
                  segments={env?.address_space ?? []}
                  zh={zh}
                  selected={focusIp}
                  onSelect={(ip) => setFocusIp((cur) => (cur === ip ? null : ip))}
                />
              </section>
            ) : null}

            {env?.address_space?.length ? (
              <section className="dx-sec">
                <div className="dx-sec-k">{tx.space}</div>
                <AddressSpace
                  segments={env.address_space}
                  zh={zh}
                  selected={focusIp}
                  onSelect={(ip) => setFocusIp((cur) => (cur === ip ? null : ip))}
                />
              </section>
            ) : null}

            {/* Ownership panel, driven by the current selection. A drifting
                address shows its step chart; any other clicked address states
                its stable/unbound status so the click always answers. With
                nothing selected, the known drifts show by default. */}
            {focusIp ? (
              <section className="dx-sec">
                {focusDrift ? (
                  <OwnershipContention finding={focusDrift} zh={zh} />
                ) : (
                  <OwnershipStable ip={focusIp} cell={focusCell} zh={zh} />
                )}
              </section>
            ) : (
              contested.map((finding) => (
                <section className="dx-sec" key={`own-${finding.finding_id}`}>
                  <OwnershipContention finding={finding} zh={zh} />
                </section>
              ))
            )}

            <section className="dx-sec">
              <div className="dx-sec-k">
                {tx.ledger}
                {focusIp ? (
                  <button type="button" className="dx-clear" onClick={() => setFocusIp(null)}>
                    {zh ? `按 ${focusIp} 过滤 · 清除` : `FILTERED BY ${focusIp} · CLEAR`}
                  </button>
                ) : null}
              </div>

              <div className="dx-guard">
                <span className="dx-lock" />
                <p>{zh ? TAILSCALE_GUARD[0] : TAILSCALE_GUARD[1]}</p>
              </div>

              {scenario === 'live' && rows.length ? (
                <div className="dx-ledger">
                  {rows.map((row) => {
                    const { fam } = row
                    const open = openRow === fam.id
                    const liveState = row.live
                      ? 'confirmed'
                      : row.hits.length ? row.hits[0].verification.state
                      : row.archived.length ? 'archived' : 'standby'
                    const probeHit = fam.id === 'fam-address-ownership'
                      ? row.hits.find((h) => h.fault_class === 'duplicate_ip_static') ?? null
                      : null
                    return (
                      <div className={`dx-row is-cat sev-${fam.severity} v-${liveState}${open ? ' is-open' : ''}`} key={fam.id}>
                        <button type="button" className="dx-row-h is-cat" onClick={() => setOpenRow(open ? null : fam.id)} aria-expanded={open}>
                          <span className="dx-row-p">P{fam.prio}</span>
                          <span className="dx-row-sev">{fam.severity}</span>
                          <span className={`dx-row-a a-${fam.automation}`}>{pick(AUTOMATION_LABEL[fam.automation], zh, fam.automation)}</span>
                          <span className="dx-row-v">
                            {liveState === 'standby' ? tx.standby : pick(VERIFY_LABEL[liveState], zh, liveState)}
                            {row.hits.length + row.exposures.length ? ` · ${row.hits.length + row.exposures.length}` : ''}
                          </span>
                          <span className="dx-row-t">{zh ? fam.title[0] : fam.title[1]}</span>
                          <span className="dx-row-s">{zh ? fam.confirm[0] : fam.confirm[1]}</span>
                        </button>
                        {open ? (
                          <div className="dx-row-b">
                            <p className="dx-row-note"><b>{tx.rationale}</b> — {zh ? fam.rationale[0] : fam.rationale[1]}</p>
                            {fam.protectedNote ? (
                              <div className="dx-prot">
                                <span className="dx-lock" />
                                <b>{tx.protectedK}</b>
                                <p>{zh ? fam.protectedNote[0] : fam.protectedNote[1]}</p>
                              </div>
                            ) : null}
                            {row.hits.length ? (
                              <div className="dx-hits">
                                <span>{tx.liveHits}</span>
                                {row.hits.map((hit) => (
                                  <em key={hit.finding_id} className={`v-${hit.verification.state}`}>
                                    {hit.subject} · {pick(FAULT_LABEL[hit.fault_class], zh, hit.fault_class)} · {pick(VERIFY_LABEL[hit.verification.state], zh, hit.verification.state)}
                                  </em>
                                ))}
                              </div>
                            ) : null}
                            {row.exposures.length ? (
                              <div className="dx-hits">
                                <span>{tx.exposureHits}</span>
                                {row.exposures.map((f) => {
                                  const host = surface.find((h) => h.host === f.host)
                                  const ports = portsOf(host)
                                  return (
                                    <em key={f.id} className="v-confirmed">
                                      {host?.ip ?? f.host} · {kindLabel(f.kind, zh)}{ports ? ` · ${ports}` : ''}
                                    </em>
                                  )
                                })}
                              </div>
                            ) : null}
                            {row.archived.length ? (
                              <div className="dx-hits">
                                <span>{tx.archivedHits}</span>
                                {row.archived.map((incident) => (
                                  <em key={incident.id} className="v-archived">{incident.asset?.ip ?? '—'} · {incident.title}</em>
                                ))}
                              </div>
                            ) : null}
                            <ol className="dx-pl">
                              {fam.playbook.map((step, index) => {
                                const runnable = step.risk === 'readonly' && RUNNABLE.test(step.command)
                                const key = `${fam.id}-${index}`
                                const out = ran[key]
                                return (
                                  <li className={`dx-pl-step is-${step.risk}`} key={key}>
                                    <div className="dx-pl-meta">
                                      <span className="dx-pl-risk">
                                        {step.risk === 'gated' ? <><span className="dx-lock" /> {pick(STEP_LABEL.gated, zh, 'gated')}</> : pick(STEP_LABEL[step.risk], zh, step.risk)}
                                      </span>
                                      <span className="dx-pl-what">{zh ? step.what[0] : step.what[1]}</span>
                                    </div>
                                    <div className="dx-pl-cmd">
                                      <code>{step.command}</code>
                                      {runnable ? (
                                        <button
                                          type="button"
                                          className="dx-pl-run"
                                          onClick={() => runStep(key, step.command)}
                                          disabled={out?.state === 'run'}
                                        >
                                          {out?.state === 'run' ? tx.running : tx.run}
                                        </button>
                                      ) : null}
                                    </div>
                                    {out && out.state !== 'run' ? (
                                      <pre className={`dx-pl-out${out.state === 'err' ? ' is-err' : ''}`}>{out.text}</pre>
                                    ) : null}
                                  </li>
                                )
                              })}
                            </ol>
                            {probeHit ? (
                              <div className="dx-probe">
                                <button type="button" onClick={() => runProbe(probeHit.subject)} disabled={probe?.state === 'run'}>
                                  {probe?.ip === probeHit.subject && probe.state === 'run' ? tx.probing : tx.probe}
                                </button>
                                {probe?.ip === probeHit.subject && probe.state === 'done' && probe.data ? (
                                  <div className="dx-probe-out">
                                    <b>{probe.data.verdict}</b>
                                    <span>{probe.data.verdict_note}</span>
                                    <div className="dx-probe-grid">
                                      {Object.entries(probe.data.service_profiles).map(([mac, ports]) => (
                                        <div key={mac}><dt>{mac}</dt><dd>{ports.join(' · ') || '—'}</dd></div>
                                      ))}
                                    </div>
                                    {Object.entries(probe.data.impact).map(([port, info]) => (
                                      <div className="dx-probe-imp" key={port}>
                                        {info.service} ({port}) —{' '}
                                        {zh
                                          ? `约 ${Math.round(info.unreachable_share * 100)}% 的时间不可达`
                                          : `unreachable ~${Math.round(info.unreachable_share * 100)}% of samples`}
                                      </div>
                                    ))}
                                  </div>
                                ) : null}
                                {probe?.ip === probeHit.subject && probe.state === 'err' ? <span className="dx-probe-err">{probe.m}</span> : null}
                              </div>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    )
                  })}
                </div>
              ) : null}
            </section>

            {env?.coverage?.length ? (
              <section className="dx-sec">
                <div className="dx-sec-k">{tx.cov}</div>
                <CoverageRail rows={env.coverage} zh={zh} />
              </section>
            ) : null}
          </>
        )}
    </div>
  )
}
