export const INVESTIGATION_CONTEXT_STORAGE_KEY = 'autopoiesis.investigation-context.v1'

export type InvestigationIntent =
  | 'event_facts'
  | 'impact_scope'
  | 'evidence_hypotheses'
  | 'retrieval_evidence'
  | 'remediation_readback'
  | 'related_memory'

export type InvestigationView =
  | 'investigation'
  | 'console'
  | 'constellation'
  | 'overview'
  | 'trajectory'
  | 'diagnose'
  | 'retrieval'

export interface InvestigationSourceEventInput {
  id?: string | null
  kind?: string | null
  observedAt?: string | null
  summary?: string | null
  severity?: string | null
  scope?: string | null
}

export interface InvestigationSourceEvent {
  id: string | null
  kind: string | null
  observedAt: string | null
  summary: string | null
  severity: string | null
  scope: string | null
}

export interface InvestigationVisit {
  view: InvestigationView
  intent: InvestigationIntent
  visitedAt: string
}

export interface InvestigationContext {
  version: 1
  caseId: string | null
  subject: string
  sourceEvent: InvestigationSourceEvent
  timeWindow: { start: string | null; end: string | null }
  currentIntent: InvestigationIntent
  visitedViews: InvestigationVisit[]
  updatedAt: string
}

export const INVESTIGATION_INTENTS: InvestigationIntent[] = [
  'event_facts',
  'impact_scope',
  'retrieval_evidence',
  'remediation_readback',
  'related_memory',
]

const INTENT_VIEW: Record<InvestigationIntent, InvestigationView> = {
  event_facts: 'investigation',
  impact_scope: 'console',
  evidence_hypotheses: 'investigation',
  retrieval_evidence: 'retrieval',
  remediation_readback: 'diagnose',
  related_memory: 'trajectory',
}

const isoOrNull = (value?: string | null): string | null => {
  if (!value) return null
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? null : parsed.toISOString()
}

const timeWindow = (observedAt?: string | null): { start: string | null; end: string | null } => {
  const normalized = isoOrNull(observedAt)
  if (!normalized) return { start: null, end: null }
  const center = new Date(normalized).valueOf()
  return {
    start: new Date(center - 5 * 60_000).toISOString(),
    end: new Date(center + 5 * 60_000).toISOString(),
  }
}

export function investigationView(intent: InvestigationIntent): InvestigationView {
  return INTENT_VIEW[intent]
}

export function adjacentIntent(
  intent: InvestigationIntent,
  direction: -1 | 1,
): InvestigationIntent | null {
  const index = INVESTIGATION_INTENTS.indexOf(intent === 'evidence_hypotheses' ? 'event_facts' : intent)
  return INVESTIGATION_INTENTS[index + direction] ?? null
}

export function createInvestigationContext({
  caseId,
  subject,
  sourceEvent = {},
  originView,
  intent = 'event_facts',
  now = new Date().toISOString(),
}: {
  caseId?: string | null
  subject: string
  sourceEvent?: InvestigationSourceEventInput
  originView: InvestigationView
  intent?: InvestigationIntent
  now?: string
}): InvestigationContext {
  const observedAt = isoOrNull(sourceEvent.observedAt)
  return {
    version: 1,
    caseId: caseId || null,
    subject,
    sourceEvent: {
      id: sourceEvent.id || caseId || null,
      kind: sourceEvent.kind || null,
      observedAt,
      summary: sourceEvent.summary || null,
      severity: sourceEvent.severity || null,
      scope: sourceEvent.scope || null,
    },
    timeWindow: timeWindow(observedAt),
    currentIntent: intent === 'evidence_hypotheses' ? 'event_facts' : intent,
    visitedViews: [{ view: originView, intent: intent === 'evidence_hypotheses' ? 'event_facts' : intent, visitedAt: now }],
    updatedAt: now,
  }
}

export function visitInvestigationContext(
  context: InvestigationContext,
  intent: InvestigationIntent,
  view = investigationView(intent),
  now = new Date().toISOString(),
): InvestigationContext {
  if (intent === 'evidence_hypotheses') intent = 'event_facts'
  const latest = context.visitedViews.at(-1)
  const nextVisit = latest?.view === view && latest.intent === intent
    ? context.visitedViews
    : [...context.visitedViews, { view, intent, visitedAt: now }].slice(-30)
  return { ...context, currentIntent: intent, visitedViews: nextVisit, updatedAt: now }
}

const isIntent = (value: unknown): value is InvestigationIntent =>
  typeof value === 'string' && (value === 'evidence_hypotheses' || INVESTIGATION_INTENTS.includes(value as InvestigationIntent))

const isView = (value: unknown): value is InvestigationView =>
  typeof value === 'string' && ['investigation', 'console', 'constellation', 'overview', 'trajectory', 'diagnose', 'retrieval'].includes(value)

export function parseInvestigationContext(raw: string | null): InvestigationContext | null {
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Partial<InvestigationContext>
    if (value.version !== 1 || typeof value.subject !== 'string' || !value.subject || !isIntent(value.currentIntent)) return null
    if (!value.sourceEvent || !value.timeWindow || !Array.isArray(value.visitedViews)) return null
    const visits = value.visitedViews.filter((visit): visit is InvestigationVisit => Boolean(
      visit && isView(visit.view) && isIntent(visit.intent) && typeof visit.visitedAt === 'string',
    )).slice(-30)
    return {
      version: 1,
      caseId: typeof value.caseId === 'string' ? value.caseId : null,
      subject: value.subject,
      sourceEvent: {
        id: typeof value.sourceEvent.id === 'string' ? value.sourceEvent.id : null,
        kind: typeof value.sourceEvent.kind === 'string' ? value.sourceEvent.kind : null,
        observedAt: isoOrNull(value.sourceEvent.observedAt),
        summary: typeof value.sourceEvent.summary === 'string' ? value.sourceEvent.summary : null,
        severity: typeof value.sourceEvent.severity === 'string' ? value.sourceEvent.severity : null,
        scope: typeof value.sourceEvent.scope === 'string' ? value.sourceEvent.scope : null,
      },
      timeWindow: {
        start: isoOrNull(value.timeWindow.start),
        end: isoOrNull(value.timeWindow.end),
      },
      currentIntent: value.currentIntent === 'evidence_hypotheses' ? 'event_facts' : value.currentIntent,
      visitedViews: visits,
      updatedAt: typeof value.updatedAt === 'string' ? value.updatedAt : new Date().toISOString(),
    }
  } catch {
    return null
  }
}

export function readInvestigationContext(): InvestigationContext | null {
  if (typeof window === 'undefined') return null
  try {
    return parseInvestigationContext(window.localStorage.getItem(INVESTIGATION_CONTEXT_STORAGE_KEY))
  } catch {
    return null
  }
}

export function writeInvestigationContext(context: InvestigationContext | null): void {
  if (typeof window === 'undefined') return
  try {
    if (context) window.localStorage.setItem(INVESTIGATION_CONTEXT_STORAGE_KEY, JSON.stringify(context))
    else window.localStorage.removeItem(INVESTIGATION_CONTEXT_STORAGE_KEY)
  } catch {
    // Navigation remains usable when storage is disabled by the browser.
  }
}
