import { describe, expect, it } from 'vitest'
import { buildRelationRows } from './memory-relations'

describe('buildRelationRows', () => {
  it('groups multiple typed relations under one target and keeps link order', () => {
    const rows = buildRelationRows(
      ['epi-dhcp', 'epi-session', 'untyped-insight'],
      [
        { target_id: 'epi-session', relation_type: 'similar_to', confidence: 0.4, evidence_ids: [] },
        { target_id: 'epi-session', relation_type: 'follows', confidence: 1, evidence_ids: [] },
        { target_id: 'epi-device-port', relation_type: 'precedes', confidence: 1, evidence_ids: [] },
      ],
    )

    expect(rows.map((row) => row.targetId)).toEqual([
      'epi-dhcp',
      'epi-session',
      'untyped-insight',
      'epi-device-port',
    ])
    expect(rows[1].relations.map((relation) => relation.relation_type)).toEqual([
      'similar_to',
      'follows',
    ])
    expect(rows[2].relations).toEqual([])
  })

  it('deduplicates a target present in both links and typed relations', () => {
    const rows = buildRelationRows(
      ['epi-session', 'epi-session'],
      [{ target_id: 'epi-session', relation_type: 'similar_to', confidence: 0.4, evidence_ids: [] }],
    )

    expect(rows).toHaveLength(1)
    expect(rows[0].relations[0].confidence).toBe(0.4)
  })
})
