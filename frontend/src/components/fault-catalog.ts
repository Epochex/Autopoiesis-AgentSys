/* The prioritized fault-family catalog behind the verdict ledger.
 *
 * This is the whole fault space of this network in one list: every fault_class
 * the environment detectors emit, every recon exposure kind, and the archived
 * incidents, folded into nine families and ranked by how badly each one hurts.
 *
 * Each family states who is allowed to fix it:
 *   auto     the fix only touches something that is already broken and already
 *            serving nobody, on one machine — the system may run it and keep
 *            following up on its own
 *   guarded  the system may run it, but only inside stated conditions:
 *            allowlists, auto-expiry, off-hours windows, watch-then-rollback
 *   manual   the fix touches the firewall, a shared subnet, or a third-party
 *            device that is currently working — the system drafts the plan, a
 *            person approves and runs it
 *
 * One rule outranks every playbook here: the Tailscale path is the only way in
 * from outside, so nothing — automatic or approved — may cut it. */

import type { Kind } from './scan-data'

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
  /** join keys against recon Finding.kind */
  reconKinds?: Kind[]
  /** how a hit in this family gets confirmed, in plain words */
  confirm: [string, string]
  /** why this family is fixed by the machine, by the machine under conditions,
   *  or only by a person */
  rationale: [string, string]
  /** a hard do-not-touch, rendered as a warning strip when present */
  protectedNote?: [string, string]
  playbook: CatalogStep[]
}

export const AUTOMATION_LABEL: Record<Automation, [string, string]> = {
  auto: ['系统自动修', 'AUTO'],
  guarded: ['有条件自动', 'GUARDED'],
  manual: ['人工处理', 'HUMAN'],
}

export const STEP_LABEL: Record<StepRisk, [string, string]> = {
  readonly: ['先查', 'CHECK'],
  auto: ['自动修', 'AUTO FIX'],
  gated: ['人工执行', 'BY HAND'],
}

export const TAILSCALE_GUARD: [string, string] = [
  '不许动的线路 · TAILSCALE — tailscale0 网卡、tailscaled 服务、100.64.0.0/10 网段、UDP 41641 和 DERP(443) 一律不封、不重启、不改路由。这是从外面进这个内网的唯一一条路,断了人就进不来了。所有封禁、防火墙策略和网卡操作在执行前都要先确认目标不在这条路上。',
  'DO NOT TOUCH · TAILSCALE — the tailscale0 interface, the tailscaled service, 100.64.0.0/10, UDP 41641 and DERP (443) are never blocked, restarted or rerouted. It is the only way into this network from outside; cut it and nobody can get back in. Every ban, policy and interface action checks the target against this path first.',
]

/** archived incident ids → the family that owns them */
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
    title: ['同一个 IP 被两台设备抢', 'TWO DEVICES ON ONE ADDRESS'],
    faultClasses: ['duplicate_ip_static', 'duplicate_ip_dhcp', 'rogue_dhcp'],
    confirm: ['ARP 表里两个 MAC 轮流出现', 'ARP TABLE SHOWS TWO MACS TAKING TURNS'],
    rationale: [
      '连上这个 IP 落到哪台机器,取决于谁赢了上一次 ARP —— 另一台提供的服务在这期间就是断的。但要修好,得去改那台占用设备(大华摄像机)自己的地址,它现在正在干活;改错了那台设备的业务直接断。这是本网最重的一次真实事故。',
      'Which machine you reach depends on who won the last ARP exchange, so whatever the loser serves is down meanwhile. Fixing it means re-addressing the squatting device (a Dahua camera) that is currently in service — get it wrong and that device goes dark. This is the worst incident this network has had.',
    ],
    playbook: [
      { risk: 'readonly', what: ['看本机 ARP:同一个 IP 出现两个 MAC 就是在抢', 'Local ARP: two MACs on one address means they are fighting over it'], command: 'ip neigh show 192.168.1.23' },
      { risk: 'readonly', what: ['再从防火墙侧看一遍,两边对得上才算数', 'Check the firewall side too — both views must agree'], command: 'diagnose sys arp list | grep 192.168.1.23' },
      { risk: 'readonly', what: ['连打 16 次探测,算出每个服务有多少比例的时间连不上', 'Probe 16 times and measure how often each service is unreachable'], command: 'POST /api/rca/environment/probe {"ip":"192.168.1.23","samples":16}' },
      { risk: 'gated', what: ['去占用设备自己的管理页面改成别的地址,或在防火墙上把这个 IP 固定给服务器的 MAC', 'Re-address the squatter from its own admin page, or pin the address to the server MAC on the firewall'], command: 'config system dhcp reserved-address' },
      { risk: 'gated', what: ['改完再打 16 次探测,连续 16 次都落到同一台才允许关单', 'Probe 16 more times after the change; only close if all 16 land on the same machine'], command: 'POST /api/rca/environment/probe → verdict=stable' },
    ],
  },
  {
    id: 'fam-exposure',
    prio: 1,
    severity: 'critical',
    automation: 'manual',
    title: ['数据库和管理口对内网敞开', 'DATABASES AND ADMIN PORTS LEFT OPEN'],
    faultClasses: [],
    reconKinds: ['exposed_db', 'exposed_admin', 'critical_cve', 'weak_tls'],
    confirm: ['端口扫描 + 版本探测,连上就说明敞着', 'PORT SCAN + VERSION PROBE — IF IT ANSWERS, IT IS OPEN'],
    rationale: [
      '内网里任何一台机器都能直接连上这些数据库和管理口,一台机器被拿下就等于全拿。但关端口这件事本身就是在断访问:谁在用这个库、哪个程序连着这个管理口,扫描看不出来,只能人去查。所以先查清依赖,再由人来关。',
      'Any machine on this network can reach these databases and admin ports directly, so one compromised host is enough. But closing a port is by definition cutting access, and a scan cannot tell you which app depends on it — a person has to check first, then close it.',
    ],
    playbook: [
      { risk: 'readonly', what: ['确认端口真的开着(只连一下,不登录)', 'Confirm the port really answers — connect only, no login'], command: 'nc -zv 192.168.1.46 23' },
      { risk: 'readonly', what: ['看服务和版本,判断是不是明文协议、有没有已知漏洞', 'Read the service and version: plaintext protocol? known CVE?'], command: 'nmap -Pn -sV -p 23 192.168.1.46' },
      { risk: 'readonly', what: ['查最近 7 天谁在连这个端口 —— 有人在用就不能直接关', 'Who connected to this port in the last 7 days — if anyone did, it cannot just be closed'], command: 'clickhouse: SELECT DISTINCT src FROM flows WHERE dst_port=3306 AND ts > now()-7*86400' },
      { risk: 'gated', what: ['管理口换成加密协议:关掉 telnet/http,只留 ssh/https', 'Switch admin access to encrypted only: drop telnet/http, keep ssh/https'], command: 'config system interface → unset allowaccess telnet http → set allowaccess ssh https ping' },
      { risk: 'gated', what: ['数据库只放行确认在用的来源,其余拒绝;先在一台上试,确认业务没断再推其余', 'Allow only the sources you confirmed are in use; try one host first and check nothing broke before the rest'], command: 'config firewall policy  # 人工执行' },
    ],
  },
  {
    id: 'fam-host-config-drift',
    prio: 2,
    severity: 'high',
    automation: 'auto',
    title: ['主机重启后网络配置被改掉', 'HOST NETWORK CONFIG REWRITTEN ON REBOOT'],
    faultClasses: ['host_config_drift', 'gateway_reachability'],
    confirm: ['网卡没载波 + 配置和基线对不上', 'NIC HAS NO CARRIER + CONFIG NO LONGER MATCHES BASELINE'],
    rationale: [
      '网卡已经断了、配置已经被改了,这时候动它不可能更坏 —— 而且只动这一台机器,别人不受影响,所以让系统自己修。前提是必须先确认它是真断了:7/18 那次 r230 从外面看像死机,其实是网线那一路掉了、机器还活着,照着"已死"去处理就会误杀正在跑的服务。',
      'The NIC is already down and the config is already wrong, so touching it cannot make things worse — and it only affects this one machine. The precondition matters: on 7/18 r230 looked dead from outside but was alive with a downed link, and acting on "dead" would have killed running services.',
    ],
    protectedNote: [
      '只动确认没载波的物理网卡;tailscale0 和 tailscaled 一律不碰。',
      'Only physical NICs confirmed to have no carrier; tailscale0 and tailscaled are never touched.',
    ],
    playbook: [
      { risk: 'readonly', what: ['网卡必须已经是 DOWN/NO-CARRIER,才允许自动动它', 'The NIC must already read DOWN/NO-CARRIER before anything automatic runs'], command: 'ip -br link show eno1' },
      { risk: 'readonly', what: ['看是不是 cloud-init 在重启时把网络配置覆盖了', 'Check whether cloud-init overwrote the network config on boot'], command: 'cloud-init status --long' },
      { risk: 'readonly', what: ['当前配置和基线逐行比,看改了哪几行', 'Diff the live config against the baseline line by line'], command: 'diff <(netplan get) /data/baseline/netplan-r450.yaml' },
      { risk: 'auto', what: ['把已经断的网卡关了再开 —— 它已经是断的,再断一次没有代价', 'Down and up the already-dead NIC — it is already at zero'], command: 'ip link set eno1 down && ip link set eno1 up' },
      { risk: 'auto', what: ['写一个文件让 cloud-init 下次别再改网络,不立即生效,重启才生效', 'Write the file that stops cloud-init touching the network next boot; nothing changes right now'], command: 'printf "network: {config: disabled}\\n" > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg' },
      { risk: 'gated', what: ['在活着的机器上 netplan apply 要人来做:配置写错会当场断掉所有连接', 'netplan apply on a live host is done by hand: a wrong config drops every connection at once'], command: 'netplan apply  # 人工执行' },
    ],
  },
  {
    id: 'fam-mgmt-bruteforce',
    prio: 3,
    severity: 'high',
    automation: 'guarded',
    title: ['管理员账号在被反复试密码', 'SOMEONE IS GUESSING THE ADMIN PASSWORD'],
    faultClasses: ['mgmt_bruteforce'],
    confirm: ['防火墙日志里连续的登录失败', 'REPEATED LOGIN FAILURES IN THE FIREWALL LOG'],
    rationale: [
      '封一个 IP 是可以撤回的(24 小时自动解封),所以可以自动做;但封错人就等于把自己的管理通道也堵了,所以只在明确的名单条件下自动:台账里的设备、Tailscale 网段、以及你当前这条 SSH 的来源,永远不进封禁名单。',
      'A ban is reversible (24h auto-expiry), so the machine may do it — but banning the wrong source locks you out of your own network, so it only runs under explicit conditions.',
    ],
    protectedNote: [
      '永远不封 100.64.0.0/10、台账内的设备、以及当前 SSH 会话的来源 IP;收紧 trusthost 一律人工做(写错会把自己锁在门外)。',
      'Never ban 100.64.0.0/10, inventoried devices, or the source of the current SSH session; trusthost tightening is always done by hand (a typo locks you out).',
    ],
    playbook: [
      { risk: 'readonly', what: ['失败了多少次、从哪些 IP 来、集中在什么时候', 'How many failures, from which addresses, concentrated when'], command: 'skill: check_admin_auth_failures' },
      { risk: 'readonly', what: ['哪个账号被锁了', 'Which account got locked'], command: 'skill: check_admin_lockout' },
      { risk: 'readonly', what: ['来源 IP 对一遍台账和 Tailscale 网段 —— 只要命中就停下交给人', 'Check the source against the inventory and the Tailscale range — any match stops the automation'], command: 'grep -F <src_ip> /data/inventory/assets.tsv' },
      { risk: 'auto', what: ['确认是外面打进来的之后,把被锁的账号解开 —— 解锁只会恢复访问,不会断任何东西', 'Once the source is confirmed external, unlock the account — unlocking only restores access'], command: 'config system admin (unlock)' },
      { risk: 'gated', what: ['封掉攻击来源,24 小时自动解封,名单里的地址强制排除', 'Ban the source with a 24h expiry; allowlisted addresses are excluded'], command: 'diagnose user banned-ip add src4 <src_ip> 86400' },
    ],
  },
  {
    id: 'fam-perception-selfheal',
    prio: 4,
    severity: 'high',
    automation: 'auto',
    title: ['监控自己断了', 'THE MONITORING ITSELF STOPPED'],
    faultClasses: [],
    confirm: ['日志不再进来 / 服务是 failed / 磁盘满了', 'NO NEW LOGS / SERVICE FAILED / DISK FULL'],
    rationale: [
      '采集停了,上面所有判断就都是在瞎猜,所以这一族排得靠前。重启一个已经挂掉的服务本身不会更坏,清理也只删已经打包归档、而且确认送走了的旧日志。但重启必须有次数上限:服务如果是因为下游依赖不通才挂的,无限重启会把一次降级放大成一场故障,所以 30 分钟内最多两次,第三次不再重启、直接交给人。',
      'When collection stops, every verdict above is guesswork, which is why this ranks high. Restarting an already-failed service does not make it worse in itself, and cleanup only removes rotated logs already shipped. But restarts must be bounded: if the service died because a dependency is down, looping restarts turn a degradation into an outage — so at most two in thirty minutes, then it stops and escalates.',
    ],
    playbook: [
      { risk: 'readonly', what: ['列出挂掉的服务 —— 只有状态是 failed 的才允许自动重启', 'List failed units — only units already failed qualify for an automatic restart'], command: 'systemctl --failed --no-legend' },
      { risk: 'readonly', what: ['看日志还在不在往里进', 'Check whether new events are still arriving'], command: 'curl -s localhost:8026/api/healthz | jq .runtime' },
      { risk: 'readonly', what: ['看它是不是因为依赖不通才挂的 —— 是的话重启没用,别重启', 'Did it die because a dependency is down — if so a restart fixes nothing, do not restart'], command: 'journalctl -u <failed-unit> -n 50 --no-pager' },
      { risk: 'readonly', what: ['最近 30 分钟已经自动重启过几次,到 2 次就停手', 'How many automatic restarts already happened in the last 30 minutes — stop at two'], command: 'systemctl show <failed-unit> -p NRestarts' },
      { risk: 'auto', what: ['重启挂掉的采集服务,然后回头确认日志确实又进来了;没进来就不再重试', 'Restart the failed collector, then confirm events are flowing again; if they are not, do not retry'], command: 'systemctl restart <failed-unit> && sleep 10 && curl -s localhost:8026/api/healthz' },
      { risk: 'auto', what: ['删掉 14 天前、已经打包并送走的旧日志', 'Delete rotated logs older than 14 days that were already shipped'], command: 'find /data/logs -name "*.gz" -mtime +14 -delete' },
    ],
  },
  {
    id: 'fam-dhcp-lifecycle',
    prio: 5,
    severity: 'medium',
    automation: 'guarded',
    title: ['DHCP 地址不够用 / 租约乱跳', 'DHCP RUNNING OUT / LEASES CHURNING'],
    faultClasses: ['lease_churn', 'pool_pressure', 'address_unmanaged', 'host_multi_address', 'unmanaged_identity'],
    confirm: ['地址池占用率 + 租约重建频率', 'POOL OCCUPANCY + HOW OFTEN LEASES ARE REBUILT'],
    rationale: [
      '往地址池里加一段是只加不改,现有设备的租约不受影响,所以可以自动;但历史流量证明不了"这段地址没人用"—— 一台关着机的静态设备是不会出现在任何记录里的,所以必须先查够 7 天再加。改租约时长、重划网段会影响所有人,人来做。',
      'Extending a pool only adds, so existing leases are untouched — but history cannot prove a range is free, because a powered-off static device leaves no trace. Hence the seven-day check first. Lease-time and scope changes affect everyone and are done by hand.',
    ],
    playbook: [
      { risk: 'readonly', what: ['地址池用了多少、租约多久重建一次', 'How full the pool is and how often leases are rebuilt'], command: 'skill: check_dhcp_service' },
      { risk: 'readonly', what: ['准备加的那段地址,过去 7 天必须一条流量都没有', 'The range you plan to add must show zero traffic over the last 7 days'], command: 'clickhouse: SELECT count() FROM flows WHERE ip IN <candidate_range> AND ts > now()-7*86400' },
      { risk: 'gated', what: ['只往后追加确认没人用的一段,不动现有的;加完确认新设备能正常拿到地址', 'Append the verified-empty range only, leave the existing one alone, then confirm new devices still get addresses'], command: 'config system dhcp server → edit ip-range (append)' },
      { risk: 'gated', what: ['改租约时长、重划网段 —— 地址不够时加长租约只会更糟,人来判断', 'Lease-time and scope changes — longer leases make exhaustion worse, a person decides'], command: 'config system dhcp server → set lease-time  # 人工执行' },
    ],
  },
  {
    id: 'fam-policy-reachability',
    prio: 6,
    severity: 'medium',
    automation: 'manual',
    title: ['防火墙策略挡住了正常访问', 'FIREWALL POLICY IS BLOCKING SOMETHING REAL'],
    faultClasses: ['l2_loop_macflap', 'resolver_failure'],
    confirm: ['哪些流量被拒了 + 和平时比', 'WHAT GOT DENIED, COMPARED WITH A NORMAL DAY'],
    rationale: [
      '防火墙策略、VLAN、fortilink、LACP 由多个网段共享，改错会扩大影响范围，而且当前执行端没有可靠撤销能力，所以这一族由人审批。137/138 等广播噪声需要先与业务流量基线比较，确认是正常拒绝还是误伤。',
      'Firewall policies, VLANs, FortiLink, and LACP are shared across subnets. A bad change widens the blast radius, and the current executor has no reliable rollback, so a person approves it. Compare broadcast noise such as ports 137 and 138 with the traffic baseline before deciding whether a denial is expected or harmful.',
    ],
    protectedNote: [
      '改任何策略前先确认:UDP 41641 出网和 DERP(443) 必须还通 —— 否则远程就进不来了。',
      'Before any policy change, confirm UDP 41641 egress and DERP (443) still work — otherwise the remote path is gone.',
    ],
    playbook: [
      { risk: 'readonly', what: ['被拒的都是哪些端口 —— 137/138 这种是正常噪声,不是故障', 'Which ports are being denied — 137/138 noise is normal, not a fault'], command: 'skill: check_policy_deny_profile' },
      { risk: 'readonly', what: ['和平时的流量比一比,分清"本来就该拒"和"误伤了业务"', 'Compare with a normal day: denied on purpose, or collateral damage'], command: 'skill: check_traffic_baseline' },
      { risk: 'readonly', what: ['查这条具体的连接在策略表里命中了哪一条', 'Look up which policy line this exact connection hits'], command: 'diagnose firewall iprope lookup <src> <sport> <dst> <dport> <proto>' },
      { risk: 'gated', what: ['改策略/VLAN/链路:系统只出方案,人执行,改完再和流量基线对一遍', 'Policy, VLAN and link changes: the system drafts, a person runs it, then compare against the baseline again'], command: 'config firewall policy  # 人工执行' },
    ],
  },
  {
    id: 'fam-posture-maintenance',
    prio: 7,
    severity: 'medium',
    automation: 'guarded',
    title: ['安全更新过期、证书快到期', 'SECURITY UPDATES STALE, CERTS EXPIRING'],
    faultClasses: [],
    confirm: ['特征库落后多少天 · 证书剩余天数', 'HOW MANY DAYS BEHIND · DAYS LEFT ON THE CERT'],
    rationale: [
      '更新特征库一般没影响,但确实出现过坏特征把正常流量当成攻击拦下来,所以压到非工作时间做,做完盯半小时的拦截量,不对就退回去。换证书先校验配置再热加载,不会断连接。',
      'Signature updates are usually quiet, but a bad signature blocking real traffic has happened — so it runs off-hours with a 30-minute watch and a rollback. Cert swaps are validated first and hot-reloaded, so no connection drops.',
    ],
    playbook: [
      { risk: 'readonly', what: ['特征库落后了多少天', 'How far behind the definitions are'], command: 'skill: check_security_posture' },
      { risk: 'gated', what: ['非工作时间更新,更新后盯 30 分钟拦截量,异常就退回去', 'Update off-hours, watch the deny rate for 30 minutes, roll back if it jumps'], command: 'execute update-now  # 维护窗内' },
      { risk: 'gated', what: ['换证书:先校验配置,通过了再热加载,连接不会断', 'Swap the cert: validate the config first, then hot reload — no dropped connections'], command: 'nginx -t && nginx -s reload' },
    ],
  },
  {
    id: 'fam-benign-closure',
    prio: 8,
    severity: 'low',
    automation: 'auto',
    title: ['查完确认没问题,关单', 'CHECKED, NOTHING WRONG, CLOSED'],
    faultClasses: ['session_tuple_clash'],
    reconKinds: ['info'],
    confirm: ['读数都在正常范围内', 'ALL READINGS WITHIN THE NORMAL RANGE'],
    rationale: [
      '当会话冲突处于历史正常范围、DHCP 健康且安全状态最新时，系统应附上证据并关闭误报。持续积压误报会挤占真实事件的调查容量。',
      'When session conflicts remain within their historical range, DHCP is healthy, and security posture is current, the system should attach the evidence and close the false positive. Accumulated false positives consume investigation capacity needed by real incidents.',
    ],
    playbook: [
      { risk: 'readonly', what: ['会话冲突次数是不是在历史正常范围内', 'Is the session-clash count within its historical range'], command: 'skill: check_event_log' },
      { risk: 'readonly', what: ['DHCP 和安全状态的读数', 'DHCP and posture readings'], command: 'skill: check_dhcp_service · check_security_posture' },
      { risk: 'auto', what: ['把用到的所有读数附在单子上,然后关单', 'Attach every reading used, then close the case'], command: 'POST /api/rca/incidents/<id>/disposition {"status":"resolved"}' },
    ],
  },
]
