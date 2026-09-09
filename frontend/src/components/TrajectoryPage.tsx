import type { RcaCase, TheaterEvent } from '../types'
import type { Lang } from '../i18n'
import { EventMemoryWorkspace } from './EventMemoryWorkspace'
import './trajectory.css'

export function TrajectoryPage({ lang, onTrace, focusSubject, focusCaseId, onSourceChange }: {
  cases: RcaCase[]; lang: Lang; activeId: string; onPick: (id: string) => void
  onTheater?: (e: TheaterEvent) => void
  onTrace?: (subject: string, caseId?: string, event?: TheaterEvent) => void
  onSourceChange?: (subject: string, caseId: string) => void
  focusSubject?: string; focusCaseId?: string; focusArea?: 'live-memory'
}) {
  return <div className="traj-page">
    <EventMemoryWorkspace key={`${focusSubject ?? 'global'}:${focusCaseId ?? ''}`} lang={lang} subject={focusSubject} incidentRef={focusCaseId} onTrace={onTrace} onSourceChange={onSourceChange} />
  </div>
}
