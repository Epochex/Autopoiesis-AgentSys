import { describe, expect, it } from 'vitest'
import { theaterRailPresentation } from './TheaterStage'
import type { ChainStep } from './use-sentinel-chain'

const ACTION = ['detector', 'confirm', 'preflight', 'act', 'watch', 'verify']
const REPORT = ['detector', 'confirm', 'gate', 'handoff']

const step = (kind: string, extra: Partial<ChainStep> = {}): ChainStep => ({
  at: '2026-08-24T10:00:00+00:00',
  kind,
  subject: 'demo-collector.service',
  ...extra,
})

const statuses = (timeline: ChainStep[]) => theaterRailPresentation({
  railIds: ACTION,
  timeline,
  sentinel: true,
}).stages.map(({ status }) => status)

describe('theater sentinel rail projection', () => {
  it('advances a service-down chain one visible stage at a time from ledger rows', () => {
    expect(statuses([step('detected', { streak: 1, need: 2 })])).toEqual([
      'current', 'pending', 'pending', 'pending', 'pending', 'pending',
    ])
    expect(statuses([
      step('detected', { streak: 1, need: 2 }),
      step('awaiting_confirmation', { streak: 1, need: 2 }),
    ])).toEqual([
      'completed', 'current', 'pending', 'pending', 'pending', 'pending',
    ])
    expect(statuses([
      step('detected', { streak: 2, need: 2 }),
    ])).toEqual([
      'completed', 'completed', 'current', 'pending', 'pending', 'pending',
    ])
    expect(statuses([
      step('detected', { streak: 2, need: 2 }),
      step('preflight'),
    ])).toEqual([
      'completed', 'completed', 'completed', 'current', 'pending', 'pending',
    ])
    expect(statuses([
      step('detected', { streak: 2, need: 2 }),
      step('preflight'),
      step('remediation_committed'),
    ])).toEqual([
      'completed', 'completed', 'completed', 'completed', 'current', 'pending',
    ])
    expect(statuses([
      step('detected', { streak: 2, need: 2 }),
      step('preflight'),
      step('remediation_committed'),
      step('bakein_sampled', { samples: 4 }),
      step('bakein_passed'),
    ])).toEqual([
      'completed', 'completed', 'completed', 'completed', 'completed', 'current',
    ])
  })

  it('keeps verify current across the remediated-passed to resolved append race', () => {
    const running = theaterRailPresentation({
      railIds: ACTION,
      timeline: [
        step('detected', { streak: 2, need: 2 }),
        step('preflight'),
        step('remediation_committed'),
        step('bakein_passed'),
        step('remediated', { outcome: 'passed' }),
      ],
      sentinel: true,
    })
    expect(running.terminalKind).toBeNull()
    expect(running.stages.at(-1)).toMatchObject({ status: 'current', terminal: false })

    const closed = theaterRailPresentation({
      railIds: ACTION,
      timeline: [
        step('detected', { streak: 2, need: 2 }),
        step('preflight'),
        step('remediation_committed'),
        step('bakein_passed'),
        step('remediated', { outcome: 'passed' }),
        step('resolved', { outcome: 'passed' }),
      ],
      sentinel: true,
    })
    expect(closed.terminalKind).toBe('resolved')
    expect(closed.stages.every(({ status }) => status === 'completed')).toBe(true)
    expect(closed.stages.at(-1)?.terminal).toBe(true)
  })

  it('renders a safety refusal as four completed stages with handoff as the terminal landing point', () => {
    const view = theaterRailPresentation({
      railIds: REPORT,
      timeline: [
        step('detected', { streak: 2, need: 2 }),
        step('no_safe_action', { reason: 'no registered safe write' }),
      ],
      reportClosed: true,
      sentinel: true,
    })

    expect(view.terminalKind).toBe('no_safe_action')
    expect(view.stages).toEqual([
      { id: 'detector', status: 'completed', terminal: false },
      { id: 'confirm', status: 'completed', terminal: false },
      { id: 'gate', status: 'completed', terminal: false },
      { id: 'handoff', status: 'completed', terminal: true },
    ])
  })

  it('does not flash an in-flight step when a closed report loads before its timeline', () => {
    const view = theaterRailPresentation({
      railIds: REPORT,
      timeline: [],
      fallbackReached: REPORT,
      reportClosed: true,
      sentinel: true,
    })
    expect(view.stages.every(({ status }) => status === 'completed')).toBe(true)
    expect(view.stages.at(-1)?.terminal).toBe(true)
  })

  it('keeps unreachable action stages weak after recurrence escalation', () => {
    const view = theaterRailPresentation({
      railIds: ACTION,
      timeline: [
        step('detected', { streak: 2, need: 2 }),
        step('escalated'),
      ],
      sentinel: true,
    })
    expect(view.stages.map(({ status }) => status)).toEqual([
      'completed', 'completed', 'pending', 'pending', 'pending', 'pending',
    ])
    expect(view.stages[1].terminal).toBe(true)
  })
})
