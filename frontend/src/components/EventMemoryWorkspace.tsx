import { useEffect, useState } from 'react'
import type { Lang } from '../i18n'
import { LiveMemoryGraph } from './LiveMemoryGraph'
import type { GraphReceipt, GraphSession } from './LiveMemoryGraph'
import { useInvestigationCase } from './investigation-case'

export function EventMemoryWorkspace({ subject: externalSubject, incidentRef: externalRef, lang, onTrace, onSourceChange }: {
  subject?: string; incidentRef?: string; lang: Lang; onTrace?: (subject: string, caseId?: string) => void
  onSourceChange?: (subject: string, caseId: string) => void
}) {
  const [receipt, setReceipt] = useState<GraphReceipt | null>(null)
  const [loadedScope, setLoadedScope] = useState('')
  const [session, setSession] = useState<GraphSession | null>(null)
  const [error, setError] = useState('')
  const [localSource, setLocalSource] = useState<{ subject: string; caseId: string } | null>(null)
  const subject = localSource?.subject ?? externalSubject
  const incidentRef = localSource?.caseId ?? externalRef
  // A case that recorded a memory comparison names the session that decided
  // it; the observatory follows that session rather than the case's first one.
  const { record: caseRecord } = useInvestigationCase(incidentRef, 8_000)
  const pinnedSession = (() => {
    if (!caseRecord) return undefined
    const comparison = [...caseRecord.timeline].reverse().find(event => event.kind === 'memory_comparison_recorded')
    const payload = comparison?.payload && typeof comparison.payload === 'object' ? comparison.payload as Record<string, unknown> : {}
    return String(payload.sessionId ?? comparison?.sessionId ?? caseRecord.businessDecision?.sessionId ?? '') || undefined
  })()
  const scopeKey = JSON.stringify([subject, incidentRef, pinnedSession])
  useEffect(() => {
    if (!subject) return
    const abort = new AbortController(); let timer: number | undefined
    const read = async () => {
      try {
        const query = new URLSearchParams({ subject, ...(incidentRef ? { incident_ref: incidentRef } : {}), ...(pinnedSession ? { session_id: pinnedSession } : {}) })
        const r = await fetch('/api/rca/event-memory-receipt?' + query, { signal: abort.signal })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const next = await r.json() as GraphReceipt
        if (next.subject !== subject) throw new Error('Memory receipt does not match the selected subject')
        let detail: GraphSession | null = null
        if (next.investigation) {
          const s = await fetch('/api/rca/investigate/' + encodeURIComponent(next.investigation.session_id), { signal: abort.signal })
          if (!s.ok) throw new Error(`Session HTTP ${s.status}`)
          detail = await s.json() as GraphSession
          // The session must be the one the receipt named and be about this
          // subject; its own case may be an internal round of the selected case.
          if (detail.session_id !== next.investigation.session_id || detail.subject !== subject) throw new Error('Investigation session does not match the selected case')
        }
        if (!abort.signal.aborted) { setReceipt(next); setSession(detail); setLoadedScope(scopeKey); setError('') }
      } catch (e) { if (!abort.signal.aborted) setError(String(e)) }
      finally { if (!abort.signal.aborted) timer = window.setTimeout(read, 5000) }
    }
    void read(); return () => { abort.abort(); if (timer !== undefined) window.clearTimeout(timer) }
  }, [subject, incidentRef, pinnedSession, scopeKey])
  return <>
    {error ? <p role="alert">{error}</p> : null}
    <div className="lmg-navigation">{subject && onTrace ? <button onClick={() => onTrace(subject, incidentRef)}>{lang === 'zh' ? '打开处置记录 →' : 'Open actions →'}</button> : null}
    {localSource ? <button onClick={() => { setLocalSource(null); setReceipt(null); setSession(null) }}>{externalSubject ? (lang === 'zh' ? '返回所选事件' : 'Back to selected case') : (lang === 'zh' ? '返回全库图' : 'Back to all memories')}</button> : null}</div>
    <LiveMemoryGraph key={`${subject ?? 'all'}:${incidentRef ?? ''}`} lang={lang} subject={subject} receipt={loadedScope === scopeKey && receipt?.subject === subject ? receipt : null} session={loadedScope === scopeKey ? session : null} onOpenSource={async id => {
      try {
        const r = await fetch('/api/rca/investigate/' + encodeURIComponent(id))
        if (!r.ok) throw new Error(lang === 'zh' ? '该来源没有可打开的调查会话；保留原始来源 ID。' : 'No investigation session is available for this source ID.')
        const s = await r.json() as GraphSession
        if (s.session_id !== id || !s.case_id) throw new Error(lang === 'zh' ? '来源会话缺少精确事件关联' : 'The source session has no exact case association')
        if (onSourceChange) onSourceChange(s.subject, s.case_id)
        else setLocalSource({ subject: s.subject, caseId: s.case_id })
        setError('')
      } catch (e) { setError(String(e)) }
    }} />
  </>
}
