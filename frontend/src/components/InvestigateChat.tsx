import './diagnose.css'
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import { pick } from './environment-labels'
import { FAULT_CATALOG, STEP_LABEL } from './fault-catalog'
import type { StepRisk } from './fault-catalog'
import {
  InvestigationContextReceipt,
  type InvestigationReceipt,
} from './InvestigationContextReceipt'
import {
  HypothesisRail,
  type HypothesisView,
  type ProbeRound,
} from './HypothesisRail'

/* ── 查一个故障 · the chat panel that works one fault end to end ──────────────
 *
 * The panel opens by running the read-only checks itself, so the first thing on
 * screen is what was actually seen, not a prompt box. Everything below that —
 * the verdict, the steps, the follow-up questions — cites those commands by id,
 * and a click on a citation lands on the command that produced it.
 *
 * The one rule that is not cosmetic: a step marked `gated` renders its button
 * disabled. There is no path through this panel that lets the machine press it. */

export interface Evidence {
  evidence_id: string
  command: string
  output: string
  ok: boolean
  at: string
}

export interface Step {
  n: number
  risk: StepRisk
  what: string
  command: string
  why: string
  /** The server decides this, not the risk label: only a step whose command
   *  passes the read-only allowlist has an executor on this path. */
  runnable?: boolean
}

interface StartResp {
  ok: boolean
  session_id: string
  question: string
  evidence: Evidence[]
  summary: string
  probe_candidates: string[]
  probe_prior: InvestigationReceipt['probe_prior']
  historical_context: InvestigationReceipt['historical_context']
  knowledge_context: InvestigationReceipt['knowledge_context']
  retrieval_results: InvestigationReceipt['retrieval_results']
  trace_events: InvestigationReceipt['trace_events']
  hypothesis_state: HypothesisView
  probe_rounds: ProbeRound[]
}

interface AnalyzeResp {
  ok: boolean
  diagnosis: string
  runbook: Step[]
  citations: string[]
  root_cause?: string
  follow_up_evidence?: Evidence[]
  hypothesis_state?: HypothesisView
  probe_rounds?: ProbeRound[]
  memory_commit?: { committed: boolean; dossier_id?: string; reason?: string }
  action_candidate?: ActionCandidate
}

interface ActionCandidate {
  eligible: boolean
  reason?: string
  action?: string
  target?: string
  root_hypothesis_id?: string
  root_statement?: string
  supporting_evidence_ids?: string[]
}

interface RemediateResp {
  ok: boolean
  ran: boolean
  outcome?: string
  case_status?: string
  reason?: string
  readback_evidence?: Evidence
  candidate?: ActionCandidate
}

interface CloseResp { ok: boolean; resolution: 'confirmed' | 'inconclusive' | 'refuted'; dossier: { dossier_id: string } }

interface AskResp {
  ok: boolean
  answer: string
  citations: string[]
  evidence: Evidence[]
  hypothesis_state?: HypothesisView
  probe_rounds?: ProbeRound[]
  memory_commit?: { committed: boolean; dossier_id?: string; reason?: string }
}

interface RunStepResp {
  ok: boolean
  ran: boolean
  output: string
  exit_code: number
  refused?: boolean
  reason?: string
}

/** run-all echoes each step either by number or in full; only the number is
 *  ever read, and the row it belongs to comes from the array position. */
interface RunAllItem {
  step: number | Step
  ran: boolean
  output: string
  refused?: boolean
  reason?: string
}

interface RunAllResp {
  ok: boolean
  results: RunAllItem[]
  stopped_at: number | null
}

type Busy = 'start' | 'analyze' | 'ask' | 'step' | 'all' | 'close' | 'remediate' | null

type StepOut = { state: 'run' | 'done' | 'err'; text: string; note?: string }

type Turn = { id: number; q: string; a: string; citations: string[]; state: 'run' | 'done' | 'err' }

type Verdict = { diagnosis: string; rootCause: string; runbook: Step[]; citations: string[] }

type Stop = { n: number; reason: string }

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

/** One place where a dead backend, an HTML error page and a refusal body all
 *  turn into the same thing: an Error carrying text worth showing. */
async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  })
  const raw = await res.text()
  let data: unknown = null
  try {
    data = raw ? JSON.parse(raw) : null
  } catch {
    data = null
  }
  if (!isRecord(data)) throw new Error(`${res.status} ${res.statusText}`.trim() || 'no response')
  if (!res.ok || data.ok === false) {
    const detail = [data.detail, data.reason, data.error].find((v) => typeof v === 'string')
    throw new Error(typeof detail === 'string' ? detail : `${res.status} ${res.statusText}`.trim())
  }
  return data as T
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e))

const stepNo = (ref: number | Step) => (typeof ref === 'number' ? ref : ref.n)

const merge = (cur: Evidence[], add: Evidence[]) => {
  const seen = new Set(cur.map((e) => e.evidence_id))
  const fresh = add.filter((e) => e.evidence_id && !seen.has(e.evidence_id))
  return fresh.length ? [...cur, ...fresh] : cur
}

const clock = (at: string) => at.replace('T', ' ').replace(/\..*$/, '').replace('Z', '')

const seedQuestion = (zh: boolean, family?: string, subject?: string) => {
  const fam = FAULT_CATALOG.find((f) => f.id === family)
  const title = fam ? (zh ? fam.title[0] : fam.title[1]) : ''
  if (zh) {
    if (subject && title) return `${subject}:${title} —— 现在是什么情况,该怎么办?`
    if (title) return `${title} —— 现在是什么情况,该怎么办?`
    if (subject) return `${subject} 现在是什么情况,该怎么办?`
    return '这个网络现在有什么问题,该怎么办?'
  }
  if (subject && title) return `${subject} — ${title}. What is going on, and what should be done?`
  if (title) return `${title}. What is going on, and what should be done?`
  if (subject) return `What is going on with ${subject}, and what should be done?`
  return 'What is wrong on this network right now, and what should be done?'
}

const T = (zh: boolean) => ({
  title: zh ? '查这一个故障' : 'INVESTIGATE ONE FAULT',
  lead: zh
    ? '先把该看的命令跑一遍,再让它给结论和步骤。每句话都挂着它依据的那条命令,点一下就跳过去。会改东西的步骤停在人工那一格,系统不会替你按。'
    : 'The checks run first, then it gives a verdict and the steps. Every claim carries the command it rests on — click it and you land on the output. Anything that changes state stops at the human step; the system will not press it for you.',
  ask: zh ? '要查什么' : 'WHAT TO LOOK INTO',
  askPh: zh ? '写一句话,说清要查哪台机器、什么现象' : 'One line: which machine, what you are seeing',
  start: zh ? '开始查' : 'START',
  starting: zh ? '正在查…' : 'COLLECTING…',
  ctx: zh ? '背景资料 · 系统已经跑过的命令' : 'BACKGROUND · COMMANDS ALREADY RUN',
  open: zh ? '展开' : 'OPEN',
  close: zh ? '收起' : 'CLOSE',
  count: (n: number) => (zh ? `本轮已收集 ${n} 条证据` : `${n} PIECES OF EVIDENCE SO FAR`),
  none: zh ? '还没有跑过命令' : 'NOTHING RUN YET',
  failed: zh ? '这一步没成' : 'THAT DID NOT GO THROUGH',
  analyze: zh ? '分析' : 'ANALYZE',
  analyzing: zh ? '分析中…' : 'ANALYZING…',
  hint: zh ? '读上面这些命令的输出,给结论和步骤' : 'READS THE OUTPUT ABOVE, RETURNS A VERDICT AND STEPS',
  verdict: zh ? '结论' : 'VERDICT',
  runbook: zh ? '接下来怎么做' : 'WHAT TO DO NEXT',
  all: zh ? '全部执行' : 'RUN ALL',
  allRunning: zh ? '执行中…' : 'RUNNING…',
  run: zh ? '执行' : 'RUN',
  running: zh ? '执行中…' : 'RUNNING…',
  gateTip: zh
    ? '这一步会动到还在正常工作的东西,必须由人来执行。系统只把命令写出来,按不下去。'
    : 'This step touches something that is still working, so a person runs it. The system writes the command out and stops there.',
  autoTip: zh
    ? '自动修复步骤在本界面锁定。当前前端没有处置入口,请交由运维按审批流程处理。'
    : 'Automatic remediation is locked in this interface. This frontend has no remediation entry point; use the approved operator process.',
  stopped: (n: number) => (zh ? `停在第 ${n} 步` : `STOPPED AT STEP ${n}`),
  refused: zh ? '没执行' : 'NOT RUN',
  exit: (code: number) => (zh ? `退出码 ${code}` : `EXIT ${code}`),
  chat: zh ? '接着问' : 'KEEP ASKING',
  chatLab: zh ? '你的问题' : 'YOUR QUESTION',
  chatPh: zh ? '再问一句' : 'Ask another question',
  send: zh ? '问' : 'SEND',
  sending: zh ? '正在问…' : 'ASKING…',
  you: zh ? '你问' : 'YOU',
  it: zh ? '回答' : 'ANSWER',
  cites: zh ? '依据' : 'FROM',
  ok: zh ? '有输出' : 'ANSWERED',
  bad: zh ? '没跑通' : 'FAILED',
  disposition: zh ? '人工确认 · 写入故障档案' : 'OPERATOR DISPOSITION · ARCHIVE DOSSIER',
  root: zh ? '根因' : 'ROOT CAUSE',
  operator: zh ? '确认人' : 'CONFIRMED BY',
  note: zh ? '处置备注' : 'OPERATOR NOTE',
  confirm: zh ? '确认根因' : 'CONFIRM ROOT',
  refute: zh ? '否定根因' : 'REFUTE ROOT',
  inconclusive: zh ? '证据不足并升级' : 'INCONCLUSIVE · ESCALATE',
  archiving: zh ? '正在归档…' : 'ARCHIVING…',
  archived: zh ? '已写入长期故障档案' : 'SAVED TO LONG-TERM DOSSIER',
})

export function InvestigateChat({ lang, family, subject, caseId }: { lang: Lang; family?: string; subject?: string; caseId?: string }) {
  const zh = lang === 'zh'
  const tx = useMemo(() => T(zh), [zh])
  const uid = useId()

  const [question, setQuestion] = useState(() => seedQuestion(zh, family, subject))
  const [draft, setDraft] = useState('')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [summary, setSummary] = useState('')
  const [receipt, setReceipt] = useState<InvestigationReceipt | null>(null)
  const [hypotheses, setHypotheses] = useState<HypothesisView | null>(null)
  const [probeRounds, setProbeRounds] = useState<ProbeRound[]>([])
  const [evidence, setEvidence] = useState<Evidence[]>([])
  const [verdict, setVerdict] = useState<Verdict | null>(null)
  const [stepOut, setStepOut] = useState<Record<number, StepOut>>({})
  const [stop, setStop] = useState<Stop | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [busy, setBusy] = useState<Busy>(null)
  const [err, setErr] = useState<string | null>(null)
  const [openCtx, setOpenCtx] = useState(false)
  const [flash, setFlash] = useState<{ id: string } | null>(null)
  const [rootDraft, setRootDraft] = useState('')
  const [operatorId, setOperatorId] = useState('')
  const [operatorNote, setOperatorNote] = useState('')
  const [archived, setArchived] = useState<string | null>(null)
  const [actionCandidate, setActionCandidate] = useState<ActionCandidate | null>(null)
  const [actionResult, setActionResult] = useState<RemediateResp | null>(null)

  // One request at a time: the session accumulates evidence server-side, so two
  // in flight would interleave into a context neither answer was written from.
  const busyRef = useRef(false)
  const aliveRef = useRef(true)
  const seqRef = useRef(0)
  const evRefs = useRef(new Map<string, HTMLLIElement>())
  const chatRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => () => { aliveRef.current = false }, [])

  const begin = useCallback(async (q: string) => {
    if (busyRef.current || !q.trim()) return
    busyRef.current = true
    setBusy('start')
    setErr(null)
    setSessionId(null)
    setSummary('')
    setReceipt(null)
    setHypotheses(null)
    setProbeRounds([])
    setEvidence([])
    setVerdict(null)
    setStepOut({})
    setStop(null)
    setTurns([])
    setRootDraft('')
    setArchived(null)
    setActionCandidate(null)
    setActionResult(null)
    try {
      const d = await post<StartResp>('/api/rca/investigate/start', {
        question: q,
        family,
        subject,
        case_id: caseId,
      })
      if (!aliveRef.current) return
      setSessionId(d.session_id)
      setSummary(d.summary ?? '')
      setEvidence(d.evidence ?? [])
      setHypotheses(d.hypothesis_state ?? null)
      setProbeRounds(d.probe_rounds ?? [])
      setReceipt({
        probe_candidates: d.probe_candidates ?? [],
        probe_prior: d.probe_prior ?? {},
        historical_context: d.historical_context ?? {},
        knowledge_context: d.knowledge_context ?? [],
        retrieval_results: d.retrieval_results ?? [],
        trace_events: d.trace_events ?? [],
      })
      setOpenCtx(false)
    } catch (e) {
      if (aliveRef.current) setErr(errText(e))
    } finally {
      busyRef.current = false
      if (aliveRef.current) setBusy(null)
    }
  }, [caseId, family, subject])

  // One session per fault. A language switch re-labels the panel; it must not
  // throw away the transcript, so the seed key ignores it.
  const seededRef = useRef<string | null>(null)
  useEffect(() => {
    const key = `${caseId ?? ''}|${family ?? ''}|${subject ?? ''}`
    if (seededRef.current === key) return
    seededRef.current = key
    const q = seedQuestion(zh, family, subject)
    setQuestion(q)
    void begin(q)
  }, [begin, caseId, family, subject, zh])

  const analyze = useCallback(async () => {
    if (busyRef.current || !sessionId) return
    busyRef.current = true
    setBusy('analyze')
    setErr(null)
    try {
      const d = await post<AnalyzeResp>('/api/rca/investigate/analyze', { session_id: sessionId })
      if (!aliveRef.current) return
      setVerdict({
        diagnosis: d.diagnosis ?? '', rootCause: d.root_cause ?? '',
        runbook: d.runbook ?? [], citations: d.citations ?? [],
      })
      setEvidence((cur) => merge(cur, d.follow_up_evidence ?? []))
      if (d.hypothesis_state) setHypotheses(d.hypothesis_state)
      if (d.probe_rounds) setProbeRounds(d.probe_rounds)
      if (d.memory_commit?.committed && d.memory_commit.dossier_id) {
        setArchived(d.memory_commit.dossier_id)
      }
      setActionCandidate(d.action_candidate ?? null)
      setActionResult(null)
      setRootDraft(d.root_cause ?? '')
      setStepOut({})
      setStop(null)
    } catch (e) {
      if (aliveRef.current) setErr(errText(e))
    } finally {
      busyRef.current = false
      if (aliveRef.current) setBusy(null)
    }
  }, [sessionId])

  const runRemediation = useCallback(async () => {
    if (busyRef.current || !sessionId || !actionCandidate?.eligible) return
    busyRef.current = true
    setBusy('remediate')
    setErr(null)
    try {
      const d = await post<RemediateResp>('/api/rca/investigate/remediate', { session_id: sessionId })
      if (!aliveRef.current) return
      setActionResult(d)
      if (d.readback_evidence) setEvidence((cur) => merge(cur, [d.readback_evidence as Evidence]))
    } catch (e) {
      if (aliveRef.current) setErr(errText(e))
    } finally {
      busyRef.current = false
      if (aliveRef.current) setBusy(null)
    }
  }, [actionCandidate, sessionId])

  const archive = useCallback(async (resolution: 'confirmed' | 'inconclusive' | 'refuted') => {
    if (busyRef.current || !sessionId || !verdict || !rootDraft.trim() || !operatorId.trim()) return
    busyRef.current = true
    setBusy('close')
    setErr(null)
    try {
      const d = await post<CloseResp>('/api/rca/investigate/close', {
        session_id: sessionId,
        resolution,
        root_cause: rootDraft,
        confirmed_by: operatorId,
        evidence_ids: verdict.citations,
        operator_note: operatorNote || null,
      })
      if (aliveRef.current) setArchived(d.dossier.dossier_id)
    } catch (e) {
      if (aliveRef.current) setErr(errText(e))
    } finally {
      busyRef.current = false
      if (aliveRef.current) setBusy(null)
    }
  }, [operatorId, operatorNote, rootDraft, sessionId, verdict])

  const runStep = useCallback(async (step: Step) => {
    if (busyRef.current || !sessionId) return
    busyRef.current = true
    setBusy('step')
    setErr(null)
    setStepOut((cur) => ({ ...cur, [step.n]: { state: 'run', text: '' } }))
    try {
      const d = await post<RunStepResp>('/api/rca/investigate/run-step', { session_id: sessionId, step: step.n })
      if (!aliveRef.current) return
      const blocked = d.refused === true || d.ran === false
      setStepOut((cur) => ({
        ...cur,
        [step.n]: {
          state: blocked ? 'err' : 'done',
          text: blocked ? '' : d.output ?? '',
          note: blocked ? d.reason ?? tx.refused : tx.exit(d.exit_code ?? 0),
        },
      }))
    } catch (e) {
      if (aliveRef.current) setStepOut((cur) => ({ ...cur, [step.n]: { state: 'err', text: '', note: errText(e) } }))
    } finally {
      busyRef.current = false
      if (aliveRef.current) setBusy(null)
    }
  }, [sessionId, tx])

  const runAll = useCallback(async () => {
    const book = verdict?.runbook ?? []
    if (busyRef.current || !sessionId || !book.length) return
    busyRef.current = true
    setBusy('all')
    setErr(null)
    setStop(null)
    setStepOut(Object.fromEntries(book.map((s) => [s.n, { state: 'run', text: '' } as StepOut])))
    try {
      const d = await post<RunAllResp>('/api/rca/investigate/run-all', { session_id: sessionId })
      if (!aliveRef.current) return
      const results = d.results ?? []
      const next: Record<number, StepOut> = {}
      results.forEach((item, index) => {
        const n = book[index]?.n ?? stepNo(item.step)
        const blocked = item.refused === true || item.ran === false
        next[n] = {
          state: blocked ? 'err' : 'done',
          text: blocked ? '' : item.output ?? '',
          note: blocked ? item.reason ?? tx.refused : undefined,
        }
      })
      setStepOut(next)
      // stopped_at may be the step number or its index into the runbook; the
      // refused row carries the same step either way, so read that first.
      const halted = results.findIndex((r) => r.refused === true || r.ran === false)
      if (halted >= 0) {
        setStop({ n: book[halted]?.n ?? halted + 1, reason: results[halted].reason ?? tx.refused })
      } else if (d.stopped_at != null) {
        const row = book.find((s) => s.n === d.stopped_at) ?? book[d.stopped_at]
        setStop({ n: row?.n ?? d.stopped_at, reason: tx.refused })
      }
    } catch (e) {
      if (aliveRef.current) {
        setErr(errText(e))
        setStepOut({})
      }
    } finally {
      busyRef.current = false
      if (aliveRef.current) setBusy(null)
    }
  }, [sessionId, tx, verdict])

  const ask = useCallback(async (q: string) => {
    if (busyRef.current || !sessionId || !q.trim()) return
    busyRef.current = true
    setBusy('ask')
    setErr(null)
    seqRef.current += 1
    const id = seqRef.current
    setTurns((cur) => [...cur, { id, q, a: '', citations: [], state: 'run' }])
    setDraft('')
    try {
      const d = await post<AskResp>('/api/rca/investigate/ask', { session_id: sessionId, question: q })
      if (!aliveRef.current) return
      setEvidence((cur) => merge(cur, d.evidence ?? []))
      if (d.hypothesis_state) setHypotheses(d.hypothesis_state)
      if (d.probe_rounds) setProbeRounds(d.probe_rounds)
      if (d.memory_commit?.committed && d.memory_commit.dossier_id) {
        setArchived(d.memory_commit.dossier_id)
      }
      setTurns((cur) =>
        cur.map((t) => (t.id === id ? { ...t, a: d.answer ?? '', citations: d.citations ?? [], state: 'done' } : t)))
    } catch (e) {
      if (aliveRef.current) {
        const m = errText(e)
        setTurns((cur) => cur.map((t) => (t.id === id ? { ...t, a: m, state: 'err' } : t)))
      }
    } finally {
      busyRef.current = false
      if (aliveRef.current) setBusy(null)
    }
  }, [sessionId])

  // The chip sets the target; the scroll waits for the list to be open.
  useEffect(() => {
    if (!flash) return
    const node = evRefs.current.get(flash.id)
    node?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    const timer = window.setTimeout(() => setFlash(null), 1800)
    return () => window.clearTimeout(timer)
  }, [flash])

  useEffect(() => {
    const box = chatRef.current
    if (box) box.scrollTop = box.scrollHeight
  }, [turns])

  const known = useMemo(() => new Set(evidence.map((e) => e.evidence_id)), [evidence])
  // A fresh object every time, so clicking the same chip twice scrolls twice.
  const jump = useCallback((id: string) => {
    setOpenCtx(true)
    setFlash({ id })
  }, [])

  const listId = `${uid}-ctx`
  const idle = busy === null

  const chips = (ids: string[]) =>
    ids.length ? (
      <div className="dx-iv-cites">
        <span className="dx-iv-cites-k">{tx.cites}</span>
        {ids.map((id) =>
          known.has(id) ? (
            <button key={id} type="button" className="dx-iv-chip" onClick={() => jump(id)}>{id}</button>
          ) : (
            <span key={id} className="dx-iv-chip is-miss">{id}</span>
          ))}
      </div>
    ) : null

  return (
    <section className="dx-iv" aria-busy={!idle}>
      <div className="dx-iv-head">
        <div className="dx-iv-head-k">
          <h2 className="dx-iv-title">{tx.title}</h2>
          {subject ? <span className="dx-iv-scope">{subject}</span> : null}
        </div>
        <p className="dx-iv-lead">{tx.lead}</p>
        <form
          className="dx-iv-ask"
          onSubmit={(e) => { e.preventDefault(); void begin(question) }}
        >
          <label className="dx-iv-lab" htmlFor={`${uid}-q`}>{tx.ask}</label>
          <div className="dx-iv-ask-row">
            <input
              id={`${uid}-q`}
              className="dx-iv-in"
              value={question}
              placeholder={tx.askPh}
              disabled={busy === 'start'}
              onChange={(e) => setQuestion(e.target.value)}
            />
            <button type="submit" className="dx-iv-go" disabled={!idle || !question.trim()}>
              {busy === 'start' ? tx.starting : tx.start}
            </button>
          </div>
        </form>
      </div>

      {err ? <p className="dx-iv-err">{tx.failed} · {err}</p> : null}

      {summary ? <p className="dx-iv-sum">{summary}</p> : null}

      {sessionId && receipt ? (
        <InvestigationContextReceipt lang={lang} sessionId={sessionId} receipt={receipt} />
      ) : null}

      <HypothesisRail lang={lang} view={hypotheses} rounds={probeRounds} onEvidence={jump} />

      <div className="dx-iv-ev">
        <button
          type="button"
          className="dx-iv-ev-h"
          aria-expanded={openCtx}
          aria-controls={listId}
          onClick={() => setOpenCtx((v) => !v)}
        >
          <span className="dx-iv-ev-k">{tx.ctx}</span>
          <span className="dx-iv-count">{tx.count(evidence.length)}</span>
          <span className="dx-iv-ev-t">{openCtx ? tx.close : tx.open}</span>
        </button>
        {openCtx ? (
          <ul className="dx-iv-ev-list" id={listId}>
            {evidence.length ? evidence.map((item) => (
              <li
                key={item.evidence_id}
                className={`dx-iv-ev-i${item.ok ? '' : ' is-bad'}${flash?.id === item.evidence_id ? ' is-flash' : ''}`}
                ref={(node) => {
                  if (node) evRefs.current.set(item.evidence_id, node)
                  else evRefs.current.delete(item.evidence_id)
                }}
              >
                <div className="dx-iv-ev-m">
                  <span className="dx-iv-id">{item.evidence_id}</span>
                  <span className={item.ok ? '' : 'dx-iv-bad'}>{item.ok ? tx.ok : tx.bad}</span>
                  <span>{clock(item.at)}</span>
                </div>
                <code className="dx-iv-cmd">{item.command}</code>
                <pre className={`dx-iv-out${item.ok ? '' : ' is-err'}`}>{item.output || '—'}</pre>
              </li>
            )) : (
              <li className="dx-iv-ev-i is-empty">{busy === 'start' ? tx.starting : tx.none}</li>
            )}
          </ul>
        ) : null}
      </div>

      <div className="dx-iv-act">
        <button type="button" className="dx-iv-main" onClick={() => void analyze()} disabled={!idle || !sessionId}>
          {busy === 'analyze' ? tx.analyzing : tx.analyze}
        </button>
        <span className="dx-iv-hint">{tx.hint}</span>
      </div>

      {verdict ? (
        <>
          <div className="dx-iv-k"><h3 className="dx-iv-k-t">{tx.verdict}</h3></div>
          <div className="dx-iv-sec">
            <p className="dx-iv-diag">{verdict.diagnosis}</p>
            {chips(verdict.citations)}
          </div>

          {actionCandidate ? (
            <div className="dx-action-rail" aria-label={zh ? '根因到结果回读' : 'Root to readback'}>
              <div><span>01</span><small>{zh ? '已确认根因' : 'CONFIRMED ROOT'}</small><b>{actionCandidate.root_hypothesis_id ?? verdict.rootCause}</b></div>
              <div><span>02</span><small>{zh ? '允许动作' : 'ALLOWLISTED ACTION'}</small><b>{actionCandidate.action ?? (zh ? '无可执行动作' : 'NO ACTION')}</b></div>
              <div><span>03</span><small>{zh ? '目标' : 'TARGET'}</small><b>{actionCandidate.target ?? '—'}</b></div>
              <div className={actionResult?.outcome === 'passed' ? 'is-passed' : ''}>
                <span>04</span><small>{zh ? '结果回读' : 'READBACK'}</small>
                <b>{actionResult?.outcome ?? (actionCandidate.eligible ? (zh ? '等待执行' : 'READY') : actionCandidate.reason ?? '—')}</b>
              </div>
              {actionCandidate.eligible && !actionResult ? (
                <button type="button" onClick={() => void runRemediation()} disabled={!idle}>
                  {busy === 'remediate' ? (zh ? '观察中…' : 'OBSERVING…') : (zh ? '执行并观察' : 'EXECUTE + OBSERVE')}
                </button>
              ) : null}
              {actionResult?.readback_evidence ? (
                <details><summary>{zh ? '查看动作回执与回读' : 'ACTION RECEIPT + READBACK'}</summary>
                  <pre>{actionResult.readback_evidence.output}</pre>
                </details>
              ) : null}
            </div>
          ) : null}

          <details className="dx-manual-disposition">
            <summary>{tx.disposition}</summary>
            <div className="dx-iv-sec dx-iv-disposition">
            <label className="dx-iv-lab" htmlFor={`${uid}-root`}>{tx.root}</label>
            <textarea id={`${uid}-root`} className="dx-iv-in" value={rootDraft}
              onChange={(e) => setRootDraft(e.target.value)} disabled={Boolean(archived)} />
            <div className="dx-iv-disposition-grid">
              <label><span className="dx-iv-lab">{tx.operator}</span><input className="dx-iv-in"
                value={operatorId} onChange={(e) => setOperatorId(e.target.value)} disabled={Boolean(archived)} /></label>
              <label><span className="dx-iv-lab">{tx.note}</span><input className="dx-iv-in"
                value={operatorNote} onChange={(e) => setOperatorNote(e.target.value)} disabled={Boolean(archived)} /></label>
            </div>
            {archived ? <p className="dx-iv-archived">{tx.archived} · {archived}</p> : (
              <div className="dx-iv-disposition-actions">
                <button type="button" className="dx-iv-go" onClick={() => void archive('confirmed')}
                  disabled={!idle || !rootDraft.trim() || !operatorId.trim() || !verdict.citations.length}>
                  {busy === 'close' ? tx.archiving : tx.confirm}
                </button>
                <button type="button" className="dx-iv-go is-secondary" onClick={() => void archive('refuted')}
                  disabled={!idle || !rootDraft.trim() || !operatorId.trim() || !verdict.citations.length}>{tx.refute}</button>
                <button type="button" className="dx-iv-go is-secondary" onClick={() => void archive('inconclusive')}
                  disabled={!idle || !rootDraft.trim() || !operatorId.trim()}>{tx.inconclusive}</button>
              </div>
            )}
            </div>
          </details>

          <div className="dx-iv-k">
            <h3 className="dx-iv-k-t">{tx.runbook}</h3>
            <button
              type="button"
              className="dx-iv-go is-inline"
              onClick={() => void runAll()}
              disabled={!idle || !verdict.runbook.length}
            >
              {busy === 'all' ? tx.allRunning : tx.all}
            </button>
          </div>
          <div className="dx-iv-sec">
            {stop ? (
              <p className="dx-iv-stop"><b>{tx.stopped(stop.n)}</b>{stop.reason}</p>
            ) : null}
            <ol className="dx-iv-steps">
              {verdict.runbook.map((step) => {
                const out = stepOut[step.n]
                // Locked unless the server said this step is runnable here. The
                // current frontend exposes no execution entry point for `auto` steps.
                const locked = step.runnable === false || step.risk !== 'readonly'
                const noteId = `${uid}-gate-${step.n}`
                const lockTip = step.risk === 'auto' ? tx.autoTip : tx.gateTip
                return (
                  <li
                    className={`dx-iv-step is-${step.risk}${stop?.n === step.n ? ' is-stop' : ''}`}
                    key={step.n}
                  >
                    <div className="dx-iv-step-m">
                      <span className="dx-iv-n">{step.n}</span>
                      <span className="dx-iv-risk">
                        {locked ? <span className="dx-lock" /> : null}
                        {pick(STEP_LABEL[step.risk], zh, step.risk)}
                      </span>
                      <span className="dx-iv-what">{step.what}</span>
                    </div>
                    {step.why ? <p className="dx-iv-why">{step.why}</p> : null}
                    <div className="dx-iv-line">
                      <code className="dx-iv-cmd">{step.command}</code>
                      <span className="dx-iv-gate" title={locked ? lockTip : undefined}>
                        <button
                          type="button"
                          className={`dx-iv-run${locked ? ' is-locked' : ''}`}
                          onClick={() => void runStep(step)}
                          disabled={locked || !idle || !sessionId}
                          title={locked ? lockTip : undefined}
                          aria-describedby={locked ? noteId : undefined}
                        >
                          {out?.state === 'run' ? tx.running : tx.run}
                        </button>
                      </span>
                    </div>
                    {locked ? <p className="dx-iv-note" id={noteId}><span className="dx-lock" />{lockTip}</p> : null}
                    {out && out.state !== 'run' ? (
                      <pre className={`dx-iv-out${out.state === 'err' ? ' is-err' : ''}`}>
                        {out.state === 'err' ? out.note ?? out.text : out.text || '—'}
                      </pre>
                    ) : null}
                    {out?.state === 'done' && out.note ? <p className="dx-iv-exit">{out.note}</p> : null}
                  </li>
                )
              })}
            </ol>
          </div>
        </>
      ) : null}

      <div className="dx-iv-k"><h3 className="dx-iv-k-t">{tx.chat}</h3></div>
      <div className="dx-iv-sec">
        <div className="dx-iv-chat" ref={chatRef} role="log" aria-live="polite">
          {turns.map((turn) => (
            <div className="dx-iv-turn" key={turn.id}>
              <p className="dx-iv-q"><b>{tx.you}</b>{turn.q}</p>
              <p className={`dx-iv-a${turn.state === 'err' ? ' is-err' : ''}`}>
                <b>{tx.it}</b>
                {turn.state === 'run' ? tx.sending : turn.a}
              </p>
              {turn.state === 'done' ? chips(turn.citations) : null}
            </div>
          ))}
        </div>
        <form className="dx-iv-ask" onSubmit={(e) => { e.preventDefault(); void ask(draft) }}>
          <label className="dx-iv-lab" htmlFor={`${uid}-a`}>{tx.chatLab}</label>
          <div className="dx-iv-ask-row">
            <input
              id={`${uid}-a`}
              className="dx-iv-in"
              value={draft}
              placeholder={tx.chatPh}
              disabled={!sessionId}
              onChange={(e) => setDraft(e.target.value)}
            />
            <button type="submit" className="dx-iv-go" disabled={!idle || !sessionId || !draft.trim()}>
              {busy === 'ask' ? tx.sending : tx.send}
            </button>
          </div>
        </form>
      </div>
    </section>
  )
}
