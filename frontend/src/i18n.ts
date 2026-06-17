export type Lang = 'en' | 'zh'

const T: Record<string, [string, string]> = {
  failedLogins: ['failed admin logins', '失败登录'],
  denied: ['denied flows', '拒绝流量'],
  lockouts: ['lockouts', '锁定'],
  sources: ['source IPs', '源IP'],
  attackers: ['attackers', '攻击源'],
  gateway: ['gateway', '网关'],
  ports: ['blocked ports', '拦截端口'],
  sink: ['syslog sink', '日志汇聚'],
  engine: ['engine', '引擎'],
  confidence: ['confidence', '置信'],
  verified: ['verified', '已校验'],
  accuracy: ['accuracy', '准确率'],
  withControl: ['skill control', '技能调度'],
  withoutControl: ['no control', '无调度'],
}

// affirmative root-cause labels
const RC: Record<string, [string, string]> = {
  admin_bruteforce_lockout: ['Admin lockout · exposure controlled', '管理口锁定 · 暴露面已控'],
  internal_policy_deny_expected: ['Policy enforcing access control', '策略按设计拦截'],
  benign_session_clash: ['Session-clash housekeeping', '会话冲突 · 常规日志'],
  dhcp_service_healthy: ['DHCP allocation healthy', 'DHCP 分配正常'],
  security_posture_current: ['Security posture current', '安全态势 · 最新'],
  device_service_port_probe_contained: ['Device-port probes contained', '设备端口探测已遏制'],
  firewall_resource_healthy: ['Firewall headroom ample', '防火墙余量充足'],
  unknown: ['Pending', '待分类'],
}

export const t = (k: string, lang: Lang) => (T[k] ? T[k][lang === 'zh' ? 1 : 0] : k)
export const rc = (k: string, lang: Lang) => (RC[k] ? RC[k][lang === 'zh' ? 1 : 0] : k)
