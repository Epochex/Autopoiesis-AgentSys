import { useCallback, useEffect, useState } from 'react'
import type { Lang } from '../i18n'

type OperationalRow = {
  id: string
  title: string
  status: string
  source: string
  first_seen?: string | null
  last_seen?: string | null
  evidence_count?: number
  sample_count?: number
  confidence?: number
  reason?: string | null
}

type OperationalPayload = {
  ok: boolean
  durable: boolean
  last_refresh?: string | null
  coverage: { sources: string[]; blind_spots: string[] }
  counts: { dossiers: number; risks: number; features: number }
  dossiers: OperationalRow[]
  risks: OperationalRow[]
  features: OperationalRow[]
}

const clock = (value?: string | null) =>
  value ? value.replace('T', ' ').replace(/\.\d+(?=Z|[+-]\d\d:\d\d$)/, '').replace('Z', '') : '—'

export function OperationalMemoryPanel({ lang, subject }: { lang: Lang; subject?: string }) {
  const zh = lang === 'zh'
  const [data, setData] = useState<OperationalPayload | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (refresh = false) => {
    setBusy(true)
    setError(null)
    try {
      if (refresh) {
        const update = await fetch('/api/rca/operational-memory/refresh', {
          method: 'POST', headers: { Accept: 'application/json' },
        })
        if (!update.ok) throw new Error(`refresh HTTP ${update.status}`)
      }
      const query = subject ? `?subject=${encodeURIComponent(subject)}` : ''
      const response = await fetch(`/api/rca/operational-memory${query}`, {
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const payload = await response.json() as OperationalPayload
      if (!payload.ok) throw new Error('operational memory unavailable')
      setData(payload)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }, [subject])

  useEffect(() => { void load(false) }, [load])

  const groups: { key: keyof Pick<OperationalPayload, 'dossiers' | 'risks' | 'features'>; label: string }[] = [
    { key: 'dossiers', label: zh ? '故障档案' : 'INCIDENT DOSSIERS' },
    { key: 'risks', label: zh ? '长期风险' : 'LONG-RUN RISKS' },
    { key: 'features', label: zh ? '网络特征晋升审计' : 'NETWORK FEATURE PROMOTION AUDIT' },
  ]

  return (
    <div className="dx-om">
      <div className="dx-om-head">
        <div>
          <b>{zh ? '长期运维记忆' : 'OPERATIONAL MEMORY'}</b>
          <span>{subject ? `${zh ? '当前对象' : 'SUBJECT'} · ${subject}` : (zh ? '整张内网' : 'WHOLE NETWORK')}</span>
        </div>
        <button type="button" onClick={() => void load(true)} disabled={busy}>
          {busy ? (zh ? '正在更新…' : 'REFRESHING…') : (zh ? '从真实来源更新' : 'REFRESH FROM SOURCES')}
        </button>
      </div>
      {error ? <div className="dx-om-error">{error}</div> : null}
      {data ? (
        <>
          <div className="dx-om-meta">
            <span>{data.durable ? (zh ? '持久化已连接' : 'DURABLE') : (zh ? '进程内降级存储' : 'IN-PROCESS FALLBACK')}</span>
            <span>{zh ? '来源' : 'SOURCES'} · {(data.coverage?.sources ?? []).join(' · ') || '—'}</span>
            <span>{zh ? '最近更新' : 'UPDATED'} · {clock(data.last_refresh)}</span>
          </div>
          {(data.coverage?.blind_spots ?? []).length ? (
            <div className="dx-om-blind">
              <b>{zh ? '覆盖缺口' : 'COVERAGE GAPS'}</b>
              {(data.coverage?.blind_spots ?? []).join(' · ')}
            </div>
          ) : null}
          <div className="dx-om-groups">
            {groups.map(({ key, label }) => {
              const rows = data[key] ?? []
              return (
                <section key={key}>
                  <header><b>{label}</b><span>{rows.length}</span></header>
                  {rows.length ? rows.slice(0, 8).map((row) => (
                    <article key={row.id}>
                      <div><b>{row.title}</b><span>{row.status} · {row.source}</span></div>
                      <p>{row.reason || `${clock(row.first_seen)} → ${clock(row.last_seen)}`}</p>
                      <footer>
                        {typeof row.evidence_count === 'number' ? <span>{zh ? '证据' : 'EVIDENCE'} {row.evidence_count}</span> : null}
                        {typeof row.sample_count === 'number' ? <span>{zh ? '样本' : 'SAMPLES'} {row.sample_count}</span> : null}
                        {typeof row.confidence === 'number' ? <span>{zh ? '置信度' : 'CONF'} {row.confidence.toFixed(2)}</span> : null}
                      </footer>
                    </article>
                  )) : <div className="dx-om-empty">{zh ? '当前过滤范围没有记录' : 'NO RECORDS IN THIS SCOPE'}</div>}
                </section>
              )
            })}
          </div>
        </>
      ) : null}
    </div>
  )
}
