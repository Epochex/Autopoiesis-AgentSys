import type { MemoryRelation } from '../types'

export type RelationRow = { targetId: string; relations: MemoryRelation[] }

/** Join cursor-visible link targets to their typed relation metadata.
 * A link can exist without a typed relation, so every target remains visible
 * and carries an explicit generic label in the inspector.
 */
export function buildRelationRows(links: string[], relations: MemoryRelation[]): RelationRow[] {
  const targets = Array.from(new Set([...links, ...relations.map((relation) => relation.target_id)]))
  return targets.map((targetId) => ({
    targetId,
    relations: relations.filter((relation) => relation.target_id === targetId),
  }))
}
