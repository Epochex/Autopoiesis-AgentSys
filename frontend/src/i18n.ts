export type Lang = 'en' | 'zh'

// Evidence-specific root-cause labels. Keep risk findings and healthy observations distinct.
const RC: Record<string, [string, string]> = {
  admin_bruteforce_lockout: ['Admin brute force triggered lockout', '管理口遭暴力尝试并触发锁定'],
  internal_policy_deny_expected: ['High-volume internal flows denied by policy', '内部主机大量访问被策略拒绝'],
  benign_session_clash: ['Informational session-clash events', '信息性会话冲突事件'],
  dhcp_service_healthy: ['DHCP leases issued normally', 'DHCP 租约正常发放'],
  security_posture_current: ['FortiGuard updates succeeded', 'FortiGuard 更新成功'],
  device_service_port_probe_contained: ['Camera/DVR service ports probed and denied', '摄像机/DVR 服务端口遭探测并被拒绝'],
  firewall_resource_healthy: ['Low CPU, memory and session load', 'CPU、内存与会话负载较低'],
  unknown: ['Root cause unknown', '根因来源未知'],
}

export const rc = (k: string, lang: Lang) => (RC[k] ? RC[k][lang === 'zh' ? 1 : 0] : k)
