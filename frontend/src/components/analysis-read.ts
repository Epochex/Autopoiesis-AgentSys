export type AnalysisMetadata = {
  analysisState?: 'baseline' | 'cached' | 'fresh'
  analysisAvailable?: boolean
  analysisError?: string
  cacheReading?: boolean
  cache?: { status: 'miss' | 'hit' | 'stale' | 'generated'; analyzedAt?: string | null; expiresAt?: string | null; evidenceChanged?: boolean }
  facts?: Record<string, unknown>
  riskAssessment?: { attackAttempt?: unknown; blocked?: unknown; successfulIntrusion?: unknown; businessImpact?: unknown; basis?: unknown }
}
export type AnalysisMode = 'read' | 'analyze'
export function analysisUrl(scope: 'wan' | 'host' | 'subnet', subject: string, lang: string, mode: AnalysisMode = 'read', force = false) {
  const query = new URLSearchParams({lang, mode, force: String(force)})
  if (scope === 'wan') { query.set('ip', subject); query.set('source', 'production'); return `/api/rca/wan_threat?${query}` }
  query.set('scope', scope); query.set('subject', subject)
  return `/api/rca/production_analysis?${query}`
}
export async function readAnalysis(scope: 'wan' | 'host' | 'subnet', subject: string, lang: string, mode: AnalysisMode = 'read', force = false) {
  const response = await fetch(analysisUrl(scope, subject, lang, mode, force))
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  const result = await response.json()
  if (!result.ok) throw new Error(result.text || 'Analysis unavailable')
  return result
}

export function observationFor(facts: Record<string, unknown> | undefined, ip: string, field: 'selected' | 'sameNetblock' = 'selected'): Record<string, unknown> | undefined {
  const rows = facts?.[field]
  if (!Array.isArray(rows)) return undefined
  return rows.find((row): row is Record<string, unknown> => row !== null && typeof row === 'object' && row.ip === ip)
}
export function observedCount(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined
}
