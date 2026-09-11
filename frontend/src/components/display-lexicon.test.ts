import { describe, expect, it } from 'vitest'
import { PATTERNS, display, englishFor } from './display-lexicon'

const HAN = /[㐀-鿿]/

describe('display lexicon', () => {
  it('leaves Chinese alone in the Chinese UI', () => {
    expect(display('同网段横向通信', 'zh')).toBe('同网段横向通信')
  })

  it('translates fixed vocabulary', () => {
    expect(display('同网段横向通信', 'en')).toBe('Same-segment lateral traffic')
    expect(display('重启失败服务', 'en')).toBe('Restart failed services')
  })

  it('keeps every measurement and address in a composed sentence', () => {
    expect(englishFor('24 小时活动量达到过去 7 天日均的 118.8 倍'))
      .toBe('24-hour activity is 118.8× the 7-day daily average')
    expect(englishFor('已确认 192.168.1.15 存在地址归属冲突'))
      .toBe('Confirmed address-ownership conflict on 192.168.1.15')
    expect(englishFor('复核窗口内在 2 个 MAC 之间换手 1 次'))
      .toBe('1 handover between 2 MACs during the review window')
  })

  it('translates each segment of a · joined label', () => {
    expect(englishFor('192.168.1.21 · 地址归属与租约冲突已由复核确认'))
      .toBe('192.168.1.21 · address ownership conflicts with the lease, confirmed on review')
    expect(englishFor('ClickHouse · FortiGate · 生产事实流'))
      .toBe('ClickHouse · FortiGate · Production fact stream')
  })

  it('takes the English half of a string that carries both languages', () => {
    expect(englishFor('留出集，采集窗口 2026-06-16 / Held-out capture from 2026-06-16.'))
      .toBe('Held-out capture from 2026-06-16.')
  })

  it('replaces known Chinese inside otherwise English records', () => {
    const stored = 'Incident sentinel-case-abc: unit.service 处于 failed 状态，服务进程不可用。目标属于受控单元白名单，已进入重启前置校验。'
    const english = englishFor(stored)
    expect(english).not.toBeNull()
    expect(HAN.test(english as string)).toBe(false)
    expect(english).toContain('sentinel-case-abc')
  })

  it('agrees in number with the count it was given', () => {
    expect(englishFor('复核窗口内在 3 个 MAC 之间换手 5 次'))
      .toBe('5 handovers between 3 MACs during the review window')
    expect(englishFor('访问 1 个目标，高于过去 7 天日均'))
      .toBe('Reached 1 destination, above the 7-day daily average')
  })

  it('renders every case-decision template it declares', () => {
    expect(englishFor('预定义故障条件已排除，转入开放根因调查'))
      .toBe('Predefined fault conditions ruled out; moving to open root-cause investigation')
    expect(englishFor('已取得 4 条当前观察，仍有 2 个候选原因等待区分。'))
      .toBe('4 current observations collected; 2 candidate causes still need separating.')
    expect(englishFor('候选原因 open_root 在当前对象上是否成立？'))
      .toBe('Does candidate cause open_root hold for this object?')
    expect(englishFor('已确认：受管服务进入失败状态'))
      .toBe('Confirmed: managed service entered the failed state')
  })

  it('renders the sentinel and detector sentences', () => {
    expect(englishFor('192.168.1.23 · 已完成 6 次健康回读'))
      .toBe('192.168.1.23 · 6 health readbacks completed')
    expect(englishFor('unit.service 处于 failed 状态，服务进程不可用。目标属于受控单元白名单，已进入重启前置校验。'))
      .toBe('unit.service is in the failed state and its service process is not running. The target is on the controlled-unit allowlist and has entered restart preflight checks.')
    expect(englishFor('eno2 配了地址 192.168.1.23 但没有载波，说明它本来该承载流量、现在断了。'))
      .toBe('eno2 has address 192.168.1.23 configured but no carrier, which means it was meant to carry traffic and is now down.')
    expect(englishFor('观察窗口通过')).toBe('Observation window passed')
  })

  it('has no pattern that cannot match, so an escaping slip fails here', () => {
    // A doubled backslash in a regex literal matches a literal backslash and
    // would silently never fire, leaving the string Chinese on screen.
    for (const [pattern] of PATTERNS) expect(pattern.source).not.toMatch(/\\\\[dsSwW]/)
  })

  it('anchors every pattern and pairs every group with a slot', () => {
    for (const [pattern, target] of PATTERNS) {
      expect(pattern.source.startsWith('^') && pattern.source.endsWith('$')).toBe(true)
      const groups = new RegExp(pattern.source + '|').exec('')!.length - 1
      const slots = new Set([...target.matchAll(/\$(\d)/g)].map(m => m[1]))
      expect(slots.size).toBe(groups)
    }
  })

  it('reports prose it cannot render rather than passing Chinese through', () => {
    expect(englishFor('这段话不在词表里')).toBeNull()
    // A template whose capture is unknown prose must not half-translate.
    expect(englishFor('已确认：某个词表里没有的根因')).toBeNull()
    expect(englishFor('192.168.1.9 · 某个词表里没有的原因')).toBeNull()
  })

  it('never halves raw command output that happens to contain a slash', () => {
    const raw = 'Filesystem Size Used Avail Use% Mounted on\ntmpfs 1.5G 2.8M 1.5G 1% /run\n… [略去 2,894 字符] …\n/dev/sda1 / 中文'
    const rendered = englishFor(raw)
    expect(rendered).toBeNull() // residual Chinese, so the caller keeps the original
    expect(englishFor('a / b 中 / c')).toBeNull()
  })

  it('translates the omission marker the backend injects into evidence', () => {
    expect(englishFor('head\n… [略去 2,894 字符] …\ntail'))
      .toBe('head\n… [2,894 characters omitted] …\ntail')
  })

  it('keeps the column alignment of quoted command output', () => {
    const df = 'Filesystem      Size  Used Avail\n… [略去 12 字符] …\ntmpfs           1.5G  2.8M  1.5G'
    expect(englishFor(df))
      .toBe('Filesystem      Size  Used Avail\n… [12 characters omitted] …\ntmpfs           1.5G  2.8M  1.5G')
  })

  it('leaves device configuration identifiers untouched', () => {
    // A FortiGate interface really is named this; it is a name, not prose.
    expect(display('64网段showroo', 'en')).toBe('64网段showroo')
  })
})
