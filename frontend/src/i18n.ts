export type Lang = 'en' | 'zh'

type Dict = Record<string, { en: string; zh: string }>

const DICT: Dict = {
  brandSub: { en: 'Network RCA · live R230 FortiGate', zh: '网络根因分析 · R230 真实 FortiGate' },
  live: { en: 'real dataset live', zh: '真实数据已接入' },
  blocked: { en: 'dataset staging', zh: '数据接入中' },
  syslogOk: { en: 'R230 syslog reachable', zh: 'R230 syslog 可达' },
  syslogDown: { en: 'R230 syslog standby', zh: 'R230 syslog 待连' },
  refresh: { en: 'Refresh', zh: '刷新' },
  reasoner: { en: 'Reasoner', zh: '推理引擎' },
  source: { en: 'Source', zh: '数据源' },
  window: { en: 'Window', zh: '时间窗' },
  failedLogins: { en: 'Failed admin logins', zh: '失败登录' },
  deniedFlows: { en: 'Denied flows', zh: '拒绝流量' },
  topPort: { en: 'Top denied port', zh: '最多拒绝端口' },
  srcIps: { en: 'src IPs', zh: '个源IP' },
  lockouts: { en: 'lockouts', zh: '次锁定' },
  hits: { en: 'hits', zh: '次' },
  cases: { en: 'Held-out cases', zh: '留出案例' },
  pipeline: { en: 'RCA pipeline', zh: 'RCA 推理管道' },
  diagnosis: { en: 'Diagnosis', zh: '诊断结论' },
  confidence: { en: 'confidence', zh: '置信度' },
  evidence: { en: 'Cited evidence', zh: '引用证据' },
  actions: { en: 'Recommended actions', zh: '建议动作' },
  readonly: { en: 'readonly', zh: '只读' },
  verifierPassed: { en: 'verifier passed', zh: '校验通过' },
  verifierFailed: { en: 'verifier failed', zh: '校验失败' },
  ablation: { en: 'Ablation on real held-out', zh: '真实留出集消融对照' },
  ablationNote: {
    en: 'Skill control holds root-cause accuracy at 100%. The controller is the module that secures precision under heavy evidence volume.',
    zh: '技能调度让根因准确率稳定在 100%。调度层是高证据量下守住精度的核心模块。',
  },
  denyByPort: { en: 'Denied flows by destination port', zh: '按目的端口的拒绝流量' },
  accuracy: { en: 'root-cause accuracy', zh: '根因准确率' },
  providerError: { en: 'Engine standby', zh: '引擎待接入' },
  noDataset: { en: 'Real held-out dataset staging', zh: '真实留出数据集接入中' },
  topology: { en: 'Live network topology', zh: '实时网络拓扑' },
  attackers: { en: 'External attackers', zh: '外部攻击源' },
  internalHosts: { en: 'Internal hosts', zh: '内网主机' },
  deniedPorts: { en: 'Blocked ports', zh: '被拦端口' },
  syslogSink: { en: 'R230 syslog sink', zh: 'R230 日志汇聚' },
  consoleNode: { en: 'selfevo console', zh: 'selfevo 控制台' },
  inspect: { en: 'Stage detail', zh: '阶段详情' },
  clickStage: { en: 'Click a stage in the pipeline to inspect it', zh: '点击管道中的某个阶段查看详情' },
  overview: { en: 'Overview', zh: '总览' },
  evidenceVol: { en: 'attack flows', zh: '攻击流' },
  denyVol: { en: 'deny flows', zh: '拒绝流' },
  // pipeline stage labels
  st_alert: { en: 'Alert', zh: '告警' },
  st_memory: { en: 'Memory', zh: '记忆检索' },
  st_skills: { en: 'Skills (top-k)', zh: '技能选择' },
  st_tools: { en: 'Readonly probe', zh: '只读取证' },
  st_context: { en: 'Context', zh: '上下文压缩' },
  st_verify: { en: 'Verifier', zh: '校验' },
  st_diagnosis: { en: 'Diagnosis', zh: '诊断' },
}

// Human labels for the real root-cause keys produced by the framework.
const ROOT_CAUSE: Record<string, { en: string; zh: string }> = {
  admin_bruteforce_lockout: {
    en: 'External brute-force triggered admin lockout · exposure-surface control',
    zh: '外部爆破触发管理口锁定 · 暴露面治理',
  },
  internal_policy_deny_expected: {
    en: 'Policy enforces internal access control as designed',
    zh: '策略按设计拦截内网越权流量',
  },
  benign_session_clash: { en: 'Routine session-clash housekeeping', zh: '会话冲突属常规运维日志' },
  unknown: { en: 'Pending classification', zh: '待分类' },
}

export function makeT(lang: Lang) {
  return (key: string) => DICT[key]?.[lang] ?? key
}

export function rootCauseLabel(key: string, lang: Lang): string {
  return ROOT_CAUSE[key]?.[lang] ?? key
}
