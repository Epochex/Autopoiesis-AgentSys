import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Lang } from '../i18n'
const cache = new Map<string, string>()
const chinese = /[\u3400-\u9fff]/
/** Batch display prose only. Keep commands and original evidence outside texts. */
export function useDisplayTranslation(texts: string[], lang: Lang) {
  const key = JSON.stringify([...new Set(texts.filter(text => chinese.test(text)))].sort())
  const requested = useMemo(() => JSON.parse(key) as string[], [key])
  const [revision, setRevision] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [retryCount, setRetryCount] = useState(0)
  useEffect(() => {
    if (lang !== 'en') return
    const missing = requested.filter(text => !cache.has(text))
    if (!missing.length) return
    const controller = new AbortController()
    void (async () => {
      setLoading(true); setError(null)
      for (let start = 0; start < missing.length; start += 120) {
        const response = await fetch('/api/rca/display-translation', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({texts: missing.slice(start, start + 120), lang}), signal: controller.signal})
        if (!response.ok) throw new Error('English translation could not be completed. Please retry.')
        const payload = await response.json() as {translations: Record<string, string>}
        for (const [source, target] of Object.entries(payload.translations)) cache.set(source, target)
      }
      if (!controller.signal.aborted) setRevision(value => value + 1)
    })().catch(error => {if (!controller.signal.aborted) setError(String(error.message ?? error))}).finally(() => {if (!controller.signal.aborted) setLoading(false)})
    return () => controller.abort()
  }, [requested, lang, retryCount])
  const translate = useCallback((text: string) => { void revision; return lang === 'en' ? cache.get(text) ?? text : text }, [lang, revision])
  return {translate, loading: lang === 'en' && loading, error: lang === 'en' ? error : null, retry: () => setRetryCount(value => value + 1)}
}
