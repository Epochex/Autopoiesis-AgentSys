import { useEffect, useState } from 'react'
import type { Lang } from '../i18n'
import { LiveMemoryGraph } from './LiveMemoryGraph'
import type { GraphReceipt, GraphSession } from './LiveMemoryGraph'

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
  const scopeKey = JSON.stringify([subject, incidentRef])
  useEffect(() => {
    if (!subject) return
    const abort = new AbortController(); let timer: number | undefined
    const read = async () => {
      try {
        const query = new URLSearchParams({ subject, ...(incidentRef ? { incident_ref: incidentRef } : {}) })
        const r = await fetch('/api/rca/event-memory-receipt?' + query, { signal: abort.signal })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const next = await r.json() as GraphReceipt
        if (next.subject !== subject) throw new Error('Memory receipt does not match the selected subject')
        let detail: GraphSession | null = null
        if (next.investigation) {
          const s = await fetch('/api/rca/investigate/' + encodeURIComponent(next.investigation.session_id), { signal: abort.signal })
          if (!s.ok) throw new Error(`Session HTTP ${s.status}`)
          detail = await s.json() as GraphSession
          if (detail.session_id !== next.investigation.session_id || (incidentRef ? detail.case_id !== incidentRef : detail.subject !== subject)) throw new Error('Investigation session does not match the selected case')
        }
        if (!abort.signal.aborted) { setReceipt(next); setSession(detail); setLoadedScope(scopeKey); setError('') }
      } catch (e) { if (!abort.signal.aborted) setError(String(e)) }
      finally { if (!abort.signal.aborted) timer = window.setTimeout(read, 5000) }
    }
    void read(); return () => { abort.abort(); if (timer !== undefined) window.clearTimeout(timer) }
  }, [subject, incidentRef, scopeKey])
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
