const CACHE_KEY = 'autopoiesis.production-overview.v1'
const CACHE_VERSION = 1

interface CacheEnvelope<T> {
  version: number
  savedAt: string
  data: T
}

export function readProductionOverviewCache<T>(): T | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const envelope = JSON.parse(raw) as CacheEnvelope<T>
    const data = envelope?.data as Record<string, unknown> | undefined
    if (
      envelope.version !== CACHE_VERSION
      || !data
      || data.ok !== true
      || data.mode !== 'production_observed'
      || typeof data.observedAt !== 'string'
    ) return null
    return envelope.data
  } catch {
    return null
  }
}

export function writeProductionOverviewCache<T>(data: T): void {
  if (typeof window === 'undefined') return
  try {
    const envelope: CacheEnvelope<T> = {
      version: CACHE_VERSION,
      savedAt: new Date().toISOString(),
      data,
    }
    window.localStorage.setItem(CACHE_KEY, JSON.stringify(envelope))
  } catch {
    // Storage may be disabled or full. The in-memory response remains usable.
  }
}
