/* Label tables and formatters for the environment blocks.
 *
 * Split out of the component module so that file exports components only:
 * mixing the two breaks fast refresh, and these tables are also read by the
 * page that composes the blocks. */
import type { EnvCellState } from '../types'

export const CELL_LABEL: Record<EnvCellState, [string, string]> = {
  leased: ['已绑定身份', 'IDENTITY BOUND'],
  unbound: ['有流量·无绑定', 'TRAFFIC · NO BINDING'],
  contested: ['地址争用', 'CONTESTED'],
  silent: ['无观测', 'NO OBSERVATION'],
}

export const COVERAGE_LABEL: Record<string, [string, string]> = {
  covered: ['可检测', 'COVERED'],
  partial: ['部分', 'PARTIAL'],
  blind: ['盲区', 'BLIND'],
}

export const VERIFY_LABEL: Record<string, [string, string]> = {
  confirmed: ['实时确认仍在', 'CONFIRMED LIVE'],
  unverifiable: ['无实时源可复核', 'NO LIVE SOURCE'],
  resolved: ['已消失', 'RESOLVED'],
  archived: ['归档 · 已记录', 'ARCHIVED'],
}

export const FAULT_LABEL: Record<string, [string, string]> = {
  duplicate_ip_dhcp: ['地址重复 · 双方走 DHCP', 'DUPLICATE ADDRESS · BOTH VIA DHCP'],
  duplicate_ip_static: ['地址重复 · 一方为静态配置', 'DUPLICATE ADDRESS · ONE STATIC'],
  address_unmanaged: ['作用域内无租约绑定', 'IN-SCOPE ADDRESS WITHOUT LEASE'],
  lease_churn: ['租约反复重建', 'LEASE REBUILT CONTINUOUSLY'],
  host_multi_address: ['单主机多地址', 'HOST HOLDS MULTIPLE ADDRESSES'],
  unmanaged_identity: ['身份无法归属台账', 'IDENTITY NOT IN INVENTORY'],
  pool_pressure: ['地址池接近耗尽', 'SCOPE NEAR EXHAUSTION'],
  session_tuple_clash: ['会话元组冲突', 'SESSION TUPLE CLASH'],
  mgmt_bruteforce: ['管理面凭据攻击', 'MANAGEMENT-PLANE CREDENTIAL ATTACK'],
  gateway_reachability: ['主机无法到达网关', 'HOST CANNOT REACH GATEWAY'],
  resolver_failure: ['DNS 请求发出但无应答', 'DNS QUERIES UNANSWERED'],
  l2_loop_macflap: ['MAC 在端口间漂移', 'MAC FLAPPING BETWEEN PORTS'],
  rogue_dhcp: ['网段内存在第二个 DHCP', 'SECOND DHCP SERVER ON SEGMENT'],
  host_config_drift: ['重启后主机网络配置被改写', 'HOST NETWORK CONFIG REWRITTEN ON REBOOT'],
}

export const pick = (pair: [string, string] | undefined, zh: boolean, fallback: string) =>
  pair ? (zh ? pair[0] : pair[1]) : fallback

export const num = (value: number) => value.toLocaleString('en-US')
export const clock = (value: string | null | undefined) =>
  value ? value.replace('T', ' ').replace(/\..*Z?$/, '').replace('Z', '') : '—'

export const ageText = (seconds: number | null, zh: boolean) => {
  if (seconds == null) return '—'
  if (seconds < 90) return zh ? `${Math.round(seconds)} 秒前` : `${Math.round(seconds)}s ago`
  if (seconds < 5400) return zh ? `${Math.round(seconds / 60)} 分钟前` : `${Math.round(seconds / 60)}m ago`
  return zh ? `${(seconds / 3600).toFixed(1)} 小时前` : `${(seconds / 3600).toFixed(1)}h ago`
}

