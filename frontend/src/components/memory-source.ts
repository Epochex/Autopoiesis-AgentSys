export interface MemorySourceCase {
  sourceId: string
  caseId: string
  subject: string
}
export class MemorySourceError extends Error {
  readonly reason: 'unlinked' | 'unavailable' | 'mismatch'
  constructor(reason: 'unlinked' | 'unavailable' | 'mismatch') { super(reason); this.reason = reason }
}

/** Resolve durable provenance; opaque trace IDs never become subjects or case IDs. */
export async function resolveMemorySource(sourceId: string, signal?: AbortSignal): Promise<MemorySourceCase> {
  const read = async (url: string) => {
    const response = await fetch(url, { signal, headers: { Accept: 'application/json' } })
    if (!response.ok) throw new MemorySourceError(response.status === 404 ? 'unlinked' : 'unavailable')
    return response.json() as Promise<Record<string, unknown>>
  }
  let caseId = sourceId
  if (!sourceId.startsWith('case-')) {
    const session = await read(`/api/rca/investigate/${encodeURIComponent(sourceId)}`)
    if (session.session_id !== sourceId) throw new MemorySourceError('mismatch')
    if (typeof session.case_id !== 'string' || !session.case_id) throw new MemorySourceError('unlinked')
    caseId = session.case_id
  }
  const result = await read(`/api/rca/investigation-cases/${encodeURIComponent(caseId)}`)
  const record = result.case as Record<string, unknown> | undefined
  if (!record || record.caseId !== caseId || typeof record.subject !== 'string' || !record.subject) throw new MemorySourceError('mismatch')
  return { sourceId, caseId, subject: record.subject }
}
