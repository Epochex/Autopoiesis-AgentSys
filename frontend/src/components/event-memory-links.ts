/** Only exact structured IDs establish an event-to-memory relation. */
export function memoryIdsForEvent(payload: unknown, allowed: string[]): string[] {
  const known = new Set(allowed)
  const found = new Set<string>()
  function visit(value: unknown) {
    if (typeof value === 'string' && known.has(value)) found.add(value)
    else if (Array.isArray(value)) value.forEach(visit)
    else if (value && typeof value === 'object') Object.values(value).forEach(visit)
  }
  visit(payload)
  return [...found]
}
