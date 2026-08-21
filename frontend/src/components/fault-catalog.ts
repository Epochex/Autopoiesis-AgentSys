/* The prioritized fault-family catalog behind the verdict ledger.
 *
 * This is the full fault-family space of this network — every fault_class the
 * detectors emit, every held-out root cause, and the recon risk kinds — folded
 * into nine families and ranked by operational priority. Each family carries
 * the automation verdict the remediation contract enforces:
 *
 *   auto     the fix is monotonic (target already failed, cannot get worse),
 *            single-endpoint, and read-back verified — the system may execute
 *            and keep following up on its own
 *   guarded  automatic only inside guardrails: allowlists, auto-expiry,
 *            maintenance windows, post-change watch with rollback
 *   manual   touches the shared forwarding plane or a third-party device in
 *            service — a plan is drafted, a human approves and executes
 *
 * One invariant outranks every playbook: the Tailscale path (tailscale0,
 * tailscaled, 100.64.0.0/10, UDP 41641, DERP 443) is the only remote-work
 * route into this network and must never be blocked, restarted or rerouted by
 * any fix — automatic or human-approved. Every ban/policy/interface step is
 * preconditioned on the target not being on that path. */

export type Automation = 'auto' | 'guarded' | 'manual'
export type StepRisk = 'readonly' | 'auto' | 'gated'

export interface CatalogStep {
  risk: StepRisk
  what: [string, string]
  command: string
}

export interface FaultFamily {
  id: string
  prio: number
  severity: 'critical' | 'high' | 'medium' | 'low'
  automation: Automation
  title: [string, string]
  /** join keys against live EnvFinding.fault_class */
  faultClasses: string[]
  /** the live signal that confirms this family, shown in the source column */
  confirm: [string, string]
  /** why the automation verdict is what it is */
  rationale: [string, string]
  /** hard protection note, rendered as a warning strip when present */
  protectedNote?: [string, string]
  playbook: CatalogStep[]
}

export const AUTOMATION_LABEL: Record<Automation, [string, string]> = {
  auto: ['自动闭环', 'AUTO'],
  guarded: ['护栏内自动', 'GUARDED'],
  manual: ['必须人工', 'HUMAN'],
}

export const STEP_LABEL: Record<StepRisk, [string, string]> = {
  readonly: ['验证', 'VERIFY'],
  auto: ['自动修复', 'AUTO FIX'],
  gated: ['需人审', 'APPROVAL'],
}

export const TAILSCALE_GUARD: [string, string] = [
  '保护线路 · TAILSCALE — tailscale0 / tailscaled / 100.64.0.0/10 / UDP 41641 与 DERP(443) 永不阻断、永不重启、永不改路由。这是远程开发进入本网的唯一通道;所有封禁、策略与接口修复动作在执行前强制校验目标不在此路径上。',
  'PROTECTED PATH · TAILSCALE — tailscale0 / tailscaled / 100.64.0.0/10 / UDP 41641 and DERP (443) are never blocked, restarted or rerouted. This is the only remote-work route into this network; every ban, policy and interface fix is preconditioned on the target not being on this path.',
]

/** archived incident ids → owning family id */
export const INCIDENT_FAMILY: Record<string, string> = {
  'inc-192-168-1-23-dual-mac-arp-drift': 'fam-address-ownership',
  'inc-192-168-1-27-cloud-init-network-drift': 'fam-host-config-drift',
}

export const FAULT_CATALOG: FaultFamily[] = [
  {
    id: 'fam-address-ownership',
    prio: 0,
    severity: 'critical',
    automation: 'manual',
    title: ['地址所有权冲突', 'ADDRESS OWNERSHIP CONTENTION'],
    faultClasses: ['duplicate_ip_static', 'duplicate_ip_dhcp', 'rogue_dhcp'],
    confirm: ['ARP 双 MAC 交替 + 归属探测', 'ARP DUAL-MAC + OWNERSHIP PROBE'],
    rationale: [
      '修复必须触碰带业务的第三方设备(占用方静态 IP)或共享 DHCP 作用域——.23 双 MAC 是本网历史最重事故,误改即断该设备全部业务。',
      'The fix touches a third-party device in service (the squatter’s static IP) or the shared DHCP scope — the .23 dual-MAC drift is this network’s worst recorded incident.',
    ],
    playbook: [
      { risk: 'readonly', what: ['本机 ARP 视角:两个 MAC 交替出现即在争用', 'Local ARP view: two alternating MACs = live contention'], command: 'ip neigh show 192.168.1.23' },
      { risk: 'readonly', what: ['FortiGate 侧 ARP 归属交叉核对', 'Cross-check ownership from the FortiGate ARP table'], command: 'diagnose sys arp list | grep 192.168.1.23' },
      { risk: 'readonly', what: ['16 次采样归属探测:量化每个服务端口的不可达占比', 'Ownership probe, 16 samples: quantify per-port unreachable share'], command: 'POST /api/rca/environment/probe {"ip":"192.168.1.23","samples":16}' },
      { risk: 'gated', what: ['在占用方设备管理口改静态地址至保留段,或于 FortiGate DHCP 为服务器 MAC 建保留', 'Re-address the squatter on its own admin UI, or pin a DHCP reservation for the server MAC'], command: 'config system dhcp reserved-address  # FortiGate · 人工执行' },
      { risk: 'gated', what: ['变更后回读:连续 16 次采样归属稳定才允许关单', 'Read-back: 16 consecutive stable-ownership samples before the finding may close'], command: 'POST /api/rca/environment/probe → verdict=stable' },
    ],
  },
  {
    id: 'fam-host-config-drift',
    prio: 1,
    severity: 'high',
    automation: 'auto',
    title: ['主机网络配置漂移', 'HOST NETWORK CONFIG DRIFT'],
    faultClasses: ['host_config_drift', 'gateway_reachability'],
    confirm: ['配置基线 diff + 网关可达性', 'CONFIG BASELINE DIFF + GATEWAY REACH'],
    rationale: [
      '单调性成立:接口已无载波、配置已漂移,修复不可能比现状更差;且全部动作落在单台主机,不触碰共享面。前提核验必须多源(7/18 教训:外部视角的“已死”可能只是链路假死)。',
      'Monotonic: the interface is already dead and the config already drifted, so the fix cannot make things worse; every action is single-host. Preconditions need multi-source proof (the 7/18 lesson: “dead” from outside may be a downed link on a live box).',
    ],
    protectedNote: [
      '接口操作仅限已无载波的物理网卡;tailscale0 与 tailscaled 永不触碰。',
      'Interface actions only on physical NICs already without carrier; tailscale0 and tailscaled are never touched.',
    ],
    playbook: [
      { risk: 'readonly', what: ['接口必须已 DOWN/NO-CARRIER 才允许自动处置', 'The NIC must already read DOWN/NO-CARRIER before any auto action'], command: 'ip -br link show eno1' },
      { risk: 'readonly', what: ['cloud-init 是否在重启时改写了网络配置', 'Did cloud-init rewrite the network config on reboot'], command: 'cloud-init status --long' },
      { risk: 'readonly', what: ['当前 netplan 与基线逐行 diff', 'Line diff of live netplan against the pinned baseline'], command: 'diff <(netplan get) /data/baseline/netplan-r450.yaml' },
      { risk: 'auto', what: ['bounce 已断接口——已经是 0,不可能更差', 'Bounce the already-dead NIC — it is at zero, cannot go lower'], command: 'ip link set eno1 down && ip link set eno1 up' },
      { risk: 'auto', what: ['预写 cloud-init 网络禁用文件,不即时 apply,重启生效——写文件本身零运行时影响', 'Pre-write the cloud-init network-disable file; no immediate apply, takes effect on reboot'], command: 'printf "network: {config: disabled}\\n" > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg' },
      { risk: 'gated', what: ['活主机上 netplan apply 需人审:配置错误会瞬断所有会话', 'netplan apply on a live host needs approval: a bad config drops every session at once'], command: 'netplan apply  # 人工执行' },
    ],
  },
  {
    id: 'fam-mgmt-bruteforce',
    prio: 2,
    severity: 'high',
    automation: 'guarded',
    title: ['管理面爆破与锁定', 'MANAGEMENT-PLANE BRUTEFORCE & LOCKOUT'],
    faultClasses: ['mgmt_bruteforce'],
    confirm: ['FortiGate 事件日志 · 连续认证失败', 'FORTIGATE EVENT LOG · AUTH FAILURES'],
    rationale: [
      '封禁本身可逆(自动过期),但误封合法源等于自断管理通道,所以只在白名单护栏内自动:台账资产、Tailscale 网段、当前会话源永不进封禁名单。',
      'A ban is reversible (auto-expiry), but banning a legitimate source severs your own management path — so automation runs only inside allowlist guardrails.',
    ],
    protectedNote: [
      '永不封禁 100.64.0.0/10、资产台账内 IP、当前 SSH 会话源;trusthost 收紧永远人审(自锁风险)。',
      'Never ban 100.64.0.0/10, inventoried assets, or the current SSH session source; trusthost tightening is always human-approved (self-lockout risk).',
    ],
    playbook: [
      { risk: 'readonly', what: ['失败次数、源 IP、时间窗画像', 'Failure counts, source IPs, time-window profile'], command: 'skill: check_admin_auth_failures' },
      { risk: 'readonly', what: ['锁定事件与被锁账号', 'Lockout events and the locked account'], command: 'skill: check_admin_lockout' },
      { risk: 'readonly', what: ['攻击源对照资产台账与 Tailscale 网段——白名单命中即停止,转人工', 'Source vs. inventory and the Tailscale range — an allowlist hit stops automation, hands to a human'], command: 'grep -F <src_ip> /data/inventory/assets.tsv; ipcalc <src_ip> 100.64.0.0/10' },
      { risk: 'auto', what: ['源确认为外部攻击后解锁账号——解锁只恢复访问,不可能断任何东西', 'Unlock the admin account once the source is confirmed external — unlocking only restores access'], command: 'execute log filter … && config system admin (unlock)' },
      { risk: 'gated', what: ['封禁攻击源,24h 自动过期,白名单强制排除', 'Ban the attacking source with 24h auto-expiry, allowlist enforced'], command: 'diagnose user banned-ip add src4 <src_ip> 86400' },
    ],
  },
  {
    id: 'fam-perception-selfheal',
    prio: 3,
    severity: 'high',
    automation: 'auto',
    title: ['感知链路自愈', 'PERCEPTION PIPELINE SELF-HEAL'],
    faultClasses: [],
    confirm: ['水位线停走 · systemd failed · 磁盘阈值', 'WATERMARK STALL · SYSTEMD FAILED · DISK'],
    rationale: [
      '采集器已崩、水位线已停,重启一个 failed 单元不可能更差;感知断流时其余一切判定都在盲飞,所以这族排进高优先级。清理只碰已轮转且已投递的日志。',
      'A failed collector restarted cannot get worse; while perception is down every other verdict flies blind, hence the high priority. Cleanup touches only rotated, already-shipped logs.',
    ],
    playbook: [
      { risk: 'readonly', what: ['列出 failed 单元——只有 is-failed 的允许自动重启', 'List failed units — only is-failed units qualify for auto-restart'], command: 'systemctl --failed --no-legend' },
      { risk: 'readonly', what: ['事件流水位线是否停走', 'Has the event-stream watermark stalled'], command: 'curl -s localhost:8026/api/healthz | jq .runtime' },
      { risk: 'readonly', what: ['日志盘余量', 'Log volume headroom'], command: 'df -h /data' },
      { risk: 'auto', what: ['重启已 failed 的采集单元,重启后回读水位线恢复流动', 'Restart the failed collector unit; read back a moving watermark'], command: 'systemctl restart <failed-unit> && sleep 10 && curl -s localhost:8026/api/healthz' },
      { risk: 'auto', what: ['按 14 天保留清理已轮转日志(仅 *.gz 且已投递)', 'Retention cleanup: rotated *.gz already shipped, older than 14 days'], command: 'find /data/logs -name "*.gz" -mtime +14 -delete' },
    ],
  },
  {
    id: 'fam-dhcp-lifecycle',
    prio: 4,
    severity: 'medium',
    automation: 'guarded',
    title: ['DHCP 生命周期', 'DHCP LIFECYCLE'],
    faultClasses: ['lease_churn', 'pool_pressure', 'address_unmanaged', 'host_multi_address', 'unmanaged_identity'],
    confirm: ['租约事件流 + 池占用率', 'LEASE EVENTS + POOL OCCUPANCY'],
    rationale: [
      '扩池是增量动作,不动已有租约;但 ARP/流量历史证明不了一台断电静态设备不存在于新段,所以只在历史核验护栏内自动。作用域重构动共享服务,必须人审。',
      'Pool extension is additive and leaves existing leases alone; but ARP/flow history cannot prove a powered-off static device is absent from the new range, so it runs only inside the verification guardrail.',
    ],
    playbook: [
      { risk: 'readonly', what: ['池占用率与租约重建频率', 'Pool occupancy and lease-churn rate'], command: 'skill: check_dhcp_service' },
      { risk: 'readonly', what: ['候选扩池段 7 天 ARP/流量历史必须零命中', 'Candidate range must show zero ARP/flow hits over 7 days'], command: 'clickhouse: SELECT count() FROM flows WHERE ip IN <candidate_range> AND ts > now()-7*86400' },
      { risk: 'gated', what: ['增量扩池:仅追加经核验无主的段,不改已有段;回读新租约正常发放', 'Additive extension only, verified-unowned range; read back fresh leases being served'], command: 'config system dhcp server → edit ip-range (append)' },
      { risk: 'gated', what: ['租约时长调整与作用域重构——池压力下加长租约会恶化耗尽,人审', 'Lease-time changes and scope restructure — longer leases worsen exhaustion under pool pressure'], command: 'config system dhcp server → set lease-time  # 人工执行' },
    ],
  },
  {
    id: 'fam-policy-reachability',
    prio: 5,
    severity: 'medium',
    automation: 'manual',
    title: ['策略与可达性', 'POLICY & REACHABILITY'],
    faultClasses: ['l2_loop_macflap', 'resolver_failure'],
    confirm: ['策略拒绝画像 + 流量基线对比', 'DENY PROFILE + TRAFFIC BASELINE'],
    rationale: [
      'FortiGate 策略、VLAN、fortilink、LACP 全部是共享转发面,一次误改全网受影响——永远不存在“绝对安全”的自动修复,只有预案加人审。留出集里的策略拒绝案例正确答案是“按设计拒绝”,先判定再动手。',
      'Policies, VLANs, fortilink and LACP are the shared forwarding plane — one bad change hits every subnet. No auto fix can ever be “absolutely safe” here; and the held-out deny case’s correct verdict was “deny by design”.',
    ],
    protectedNote: [
      '任何策略变更前置校验:不得影响 UDP 41641 出网与 DERP(443)可达——否则远程开发通道即断。',
      'Every policy change is prechecked: UDP 41641 egress and DERP (443) reachability must survive — or the remote-work path dies.',
    ],
    playbook: [
      { risk: 'readonly', what: ['deny 端口分布:137/138 NetBIOS 噪声属策略预期,不是故障', 'Deny-port profile: 137/138 NetBIOS noise is policy working as designed'], command: 'skill: check_policy_deny_profile' },
      { risk: 'readonly', what: ['与流量基线对比,区分“预期拒绝”与“误伤业务”', 'Compare against baseline: intended deny vs. collateral damage'], command: 'skill: check_traffic_baseline' },
      { risk: 'readonly', what: ['具体五元组在策略表的命中路径', 'Policy lookup for the exact tuple'], command: 'diagnose firewall iprope lookup <src> <sport> <dst> <dport> <proto>' },
      { risk: 'gated', what: ['策略/VLAN/fortilink/LACP 变更:预案草案 + 人工执行 + 变更后基线回读', 'Any forwarding-plane change: drafted plan, human hands, baseline read-back after'], command: 'config firewall policy  # 人工执行' },
    ],
  },
  {
    id: 'fam-posture-maintenance',
    prio: 6,
    severity: 'medium',
    automation: 'guarded',
    title: ['安全姿态维护', 'SECURITY POSTURE MAINTENANCE'],
    faultClasses: [],
    confirm: ['FortiGuard 版本落后天数 · TLS 到期', 'FORTIGUARD LAG · TLS EXPIRY'],
    rationale: [
      '签名更新通常无扰,但坏签名误杀合法流量有真实先例,所以压在维护窗内并盯 deny 率;换证书走预校验加热加载,零中断。',
      'Signature updates are usually quiet, but a bad signature false-blocking real traffic has happened in the wild — so: maintenance window plus deny-rate watch. Cert swaps are pre-validated and hot-reloaded.',
    ],
    playbook: [
      { risk: 'readonly', what: ['FortiGuard 定义落后天数与评级', 'FortiGuard definition lag and rating'], command: 'skill: check_security_posture' },
      { risk: 'gated', what: ['维护窗(非工作时段)触发签名更新,其后 30 分钟盯 deny 率突变,异常即回滚', 'Trigger the update off-hours, watch deny-rate for 30 min, roll back on anomaly'], command: 'execute update-now  # 维护窗内' },
      { risk: 'gated', what: ['弱 TLS:预校验通过才热换证书,reload 零中断', 'Weak TLS: hot-swap the cert only after config validation; reload is zero-downtime'], command: 'nginx -t && nginx -s reload' },
    ],
  },
  {
    id: 'fam-exposure-reduction',
    prio: 7,
    severity: 'medium',
    automation: 'manual',
    title: ['暴露面收敛', 'EXPOSURE REDUCTION'],
    faultClasses: [],
    confirm: ['设备端口探测画像 + 只读侦察', 'DEVICE PORT PROBE + RECON'],
    rationale: [
      '补丁重启、隔离设备、关闭端口——每一项定义上就是移除某个访问,不存在不影响使用的版本;摄像头隔离即断监控业务。全部人审。',
      'Patching, quarantine and port closure each remove access by definition — there is no impact-free version. Quarantining a camera kills the surveillance it provides. All human-approved.',
    ],
    playbook: [
      { risk: 'readonly', what: ['37777 等设备端口的探测行为画像(大华生态)', 'Behavior profile of device ports like 37777 (Dahua ecosystem)'], command: 'skill: check_device_port_probe' },
      { risk: 'readonly', what: ['复核暴露服务与版本指纹', 'Re-verify exposed services and version fingerprints'], command: 'nmap -sV -p- --open <target_ip>' },
      { risk: 'gated', what: ['补丁/重启、隔离、关端口:逐台评估业务影响后人工执行', 'Patch/restart, quarantine, port closure: per-device impact review, human hands'], command: 'config firewall policy (quarantine)  # 人工执行' },
    ],
  },
  {
    id: 'fam-benign-closure',
    prio: 8,
    severity: 'low',
    automation: 'auto',
    title: ['良性确认关单', 'BENIGN CONFIRMATION & CLOSURE'],
    faultClasses: ['session_tuple_clash'],
    confirm: ['读数在基线内 · 无故障证据', 'READINGS IN BASELINE · NO FAULT'],
    rationale: [
      '留出集 6 例中 3 例的正确答案是“没有故障”:会话冲突在基线内、DHCP 健康、姿态最新。带证据链规范关单本身就是处置——不关单,误报堆积会淹掉真告警。',
      'Three of the six held-out cases resolve to “nothing is broken”. Closing with an evidence chain is itself the fix — unclosed false positives drown the real ones.',
    ],
    playbook: [
      { risk: 'readonly', what: ['会话元组冲突计数是否在历史基线内', 'Session tuple clash count vs. historical baseline'], command: 'skill: check_event_log' },
      { risk: 'readonly', what: ['DHCP 与安全姿态健康读数', 'DHCP and posture health readings'], command: 'skill: check_dhcp_service · check_security_posture' },
      { risk: 'auto', what: ['带证据链关单:引用全部只读读数,写入处置轨迹', 'Close with the evidence chain: cite every readonly reading into the disposition trace'], command: 'POST /api/rca/incidents/<id>/disposition {"status":"resolved"}' },
    ],
  },
]
