export interface IncidentCopyFacts {
  faultClass?: string
  managedDevice?: string
  sourceIp?: string
  destinationIp?: string
  destinationPort?: number | string
  service?: string
  action?: string
  denyCount?: number
  windowSeconds?: number
  failedLogins?: number
  distinctSources?: number
  measured?: Record<string, unknown>
}

const count = (value: unknown): string =>
  typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('en-US') : String(value ?? 'N/A')

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []

export function humanizeIncidentSummary(
  summary: string,
  facts: IncidentCopyFacts = {},
  zh: boolean,
  subject?: string,
): string {
  if (!zh) return summary
  const measured = facts.measured ?? {}
  const asset = subject || facts.sourceIp || facts.managedDevice || '该对象'
  const handovers = measured.handovers
  const captures = measured.captures
  const macs = strings(measured.macs)
  if (typeof handovers === 'number' && typeof captures === 'number') {
    return `${asset} 在 ${count(captures)} 次采集中出现 ${count(handovers)} 次 MAC 归属变化，涉及 ${count(macs.length)} 个 MAC。`
  }
  const arpMacs = strings(measured.arp_macs)
  const leasedMacs = strings(measured.dhcp_leased_macs)
  if (arpMacs.length && leasedMacs.length) {
    return `${asset} 当前由 ${arpMacs.join('、')} 响应，DHCP 租约登记为 ${leasedMacs.join('、')}，两者不一致。`
  }
  if (typeof facts.failedLogins === 'number') {
    return `${facts.managedDevice || asset} 在 ${count(facts.windowSeconds ?? 60)} 秒内出现 ${count(facts.failedLogins)} 次管理员登录失败，来源 ${count(facts.distinctSources)} 个地址。`
  }
  if (facts.sourceIp && facts.destinationIp && typeof facts.denyCount === 'number') {
    const target = `${facts.destinationIp}${facts.destinationPort ? `:${facts.destinationPort}` : ''}`
    const outcome = facts.action === 'deny' ? '，防火墙已拒绝。' : `，设备结果为 ${facts.action || '待确认'}。`
    return `${facts.sourceIp} 在 ${count(facts.windowSeconds ?? 60)} 秒内尝试访问 ${target} ${count(facts.denyCount)} 次${outcome}`
  }
  const drift = summary.match(/^(\S+) changed L2 owner (\d+)x across (\d+) captures, between (\d+) MACs$/i)
  if (drift) return `${drift[1]} 在 ${count(Number(drift[3]))} 次采集中出现 ${count(Number(drift[2]))} 次 MAC 归属变化，涉及 ${drift[4]} 个 MAC。`
  const conflict = summary.match(/^(\S+) answers as (\S+) but is leased to (\S+)$/i)
  if (conflict) return `${conflict[1]} 当前由 ${conflict[2]} 响应，DHCP 租约登记为 ${conflict[3]}，两者不一致。`
  return summary
}

export function actionLabel(
  decision: { state?: string; action?: string; disposition?: string } | null | undefined,
  zh: boolean,
): string {
  if (decision?.action) return decision.action
  if (decision?.state === 'escalated') return zh ? '转人工确认归属并制定变更' : 'OPERATOR OWNERSHIP CHECK REQUIRED'
  if (decision?.state === 'resolved') return zh ? '继续观察，无需变更' : 'MONITOR; NO CHANGE REQUIRED'
  return zh ? '下一步：补齐证据并形成处置决定' : 'NEXT: COMPLETE EVIDENCE AND DECIDE'
}

const MEMORY_ROOT_LABELS: Record<string, string> = {
  duplicate_ip_static: '的地址当前归属与登记信息冲突',
  service_failed: '上的关键服务处于失败状态',
  admin_bruteforce_lockout: '的管理员登录暴力尝试触发锁定',
}

const MEMORY_PROBE_LABELS: Record<string, string> = {
  'adapter:environment_finding': '环境观测',
  'adapter:admin_auth_window': '认证时间窗统计',
  'systemctl --failed --no-legend': '服务状态检查',
}

export function humanizeMemoryRecord(
  raw: string,
  tags: string[],
  assetIds: string[],
  zh: boolean,
): string {
  if (!zh) return raw
  const root = tags.find((tag) => tag.startsWith('root:'))?.slice(5)
  const probe = tags.find((tag) => tag.startsWith('probe:'))?.slice(6)
  if (root) {
    const subject = assetIds.length ? assetIds.join('、') : '该对象'
    const finding = MEMORY_ROOT_LABELS[root] ?? root
    const evidence = probe ? (MEMORY_PROBE_LABELS[probe] ?? probe) : '已保存证据'
    return `已确认 ${subject} ${finding}，证据来自${evidence}。`
  }
  if (raw.startsWith('Dell R230 runs Ubuntu 22.04')) {
    return 'Dell R230（192.168.1.23）运行 Ubuntu 22.04，eno2 是展厅设备网主接口，eno1 为备用接口。'
  }
  if (raw.startsWith('When an interface reports no-carrier')) {
    return '网卡存在但无载波时，先检查线缆和对端交换机端口状态，再判断硬件故障。'
  }
  if (raw.startsWith('When two routed FortiGate connected networks')) {
    return '两个经 FortiGate 路由的网段互相不可达时，先核对路由和防火墙策略，再检查交换机 VLAN。'
  }
  if (raw.startsWith('For WAN VIP publishing issues')) {
    return '排查公网端口映射时，同时核对 VIP 映射与入站策略的服务匹配。'
  }
  if (raw.startsWith('FortiGate 100E has office network')) {
    return 'FortiGate 100E：办公网位于 port3/port4，展厅设备网位于 port11/port12，AP 管理网位于 port5。'
  }
  return raw
}
