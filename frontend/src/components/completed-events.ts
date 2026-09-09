export interface CompletedEvent {
  recordId: string
  kind: 'sentinel' | 'case'
  subject: string
  cycleOpenedAt: string
  completedAt: string | null
  completionState: 'verified_completed' | 'condition_cleared' | 'in_progress'
  canArchive: boolean
  canReflect: boolean
  archived: boolean
  archivedAt?: string | null
  memoryId?: string | null
  scope: string
}

export function matchCompletedEvent(records: CompletedEvent[], subject: string, openedAt: string): CompletedEvent | undefined {
  return records.find(record => record.kind === 'sentinel' && record.subject === subject && record.cycleOpenedAt === openedAt)
}

export async function completedEventAction(action: 'archive' | 'reflect', recordId: string) {
  const response = await fetch(`/api/rca/completed-events/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Autopoiesis-Console': 'change-control' },
    body: JSON.stringify({ recordId }),
  })
  const result = await response.json()
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `HTTP ${response.status}`)
  return result as { memoryId?: string; record?: CompletedEvent; scope?: string; retrievalReady?: boolean }
}
