export type Lang = 'en' | 'zh'

type Dict = Record<string, { en: string; zh: string }>

const DICT: Dict = {
  brandSub: { en: 'Network RCA · live R230 FortiGate', zh: '网络根因分析 · R230 真实 FortiGate' },
  live: { en: 'real dataset live', zh: '真实数据已接入' },
  blocked: { en: 'dataset blocked', zh: '数据未就绪' },
  syslogOk: { en: 'R230 syslog reachable', zh: 'R230 syslog 可达' },
  syslogDown: { en: 'R230 syslog down', zh: 'R230 syslog 不通' },
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
    en: 'Removing skill control (full_tools) lets the dominant brute-force evidence swamp the deny case → misdiagnosis. The framework (skill control on) keeps accuracy.',
    zh: '去掉技能调度（full_tools）后，占主导的爆破证据淹没了 deny 案例 → 误判。开启技能调度的框架保持准确。',
  },
  denyByPort: { en: 'Denied flows by destination port', zh: '按目的端口的拒绝流量' },
  accuracy: { en: 'root-cause accuracy', zh: '根因准确率' },
  providerError: { en: 'Provider failed', zh: '推理引擎失败' },
  noDataset: { en: 'Real held-out dataset not available', zh: '真实留出数据集不可用' },
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
    en: 'Admin brute-force → lockout (exposure, not a fault)',
    zh: '管理口爆破 → 锁定（是暴露面，不是设备故障）',
  },
  internal_policy_deny_expected: {
    en: 'Internal policy-deny is expected (not an outage)',
    zh: '内网策略拒绝属预期（不是故障）',
  },
  benign_session_clash: { en: 'Benign session-clash logs', zh: '无害的 session-clash 日志' },
  unknown: { en: 'Unknown', zh: '未知' },
}

export function makeT(lang: Lang) {
  return (key: string) => DICT[key]?.[lang] ?? key
}

export function rootCauseLabel(key: string, lang: Lang): string {
  return ROOT_CAUSE[key]?.[lang] ?? key
}
