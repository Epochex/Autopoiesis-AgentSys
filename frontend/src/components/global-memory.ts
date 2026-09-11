import type { LiveMemoryEvent, LiveMemoryEventsResponse, LiveMemoryListRecord, LiveMemoryListResponse } from '../types'
export type GlobalMemorySnapshot = { records: LiveMemoryListRecord[]; events: LiveMemoryEvent[]; inventory: LiveMemoryListResponse; total: number; after: number; syncedAt: string }
type InventoryPage = LiveMemoryListResponse & { total?: number; next_offset?: number | null }
async function read<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json() as Promise<T>
}
export async function loadGlobalMemory(signal: AbortSignal, previous?: GlobalMemorySnapshot | null): Promise<GlobalMemorySnapshot> {
  const records = new Map<string, LiveMemoryListRecord>()
  let inventory: InventoryPage | undefined
  let offset = 0
  do {
    const page = await read<InventoryPage>(`/api/rca/memory?limit=250&offset=${offset}&include_quarantined=true`, signal)
    if (!page.ok || !page.durable) throw new Error('MEMORY_UNAVAILABLE')
    inventory ??= page
    page.records.forEach(record => records.set(record.memory_id, record))
    if (page.next_offset == null) break
    if (page.next_offset <= offset) throw new Error('INVALID_CURSOR')
    offset = page.next_offset
  } while (!signal.aborted)
  const events = new Map((previous?.events ?? []).map(event => [event.offset, event]))
  let after = previous?.after ?? 0
  for (;;) {
    const page = await read<LiveMemoryEventsResponse>(`/api/rca/memory/events?after=${after}&limit=2000`, signal)
    if (!page.ok || !page.durable) throw new Error('MEMORY_UNAVAILABLE')
    page.events.forEach(event => events.set(event.offset, event))
    if (page.next_offset == null) { after = Math.max(after, page.high_water ?? after, ...page.events.map(event => event.offset)); break }
    if (page.next_offset <= after) throw new Error('INVALID_CURSOR')
    after = page.next_offset
  }
  return { records: [...records.values()], events: [...events.values()].sort((a, b) => a.offset - b.offset), inventory: inventory as unknown as LiveMemoryListResponse, total: inventory.total ?? Object.values(inventory.counts).reduce((sum, value) => sum + value, 0), after, syncedAt: new Date().toISOString() }
}
export function filterGlobalMemory(records: LiveMemoryListRecord[], query: string, tier: string, state: string) {
  const search = query.trim().toLowerCase()
  return records.filter(record => (!tier || record.tier === tier) && (state === 'all' || (state === 'quarantined' ? record.quarantined : !record.quarantined)) && (!search || [record.memory_id, record.text, ...record.asset_ids, ...record.tags].some(value => value.toLowerCase().includes(search))))
}
