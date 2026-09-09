import type { Lang } from '../i18n'
import type { AnalysisMetadata } from './analysis-read'
import './analysis-status.css'

export function AnalysisStatus({ value, lang, onAnalyze }: { value: AnalysisMetadata & {loading?: boolean; error?: string; verdict?: string}; lang: Lang; onAnalyze?: (force?: boolean) => void }) {
  const zh = lang === 'zh'
  const t = (cn: string, en: string) => zh ? cn : en
  const cache = value.cache
  const failed = value.error || value.analysisError
  const label = cache?.status === 'hit' ? t('已保存研判', 'Saved analysis') : cache?.status === 'generated' ? t('本次研判已保存', 'Analysis saved') : cache?.status === 'stale' ? t('已有研判，观测已变化或缓存过期', 'Saved analysis exists; observations changed or cache expired') : t('当前观测事实', 'Current observed facts')
  const render = (v: unknown) => v == null ? t('未确认', 'Unconfirmed') : typeof v === 'boolean' ? (v ? t('有证据', 'Evidenced') : t('待核实', 'Pending verification')) : typeof v === 'object' ? JSON.stringify(v) : String(v)
  return <div className="analysis-status">
    <div className="analysis-cache"><strong>{label}</strong>{value.cacheReading && <span role="status">{t('正在读取已有研判…', 'Reading saved analysis…')}</span>}{cache?.analyzedAt && <time>{t('研判于', 'Analyzed at')} {new Date(cache.analyzedAt).toLocaleString(zh ? 'zh-CN' : 'en-GB')}</time>}{cache?.analyzedAt && cache.evidenceChanged !== undefined && <span>{cache.evidenceChanged ? t('证据已变化', 'Evidence changed') : t('证据未变化', 'Evidence unchanged')}</span>}</div>
    {value.riskAssessment && <dl className="analysis-evidence">{([['attackAttempt', t('攻击尝试', 'Attack attempts')], ['blocked', t('拦截结果', 'Blocking evidence')], ['successfulIntrusion', t('成功入侵证据', 'Successful intrusion evidence')], ['businessImpact', t('业务影响证据', 'Business impact evidence')]] as const).map(([key,label]) => <div key={key}><dt>{label}</dt><dd>{render(value.riskAssessment?.[key])}</dd></div>)}</dl>}
    {value.analysisAvailable && value.riskAssessment?.basis != null && value.riskAssessment.basis !== value.verdict && <p><strong>{t('判定依据', 'Assessment basis')}</strong> · {render(value.riskAssessment.basis)}</p>}
    {failed && <p role="alert">{failed}</p>}
    {value.loading && <p role="status">{t('深度研判处理中，当前观测继续显示。', 'Deep analysis is running; current observations remain visible.')}</p>}
    {onAnalyze && <div className="analysis-buttons"><button disabled={value.loading} onClick={() => onAnalyze(false)}>{failed ? t('重试深度研判', 'Retry deep analysis') : value.analysisAvailable ? t('复用已有研判', 'Reuse saved analysis') : t('启动 DeepSeek 深度研判', 'Run DeepSeek deep analysis')}</button>{cache?.analyzedAt && <button disabled={value.loading} onClick={() => onAnalyze(true)}>{t('基于当前证据重新研判', 'Reanalyze current evidence')}</button>}</div>}
  </div>
}
