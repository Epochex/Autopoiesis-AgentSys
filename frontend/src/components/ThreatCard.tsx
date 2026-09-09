import type { AnalysisMetadata } from './analysis-read'
import { AnalysisStatus } from './AnalysisStatus'
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

export type WanThreat = AnalysisMetadata & {
  ip: string
  loading: boolean
  attempts?: number
  eventCount?: number
  observedAt?: string
  sourceMode?: string
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
  playbook?: { target: string; targetIp: string; layer: string; commands: string[]; why: string; preconditions?: string; expected?: string; rollback?: string }[]
  impactNodes?: string[]
  confidence?: number
  lockouts?: number
  distinctSrc?: number
  model?: string
  error?: string
}

export type Peer = { ip: string; relation: string }
export type Threat = AnalysisMetadata & {
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

export function ThreatCard({ th, lang, onClose, onAnalyze }: { th: Threat; lang: Lang; onClose: () => void; onAnalyze?: (force?: boolean) => void }) {
  const zh = lang === 'zh'
  return <aside className={`threat-card sev-${th.analysisAvailable ? th.severity ?? 'pending' : 'pending'}`}>
    <div className="tc-head"><span className="tc-kicker">{zh ? '设备观测与研判' : 'Device observations and assessment'} · {th.ip}</span><button className="tc-x" onClick={onClose} aria-label={zh ? '关闭' : 'Close'}>✕</button></div>
    <div className="tc-body"><div className="tc-verdict"><strong>{th.verdict}</strong>{th.analysisAvailable && th.severity && <span className="sev-tag">{th.severity}</span>}</div>{th.analysis && th.analysis !== th.verdict ? <p>{th.analysis}</p> : null}{(th.mostLikely || th.worstCase) && <div className="an-pred"><strong>{zh ? '风险推断' : 'Risk inference'}</strong>{th.mostLikely && <p>{zh ? '最可能：' : 'Most likely: '}{th.mostLikely}</p>}{th.worstCase && <p>{zh ? '最坏情况：' : 'Worst case: '}{th.worstCase}</p>}</div>}{th.impactPeers?.length ? <div><strong>{zh ? '关联设备与依据' : 'Related devices and evidence'}</strong>{th.impactPeers.map(peer => <p key={peer.ip}>{peer.ip} · {peer.relation}</p>)}</div> : null}{th.recovery && <p>{zh ? '恢复建议：' : 'Recovery suggestion: '}{th.recovery.action} · {th.recovery.eta}</p>}<AnalysisStatus value={th} lang={lang} onAnalyze={onAnalyze}/>{th.model && <span className="tc-model">{th.model}</span>}</div>
  </aside>
}
