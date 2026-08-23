import { Scramble } from './Motion'
import type { Lang } from '../i18n'

export function Analyzing({ lang }: { lang: Lang }) {
  return (
    <div className="tc-loading">
      <span className="orbit" />
      <Scramble text={lang === 'zh' ? '研判请求处理中…' : 'ANALYSIS REQUEST IN PROGRESS…'} className="step-txt" />
    </div>
  )
}

export type WanThreat = {
  ip: string
  loading: boolean
  attempts?: number
  netblock?: string
  netblockAttempts?: number
  verdict?: string
  severity?: string
  campaign?: string
  killChain?: string
  attribution?: string
  siblings?: { ip: string; note: string; attempts?: number }[]
  internalCorrelation?: { ip: string; relation: string; deny?: number }[]
  blast?: string
  actions?: string[]
  playbook?: { target: string; targetIp: string; layer: string; commands: string[]; why: string }[]
  impactNodes?: string[]
  confidence?: number
  lockouts?: number
  distinctSrc?: number
  model?: string
  error?: string
}

export type Peer = { ip: string; relation: string }
export type Threat = {
  ip: string
  loading: boolean
  severity?: string
  verdict?: string
  analysis?: string
  impactPeers?: Peer[]
  mostLikely?: string
  worstCase?: string
  recovery?: { action: string; eta: string }
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
