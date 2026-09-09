import { useMemo, useState } from 'react'
import type { Lang } from '../i18n'
import type { Chain } from './use-sentinel-chain'
import { executionProgress } from './execution-progress'
import { useDisplayTranslation } from './useDisplayTranslation'
import './execution-progress.css'


const purposeChinese: Record<string,string> = {
  'Recorded operation receipt; process exit code and output were not captured': '核对已保存的动作回执；该历史记录未采集退出码和输出',
  'Restart the verified failed service': '重启已验证故障的服务',
  'Read the service active state': '读取服务当前活动状态',
  'Read the service restart counter': '读取服务重启计数',
  'Inspect recent service failures and dependency evidence': '检查近期服务失败与依赖证据',
  'Read the interface carrier and administrative state': '读取网卡载波与管理状态',
  'Apply the reviewed interface administrative state': '应用已审阅的网卡管理状态',
  'Verify management gateway HTTP reachability': '验证管理网关的 HTTP 可达性',
  'Execute the recorded command for this action': '执行当前动作已记录的命令',
}

export function ExecutionProgress({ chain, lang }: { chain: Chain | null; lang: Lang }) {
  const zh = lang === 'zh'
  const t = (cn: string, en: string) => zh ? cn : en
  const progress = useMemo(() => executionProgress(chain), [chain])
  const [selection, setSelection] = useState<string | null>(null)
  const selected = progress.commands.find(row => row.id === selection) ?? progress.current ?? progress.commands.at(-1)
  const display = useDisplayTranslation([progress.reason ?? '', ...progress.commands.map(command => command.purpose ?? ''), progress.samples.at(-1)?.summary ?? ''], lang)
  const states: Record<string,string> = {verified:t('✓ 恢复已验证','✓ Recovery verified'),failed:t('验证失败','Verification failed'),paused:t('已暂停，等待处理','Paused; action required'),cleared:t('当前异常已消失','Condition no longer observed'),running:t('命令执行中','Command running'),verifying:t('正在回读验证','Verification in progress'),observing:t('正在观测与校验','Observing and checking'),waiting:t('等待执行记录','Awaiting execution records')}
  return <section className="execution-progress" aria-label={t('实际执行过程','Actual execution progress')}>
    <header><h2>{t('实际执行过程','Actual execution progress')}</h2><strong data-execution-state={progress.status}>{states[progress.status]}</strong></header>
    <p className="execution-sync">{t('每 3 秒同步执行账本','Execution ledger syncs every 3 seconds')}{progress.lastReadAt ? ` · ${new Date(progress.lastReadAt).toLocaleTimeString(zh?'zh-CN':'en-GB',{hour12:false})}` : ''}</p>
    {progress.readError && <p role="alert">{t('同步失败，保留上次记录并继续重试。','Sync failed; keeping the previous records and retrying.')} {progress.readError}</p>}
    {display.error && <p role="alert">{display.error} <button onClick={display.retry}>Retry translation</button></p>}
    {progress.reason && <p><b>{t('当前依据','Current basis')}</b> · {display.translate(progress.reason)}</p>}
    <div className="execution-verification"><b>{progress.verified ? '✓ ' : ''}{t('恢复验证','Recovery verification')}</b><span>{progress.verified ? t('已落库恢复结果','Recorded recovery result') : progress.verificationPassed ? t('观察窗通过，等待最终恢复结果','Observation window passed; awaiting final recovery result') : t('等待通过验证','Awaiting verified recovery')}</span><span>{t('已记录采样','Recorded samples')} {progress.sampleCount ?? t('未记录','Not recorded')}{progress.sampleCount != null && progress.samples.length < progress.sampleCount ? ` · ${progress.samples.length ? `${t('逐次明细','Individual records')} ${progress.samples.length}/${progress.sampleCount}` : t('逐次明细未记录','Individual sample details not recorded')}` : ''} · {t('快速检查明细','Fast check records')} {progress.fastSamples ?? t('未记录','Not recorded')} · {t('稳定性检查明细','Stability check records')} {progress.stableSamples ?? t('未记录','Not recorded')}</span></div>
    {progress.samples.at(-1)?.summary && <p><b>{t('最近验证采样','Latest verification sample')}</b> · {display.translate(progress.samples.at(-1)!.summary!)} · {progress.samples.at(-1)?.at}</p>}
    {progress.samples.length > 0 ? <details className="execution-samples" open><summary>{t('逐次回读结果', 'Individual readback results')} · {progress.samples.length}</summary><table><thead><tr><th>{t('时间', 'Time')}</th><th>{t('管理连接', 'Management')}</th><th>{t('业务检查', 'Business check')}</th><th>{t('目标状态', 'Target')}</th></tr></thead><tbody>{progress.samples.map((sample, index) => <tr key={`${sample.at}-${index}`}><td>{new Date(sample.at).toLocaleTimeString(zh ? 'zh-CN' : 'en-GB', {hour12:false})}</td>{['management_ok','guard_ok','target_ok'].map(key => <td key={key}>{sample.reading?.[key] === true ? t('✓ 通过', '✓ Passed') : sample.reading?.[key] === false ? t('! 未通过', '! Failed') : t('未记录', 'Not recorded')}</td>)}</tr>)}</tbody></table></details> : null}
    {progress.failed || progress.paused ? <p className="execution-next"><b>{t('下一步','Next step')}</b> · {progress.failed ? t('检查下方失败返回及回退记录，复核原因后再决定重试。','Inspect the failed return and rollback records below; review the cause before deciding to retry.') : t('根据当前依据补齐前置条件，核实后重新发起已授权动作。','Address the stated prerequisite, verify it, then initiate the authorized action again.')}</p> : null}
    <div className="execution-command-list" aria-label={t('实际命令记录','Recorded commands')}>{[...progress.commands].reverse().map(command => <button key={command.id} aria-pressed={selected?.id === command.id} onClick={()=>setSelection(command.id)}><span>{command.state === 'receipt' ? t('执行回执（输出未采集）','Execution receipt (output not captured)') : command.state === 'running' ? t('▶ 执行中','▶ Running') : command.state === 'failed' ? t('! 非零返回或执行错误','! Nonzero return or execution error') : t('已返回','Returned')}{command.rc != null ? ` · exit ${command.rc}` : ''}</span><code>{command.argv.join(' ')}</code></button>)}</div>
    {selected ? <div className="execution-command-detail"><p><b>{t('动作目的','Action purpose')}</b> · {selected.purpose ? (zh ? purposeChinese[selected.purpose] ?? selected.purpose : display.translate(selected.purpose)) : t('该命令记录未提供目的说明','This command record does not include a purpose')}</p><pre className="execution-argv">$ {selected.argv.join(' ')}</pre><p>{selected.started_at ?? selected.at}{selected.finished_at ? ` → ${selected.finished_at}` : ''}{selected.duration_ms != null ? ` · ${selected.duration_ms} ms` : ''}</p><h3>{t('返回结果','Returned output')}</h3>{selected.state === 'receipt' ? <><p>{t('执行回执已记录；退出码、标准输出和错误输出未采集。','Execution receipt recorded; exit code, stdout and stderr were not captured.')}</p>{selected.receipt && <details><summary>{t('原始动作回执','Original action receipt')}</summary><pre>{JSON.stringify(selected.receipt,null,2)}</pre></details>}</> : selected.state === 'running' ? <p role="status">{t('命令已开始，等待工具返回。','Command started; awaiting tool output.')}</p> : <><p>{selected.rc == null ? t('退出码未记录','Exit code not recorded') : `exit ${selected.rc}`}</p><pre>{selected.stdout || selected.out || (zh ? '标准输出为空' : 'Standard output is empty')}</pre>{(selected.stderr || selected.error) && <pre>{selected.stderr || selected.error}</pre>}{selected.truncated && <small>{t('输出已截断，显示执行账本保留的部分。','Output truncated; showing the portion retained in the execution ledger.')}</small>}</>}</div> : <p>{t('当前周期尚无命令开始或返回记录。','No command start or return is recorded in the current cycle.')}</p>}
  </section>
}
