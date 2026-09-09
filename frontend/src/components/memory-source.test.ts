import { afterEach, describe, expect, it, vi } from 'vitest'
import { resolveMemorySource } from './memory-source'
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
afterEach(() => vi.unstubAllGlobals())
describe('memory provenance navigation', () => {
  it('resolves a session to the persisted case subject, including serial-to-IP mappings', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(response({ session_id: 'auth-session', case_id: 'case-firewall', subject: '192.168.1.1' })).mockResolvedValueOnce(response({ case: { caseId: 'case-firewall', subject: 'FG100ETK20014183' } }))
    vi.stubGlobal('fetch', fetcher)
    expect(await resolveMemorySource('auth-session')).toEqual({ sourceId: 'auth-session', caseId: 'case-firewall', subject: 'FG100ETK20014183' })
    expect(fetcher.mock.calls.map(row => row[0])).toEqual(['/api/rca/investigate/auth-session', '/api/rca/investigation-cases/case-firewall'])
  })
  it('verifies a direct case reference before navigating', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ case: { caseId: 'case-address', subject: '192.168.1.110' } })))
    expect(await resolveMemorySource('case-address')).toEqual({ sourceId: 'case-address', caseId: 'case-address', subject: '192.168.1.110' })
  })
  it('keeps an unknown sentinel trace unlinked instead of using it as an asset', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({}, 404)); vi.stubGlobal('fetch', fetcher)
    await expect(resolveMemorySource('sentinel-old-cycle')).rejects.toMatchObject({ reason: 'unlinked' })
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it('rejects stale or mismatched session and case responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ session_id: 'other-session', case_id: 'case-address' })))
    await expect(resolveMemorySource('auth-session')).rejects.toMatchObject({ reason: 'mismatch' })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ case: { caseId: 'other-case', subject: '192.168.1.110' } })))
    await expect(resolveMemorySource('case-address')).rejects.toMatchObject({ reason: 'mismatch' })
  })
})
