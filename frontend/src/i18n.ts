export type Lang = 'en' | 'zh'

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

export const rc = (k: string, lang: Lang) => (RC[k] ? RC[k][lang === 'zh' ? 1 : 0] : k)
