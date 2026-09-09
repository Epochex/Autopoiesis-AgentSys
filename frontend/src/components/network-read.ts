import { useEffect, useState } from 'react'

export function useRead<T>(url: string | null) {
  const [state, setState] = useState<{ url: string; data?: T; error?: string }>({ url: '' })
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    if (!url) return
    let active = true
    let controller: AbortController | null = null
    const load = () => {
      if (controller) return
      const current = new AbortController()
      controller = current
      const timeout = window.setTimeout(() => {
        current.abort()
        if (active) setState({ url, error: '读取超时，请重试' })
      }, 45_000)
      fetch(url, { signal: current.signal }).then(async response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const data = await response.json()
        if (data.ok === false) throw new Error('数据源暂不可用')
        if (active && !current.signal.aborted) setState({ url, data })
      }).catch(error => {
        if (active && !current.signal.aborted) setState({ url, error: String(error.message) })
      }).finally(() => { window.clearTimeout(timeout); controller = null })
    }
    load()
    const interval = window.setInterval(load, 30_000)
    return () => { active = false; controller?.abort(); window.clearInterval(interval) }
  }, [url, revision])
  return { data: state.url === url ? state.data : undefined, error: state.url === url ? state.error : undefined,
    retry: () => { setState({ url: '' }); setRevision(r => r + 1) } }
}
