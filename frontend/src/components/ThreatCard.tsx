import { useEffect, useState } from 'react'
import { Scramble } from './Motion'
import type { Lang } from '../i18n'

const STEPS: Record<Lang, string[]> = {
  zh: ['建立 R230 遥测会话…', '拉取设备流量与端口特征…', 'DeepSeek v4-pro 研判中…', '归纳威胁结论…'],
  en: ['opening R230 telemetry…', 'pulling flow & port features…', 'DeepSeek v4-pro reasoning…', 'composing verdict…'],
}

export function Analyzing({ lang }: { lang: Lang }) {
  const [i, setI] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setI((x) => Math.min(x + 1, STEPS[lang].length - 1)), 1400)
    return () => clearInterval(id)
  }, [lang])
  return (
    <div className="tc-loading">
      <span className="orbit" />
      <Scramble text={STEPS[lang][i]} className="step-txt" />
    </div>
  )
}

export type Threat = {
  ip: string
  loading: boolean
  severity?: string
  verdict?: string
  analysis?: string
  model?: string
  error?: string
}

export function ThreatCard({ th, lang, onClose }: { th: Threat; lang: Lang; onClose: () => void }) {
  return (
    <aside className={`threat-card sev-${th.severity ?? 'pending'}`}>
      <div className="tc-head">
        <span className="tc-kicker">{lang === 'zh' ? 'DeepSeek 主动研判' : 'DeepSeek active analysis'} · {th.ip}</span>
        <button className="tc-x" onClick={onClose} aria-label="close">✕</button>
      </div>
      {th.loading ? (
        <Analyzing lang={lang} />
      ) : th.error ? (
        <div className="tc-body err">{th.error}</div>
      ) : (
        <div className="tc-body">
          <div className="tc-verdict">
            <span className={`sev-dot ${th.severity}`} />
            <strong>{th.verdict}</strong>
            <span className="sev-tag">{th.severity}</span>
          </div>
          <p>{th.analysis}</p>
          <span className="tc-model">{th.model}</span>
        </div>
      )}
    </aside>
  )
}
