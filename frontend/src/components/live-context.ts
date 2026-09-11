export type ContextStep = { at: string; kind: string; payload: unknown }
export type ContextRow = { item_id: string; selected_for_context?: boolean; title?: string; summary?: string; [key: string]: unknown }
/** Step payloads name candidates by id only; the session's final retrieval
 * results carry the title, summary, kind and reasons for each of them. */
export function enrichRows(rows: ContextRow[], catalog: ContextRow[] = []): ContextRow[] {
  if (!catalog.length) return rows
  const byId = new Map(catalog.map(item => [item.item_id, item]))
  return rows.map(row => {
    const known = byId.get(row.item_id)
    return known ? { ...known, ...row, title: row.title ?? known.title, summary: row.summary ?? known.summary } : row
  })
}
export function contextAt(steps: ContextStep[], catalog: ContextRow[] = []) {
  const snapshots = steps.flatMap(step => {
    const p = step.payload as Record<string, unknown> | null
    if (!p) return []
    const rows = step.kind === 'context_compiled' ? p.retrieval_results : step.kind === 'memory_candidates_ranked' ? p.results
      : step.kind === 'retrieval_candidates_filtered' && Array.isArray(p.kept_ids) ? p.kept_ids.filter((id): id is string => typeof id === 'string').map(locator => ({ item_id: locator.replace(/^retrieval:(indexed_memory|knowledge_document):/, ''), locator, selected_for_context: false, candidate: true }))
      : null
    return Array.isArray(rows) ? [{ step, rows: enrichRows(rows.filter((r): r is ContextRow => !!r && typeof r.item_id === 'string'), catalog), payload: p }] : []
  })
  const current = snapshots.at(-1), previous = snapshots.at(-2)
  const previousIds = new Set(previous?.rows.filter(r => r.selected_for_context).map(r => r.item_id))
  return { current, previous, added: new Set(current?.rows.filter(r => r.selected_for_context && !previousIds.has(r.item_id)).map(r => r.item_id)) }
}
