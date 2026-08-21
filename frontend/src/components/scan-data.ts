/* ──────────────────────────────────────────────────────────────────────────
 * The shape of GET /api/rca/scan and the few derivations we are allowed to
 * make from it.
 *
 * The one rule: if the payload does not contain it, we do not compute it here
 * and we do not draw it. The scan is a static read-only report — there is no
 * approve/execute/readback state machine behind this endpoint — so nothing
 * here models "pending approval" as a live status.
 *
 * The exposure findings this serves are joined into the fault-family catalog
 * (see fault-catalog.ts) rather than rendered as their own scored list: a
 * CVSS-like number told the reader nothing they could act on, and which
 * family an exposure belongs to decides who is allowed to fix it.
 * ────────────────────────────────────────────────────────────────────────── */

export type Kind = 'critical_cve' | 'exposed_admin' | 'exposed_db' | 'weak_tls' | 'info'

export interface Svc { port: number | string; service: string; banner: string }
export interface SurfaceHost { host: string; ip: string; services: Svc[]; exposure: string; risk_score: number }

export interface Finding {
  id: string
  host: string
  kind: Kind
  severity: number
  title: string
  evidence_ids: string[]
  requires_approval: boolean
}

export interface Probe { skill: string; readonly: boolean; evidence_ids: string[] }

export interface ScanResp {
  ok: boolean
  target: { label: string; cidr: string }
  surface: SurfaceHost[]
  findings: Finding[]
  probes: Probe[]
}

const KIND: Record<Kind, { zh: string; en: string }> = {
  critical_cve:  { zh: '严重漏洞',   en: 'critical cve' },
  exposed_admin: { zh: '管理口敞开', en: 'admin port open' },
  exposed_db:    { zh: '数据库敞开', en: 'database open' },
  weak_tls:      { zh: '加密太弱',   en: 'weak tls' },
  info:          { zh: '常规',       en: 'info only' },
}
export const kindLabel = (k: Kind, zh: boolean) =>
  KIND[k] ? (zh ? KIND[k].zh : KIND[k].en) : String(k).replace(/_/g, ' ')

/** the open ports a host is exposing, for the catalog hit chip */
export const portsOf = (host: SurfaceHost | undefined) =>
  (host?.services ?? []).map((s) => s.port).join(' · ')

/* ── is the target a reserved documentation range? ───────────────────────────
   RFC 5737 reserves 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24 for
   documentation; they are not routable and cannot be a real estate.
   `target.cidr` is built from the fixture's own IPs, so this is a CHECK on
   real payload data, not a label we assert: repoint the backend at a live
   range and this returns false, so the badge disappears rather than lying. */
const RFC5737 = ['192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24']
export function isDocRange(cidr: string | undefined): boolean {
  const nets = (cidr ?? '').split(',').map((s) => s.trim()).filter(Boolean)
  return nets.length > 0 && nets.every((n) => RFC5737.includes(n))
}
