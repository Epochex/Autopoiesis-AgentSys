import { useEffect, useRef } from 'react'
import type { Lang } from '../i18n'
import type { ChainStep, CommandLine } from './use-sentinel-chain'
import './execution-log.css'

/* ── 执行记录 — every command, and what came back ────────────────────────────
 *
 * A verdict the operator cannot check is a verdict they have to take on faith.
 * The system reads a unit's state, decides, restarts it, then re-reads it
 * twelve times over ninety seconds — and all of that used to collapse into one
 * line saying it passed.
 *
 * So the transcript is shown, open by default and scrolling itself, while it
 * happens. The point is to be read along with: an operator watching a restart
 * they are unsure about should be able to see the exact argv and the exact
 * output at the moment it runs, and stop it if it looks wrong. */

/** Steps worth a divider in the transcript, so commands group under a phase. */
const PHASE_MARK: Record<string, [string, string]> = {
  detected: ['发现', 'DETECTED'],
  preflight: ['前置校验', 'PREFLIGHT'],
  remediated: ['执行 + 观察期结束', 'ACTED, WINDOW CLOSED'],
  resolved: ['判定恢复', 'RESOLVED'],
  no_safe_action: ['无安全动作', 'NO SAFE ACTION'],
  declined: ['拒绝执行', 'DECLINED'],
  // The transcript below this line is empty on purpose: the refusal happened
  // before any command was chosen, and the divider is what says so.
  escalated: ['不再自动修，转人工', 'STOPPED — HANDED TO A PERSON'],
}

type Row =
  | { sort: string; kind: 'mark'; label: [string, string] }
  | { sort: string; kind: 'cmd'; line: CommandLine }

function interleave(steps: ChainStep[], commands: CommandLine[]): Row[] {
  const rows: Row[] = []
  for (const step of steps) {
    const label = PHASE_MARK[step.kind]
    if (label) rows.push({ sort: step.at, kind: 'mark', label })
  }
  for (const line of commands) rows.push({ sort: line.at, kind: 'cmd', line })
  rows.sort((a, b) => (a.sort < b.sort ? -1 : a.sort > b.sort ? 1 : 0))
  return rows
}

export function ExecutionLog(
  { steps, commands, lang }: { steps: ChainStep[]; commands: CommandLine[]; lang: Lang },
) {
  const zh = lang === 'zh'
  const body = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  // Follow the tail, but stop following the moment the operator scrolls up to
  // read something — yanking them back to the bottom mid-read is the classic
  // way a live log becomes unusable.
  const onScroll = () => {
    const el = body.current
    if (!el) return
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  }

  const rows = interleave(steps, commands)

  useEffect(() => {
    if (pinned.current && body.current) body.current.scrollTop = body.current.scrollHeight
  }, [rows.length])

  if (!rows.length) return null

  return (
    <div className="xl">
      <div className="xl-head">
        <span className="xl-k">{zh ? '执行记录 · 实时' : 'TRANSCRIPT · LIVE'}</span>
        <span className="xl-n">{commands.length} {zh ? '条命令' : 'commands'}</span>
        <span className="xl-hint">{zh ? '自动滚动 · 往上翻会停住' : 'follows the tail; scroll up to hold'}</span>
      </div>
      <div className="xl-body" ref={body} onScroll={onScroll}>
        {rows.map((row, i) => row.kind === 'mark' ? (
          <div key={`m${i}`} className="xl-mark"><span>{row.label[zh ? 0 : 1]}</span></div>
        ) : (
          /* The exit code is shown but not judged: `systemctl is-active` returns
             3 for a failed unit, which is the reading the detector wanted, not a
             failure. Painting every non-zero red would call the system's normal
             probing an error. */
          <div key={`c${i}`} className="xl-cmd">
            <div className="xl-argv">
              <span className="xl-at">{row.line.at.slice(11, 19)}</span>
              <span className="xl-dollar">$</span>
              {row.line.argv.join(' ')}
              {row.line.rc !== 0 ? <span className="xl-rc">exit {row.line.rc}</span> : null}
            </div>
            {row.line.out ? (
              <pre className="xl-out">{row.line.out}{row.line.truncated ? ' …' : ''}</pre>
            ) : (
              <pre className="xl-out muted">{zh ? '（无输出）' : '(no output)'}</pre>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
