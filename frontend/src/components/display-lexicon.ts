/* Deterministic English for the product's own Chinese display strings.
 *
 * Two mechanisms already translated prose on this console, and neither covered
 * the English UI on its own:
 *
 *   - `useDisplayTranslation` sends prose to a model. It is the right tool for
 *     text nobody wrote by hand — an operator's note, a stored case summary —
 *     but it needs a configured key, costs money per deploy, and when the call
 *     fails the page falls back to the Chinese source. An English UI that turns
 *     Chinese the moment a key expires is not an English UI.
 *
 *   - Per-component `zh ? … : …` ternaries cover the chrome each component
 *     owns, and nothing that arrives in a payload.
 *
 * Everything the product itself emits — capability names, fault titles, change
 * actions, the sentences the detectors and the case writer compose — is closed
 * vocabulary. It is written out here instead: no key, no latency, no per-deploy
 * spend, and English that was reviewed once rather than regenerated per call.
 * The model stays as the fallback for genuinely novel prose.
 *
 * Resolution runs widest-first: an exact string, then a whole-string pattern,
 * then ` · `-joined segments translated one by one, then known fragments
 * replaced inside mixed-language text. A string that still carries Chinese
 * after all four is reported as unresolved so the caller can fall back.
 */
import type { Lang } from '../i18n'

const HAN = /[㐀-鿿]/

/* Fixed vocabulary, one exact string in, one out. */
const EXACT: Record<string, string> = {
  // Observation coverage — what this network can and cannot currently see.
  '受管设备与活跃地址': 'Managed devices and active addresses',
  '跨网段拒绝记录': 'Cross-segment denies',
  '跨网段成功连接': 'Accepted cross-segment connections',
  '同网段横向通信': 'Same-segment lateral traffic',
  '用户身份行为': 'User identity activity',
  '终端进程与文件攻击链': 'Endpoint process and file activity',
  'MAC 对应交换机端口': 'MAC-to-switch-port mapping',
  '外部威胁来源匹配': 'External threat-source matching',
  '在 FortiGate 开启 accept 会话日志': 'Enable accept session logging on the FortiGate',
  '交换机流量镜像、NetFlow 或端点网络探针': 'Switch port mirroring, NetFlow or an endpoint network probe',
  '身份系统、VPN 或终端登录数据': 'Identity system, VPN or endpoint sign-in data',
  '端点检测或主机审计数据': 'Endpoint detection or host audit data',
  'FortiSwitch MAC 地址表': 'The FortiSwitch MAC address table',
  '已授权的本地情报订阅文件': 'An authorised local threat-intelligence feed file',

  // Fault families, as the verdict ledger names them.
  '地址归属与租约冲突已由复核确认': 'address ownership conflicts with the lease, confirmed on review',
  '同一地址出现多个 DHCP 归属': 'one address claimed by several DHCP owners',
  '活跃地址缺少租约或资产归属': 'active address has no lease and no asset owner',
  '同一设备在短时间内持有多个地址': 'one device held several addresses in a short window',
  '设备租约持续重建': 'device lease is repeatedly rebuilt',
  '地址池剩余容量进入风险区间': 'address pool free capacity has entered the risk band',
  '同一会话标识出现冲突': 'conflicting session identifiers',
  '设备身份无法关联现有资产记录': 'device identity matches no existing asset record',
  '管理入口遭遇连续认证尝试': 'management interface saw repeated authentication attempts',
  '环境状态发生变化': 'Environment state changed',
  '未知对象': 'unknown subject',
  '存在尚未关闭的生产调查': 'an open production investigation',

  // Boundary traffic.
  '已阻断的跨区访问': 'Blocked cross-zone access',
  '已观测的跨区访问': 'Observed cross-zone access',
  '未映射私网地址': 'Unmapped private addresses',
  '未知服务': 'unknown service',
  '规则命中': 'rule match',

  // Case decisions.
  '不执行自动动作': 'No automatic action',
  '保留现场，不执行自动变更。': 'Preserve the scene; make no automatic change.',
  '自动调查预算已用完，案件携带现场证据升级':
    'Automatic investigation budget exhausted; case escalated with the evidence collected',
  '转交网络责任人核对缺失观察；补充结果写回原案件后可从当前检查点继续。':
    'Hand to the network owner to supply the missing observations; once the results are written back to the case, work resumes from this checkpoint.',
  '按记录中的冲突 MAC 查询交换机端口，确认合法设备后调整静态地址或 DHCP 保留；变更完成后重新采集 ARP、DHCP 与连续 L2 归属。':
    'Look up the switch ports for the conflicting MACs in the record, confirm which device is legitimate, then adjust the static address or the DHCP reservation; after the change, re-collect ARP, DHCP and continuous L2 ownership.',

  // Change actions, as offered in the change workbench.
  '固定 / 转移 DHCP 地址': 'Fix / transfer DHCP address',
  '删除 DHCP 地址保留': 'Remove DHCP reservation',
  '清理单个 DHCP 租约': 'Clear a single DHCP lease',
  '清理单条 ARP 缓存': 'Clear a single ARP entry',
  '隔离设备 / 禁用接入口': 'Isolate device / disable access port',
  '解除隔离 / 启用接入口': 'Release isolation / enable access port',
  '重新启停接入口': 'Bounce access port',
  '端口 PoE 断电重启': 'Power-cycle port PoE',
  '修改接入口 VLAN': 'Change access port VLAN',
  '创建 / 修改精确防火墙规则': 'Create / modify a specific firewall policy',
  '撤销指定防火墙规则': 'Remove a specific firewall policy',
  '临时封禁来源': 'Temporarily block a source',
  '调整管理员允许来源': 'Adjust administrator source allowlist',
  '修改终端静态地址': 'Change host static address',
  '修复主机地址持久化': 'Repair host address persistence',
  '重启失败服务': 'Restart failed services',
  '重新启停故障网卡': 'Bounce a failed interface',
  '地址分配': 'Address assignment',
  '交换机接入': 'Switch access',
  '访问控制': 'Access control',
  '管理入口': 'Management access',
  '主机网络': 'Host network',
  '主机服务': 'Host services',

  // Change scope, rollback and reason text.
  '专用测试服务状态与本机网关连接，持续真实回读':
    'Dedicated test service state and local gateway reachability, read back continuously from the device',
  '瞬时动作无法逆转，异常升级人工':
    'The action is instantaneous and cannot be reversed; failures escalate to an operator',
  'Netplan Try 240 秒自动恢复；业务检查通过后才确认配置':
    'Netplan try reverts automatically after 240 seconds; the configuration is committed only after business checks pass',
  '连续两次检测确认专用服务失败，自动恢复并持续检查服务和网关连接。':
    'Two consecutive checks confirmed the dedicated service had failed; recovery ran automatically, and service and gateway reachability stay under observation.',
  'DHCP 保留配置；客户端重新获址及冲突消失需另行观察':
    'The DHCP reservation itself; the client re-acquiring the address and the conflict clearing are observed separately',
  '依据当前观测将地址归属确认给所选设备':
    'Assign the address to the selected device on the current observations',
  '主机配置、服务状态与业务连通性；重启持久性需单独重启验收':
    'Host configuration, service state and business reachability; persistence across reboots needs a separate reboot check',
  '受控验收：恢复专用测试服务，持续检查服务状态和管理连接。':
    'Controlled acceptance run: restore the dedicated test service, then keep checking service state and management reachability.',
  '已核对本机身份、专用服务和失败到运行的差异':
    'Verified the local host identity, the dedicated service, and the failed-to-running difference',
  '预览验收完成，取消；未执行设备修改。':
    'Preview acceptance finished and cancelled; no device change was made.',
  '改完回读通过，观察期内没有回归':
    'Readback passed after the change and nothing regressed during the observation window',

  // Demonstration runs.
  '历史处置记录已减少本次检查步骤': 'Past remediation records cut the checks needed this time',
  '第二轮：调用已有记忆，观察检查与上下文变化':
    'Round 2: memory enabled, observing the change in checks and context',
  '第一轮：关闭记忆调用，记录实际检查':
    'Round 1: memory disabled, recording the checks actually run',
  '记忆提速链路未通过全部验收条件': 'The memory speed-up chain did not meet every acceptance condition',
  '记忆与长轨迹演示执行失败': 'The memory and long-trajectory demonstration failed to run',
  '服务已恢复并通过持续观察': 'Service restored and held through continuous observation',
  '一次性授权已匹配，执行重启并持续观察':
    'One-time authorization matched; restart executed and kept under observation',
  '变更前状态与 Diff 已保存，等待演示预览':
    'Before-state and diff saved, awaiting demonstration preview',
  '测试服务已故障，等待连续检测确认':
    'Test service has failed, awaiting confirmation by consecutive checks',

  // Deployment labels.
  '本机 Linux 主机': 'Local Linux host',

  // Data provenance.
  '生产事实流': 'Production fact stream',
  '留出集': 'held-out set',

  // Case decisions: what the investigation concluded, and what happens next.
  '受管物理网口失去载波': 'managed physical interface lost carrier',
  '主机缺少可用默认路由': 'host has no usable default route',
  '目标地址当前无法完成邻居解析': 'target address cannot currently be resolved by neighbour discovery',
  '受管服务进入失败状态': 'managed service entered the failed state',
  '可写文件系统达到容量阈值': 'writable filesystem reached its capacity threshold',
  '主机可用内存低于阈值': 'host free memory is below threshold',
  '调查服务健康检查失败': 'investigation service health check failed',
  'SSL VPN 认证入口': 'the SSL VPN authentication entry point',
  '防火墙管理入口': 'the firewall management entry point',
  '事件缺少受管对象，案件已携带源记录升级': 'Event names no managed object; case escalated with the source record',
  '事件已接管，当前记录无法圈定可调查的受管对象':
    'Event accepted; the current record does not identify a managed object to investigate',
  '认证攻击触发计数无法由归档事件窗口复现':
    'The authentication-attack trigger counts cannot be reproduced from the archived event window',
  '调查仍缺少能够区分候选原因的当前观察':
    'The investigation still lacks a current observation that separates the candidate causes',
  '多个故障条件同时成立': 'Several fault conditions hold at once',
  '预定义故障条件已排除，转入开放根因调查':
    'Predefined fault conditions ruled out; moving to open root-cause investigation',
  '动作完成且结果回读通过': 'Action completed and its result read back successfully',
  '动作未通过结果回读，案件已升级': 'The action failed its result readback; case escalated',
  '源记录缺少能够把告警绑定到受管资产和故障域的结构化字段。':
    'The source record has no structured field binding the alert to a managed asset and a fault domain.',
  '复发记忆把已验证探针排到首位，当前读数再次确认同一根因。':
    'Memory of the recurrence ran the verified probe first, and the current reading confirms the same root cause.',
  '当前只读观察完成候选原因排查，并确认该故障条件。':
    'Read-only observation eliminated the other candidates and confirmed this fault condition.',
  '当前观察确认了多个独立故障条件，需要按影响面分别处置。':
    'The observations confirm several independent fault conditions, each needing its own remediation by blast radius.',
  '当前只读结果排除了已定义的链路、路由、服务、磁盘和内存条件，告警原因仍未解释。':
    'Read-only results rule out the defined link, route, service, disk and memory conditions; the alert is still unexplained.',
  '允许执行已通过前置检查的单一动作，动作完成后回读原系统。':
    'Permit one action that has passed its preflight checks, then read the system back once it completes.',
  '当前动作目录没有适用的安全操作，案件携带已确认根因转交责任人。':
    'The action catalogue holds no safe operation for this case; it goes to the owner with the confirmed root cause.',
  '由设备负责人核对被尝试账户和合法来源，启用多因素认证并限制 SSL VPN 来源范围；策略提交后重新查询失败认证、账户锁定与成功登录记录。':
    'The device owner checks the attempted accounts and the legitimate sources, enables multi-factor authentication and narrows the SSL VPN source range; once the policy is committed, failed authentications, account lockouts and successful sign-ins are queried again.',
  '由设备负责人确认合法管理来源，收紧管理账户 trusthost 或关闭公网管理入口；策略提交后重新查询失败登录、锁定与管理口放行记录。':
    'The device owner confirms the legitimate management sources, tightens the administrator trusthost or closes the public management entry point; once the policy is committed, failed sign-ins, lockouts and management-port accepts are queried again.',
  '转交数据采集责任人补齐资产身份映射；补充结果写回原案件后重新启动探针。':
    'Hand to the collection owner to complete the asset identity mapping; probes restart once the result is written back to the case.',
  '案件已携带触发记录和归档回查升级给数据采集责任人；修复分类或落库缺口后使用同一事件标识重放并重新判定。':
    'The case is escalated to the collection owner with both the trigger record and the archive lookup; once the classification or ingestion gap is fixed, it is replayed under the same event id and re-adjudicated.',
  '继续执行下一项只读探针；探针失败时保留案件并报告数据源故障。':
    'Run the next read-only probe; if a probe fails, keep the case open and report the data-source failure.',
  '转交网络责任人拆分动作并逐项回读，任一失败结果写回原案件。':
    'Hand to the network owner to split the actions and read each one back; any failure is written back to the case.',
  '由调查模型提出可反证的新根因，并自动执行两个独立信号的只读检查。':
    'The investigation model proposes a new falsifiable root cause, then runs read-only checks on two independent signals.',
  '关闭案件；同一故障复发时按复发预算重新开案。':
    'Close the case; a recurrence of the same fault reopens it against the recurrence budget.',
  '停止继续变更，保留现场并调查动作失败原因。':
    'Stop making changes, preserve the scene and investigate why the action failed.',
  '保持案件调查中，从源告警补齐资产身份后再启动探针。':
    'Keep the case under investigation; probes start once asset identity is filled in from the source alert.',
  '当前不执行探针或变更。': 'No probe and no change are run.',
  '当前不执行变更。': 'No change is made.',
  '根因确认前不执行变更。': 'No change is made until the root cause is confirmed.',
  '冻结自动封禁和账户变更，优先修复证据链。':
    'Freeze automatic blocks and account changes; repairing the evidence chain comes first.',
  '冻结组合自动动作，等待逐项处置': 'Combined automatic actions are frozen, pending item-by-item remediation',
  '现场观察': 'On-site observation',
  '触发源记录': 'Trigger source record',
  '实时检测聚合': 'Live detection aggregate',
  '哪一个新的可反证故障条件能够同时解释当前症状和两个独立观察？':
    'Which new falsifiable fault condition explains both the current symptoms and two independent observations?',

  // Sentinel cycle, case records, device profiles and blast radius.
  'down/up 该网口': 'Bounce the interface',
  '不执行操作': 'No action',
  '临时防火墙封禁': 'Temporary firewall block',
  '健康回读采样': 'Health readback sampled',
  '冷却中': 'Cooling down',
  '判定恢复': 'Resolved',
  '前置条件不通过，拒绝执行': 'Preconditions failed, declined',
  '前置校验': 'Preflight check',
  '前置校验拒绝': 'Preflight declined',
  '动作已回滚': 'Action reverted',
  '动作已提交': 'Action committed',
  '动作提交': 'Action commit',
  '只出告警': 'Alert only',
  '回滚待验证': 'Revert awaiting verification',
  '回读验证': 'Readback verification',
  '基线': 'Baseline',
  '处置后观察': 'Post-action observation',
  '处置后观察期': 'Post-action observation window',
  '处置安全冷却': 'Remediation safety cooldown',
  '复发次数已到上限': 'Recurrence limit reached',
  '复发预算已用尽，升级人工处置': 'Recurrence budget exhausted, escalated to an operator',
  '实测状态': 'Measured state',
  '巡检当前读数': 'Current detector reading',
  '巡检探测器': 'Detector sweep',
  '已执行并完成观察期': 'Acted, observation window closed',
  '当前巡检已恢复': 'Current check has cleared',
  '当前没有通过安全门的预注册动作；写操作保持不变，事件证据已记录并转人工。':
    'No pre-registered action passed the safety gate; write operations are left unchanged, the incident evidence is recorded and the case is handed to an operator.',
  '快速回退窗口': 'Fast rollback window',
  '执行失败': 'Execution failed',
  '探测网关连通性': 'Probe gateway reachability',
  '新自动处置已暂停：': 'New autonomous actions are paused: ',
  '新自动处置等待写入授权，授权状态尚未初始化。':
    'New autonomous actions await write authorization; the authorization state has not been initialized.',
  '本轮影响范围未记录': 'Impact scope not recorded for this cycle',
  '没有可自动执行的动作': 'No action can be run automatically',
  '注入': 'Injection',
  '演示保留地址': 'Demonstration reserved address',
  '目标恢复，升级解除': 'Target recovered, escalation lifted',
  '稳定性窗口': 'Stability window',
  '等待二次确认': 'Awaiting second confirmation',
  '自动化分级闸门': 'Graded automation gate',
  '自动巡检（无人值守）': 'Automatic sweep (unattended)',
  '自动执行条件未满足': 'Automatic execution conditions not met',
  '自动执行条件未满足，转人工处置': 'Automatic execution conditions not met, handed to an operator',
  '观察窗口发现回归': 'Regression found in the observation window',
  '观察窗口开启': 'Observation window opened',
  '观察窗口通过': 'Observation window passed',
  '重启该 systemd 单元': 'Restart the systemd unit',
  '防火墙写入安全门': 'Firewall write safety gate',

  // Sentinel cycle, case records, device profiles and blast radius.
  'ping 二进制不可用': 'ping binary unavailable',
  '主动 ICMP 回显探测 · 实时测量(仅在请求时执行)':
    'Active ICMP echo probe · live measurement (runs only on request)',
  '依据当前观测事实，影响范围待核实': 'Current observation facts; impact requires verification',
  '保留源记录用于趋势统计；只有目标变为单播资产或影响业务时重新开案。':
    'Keep the source record for trend statistics; reopen the case only if the destination becomes a unicast asset or business is affected.',
  '内存': 'memory',
  '内网组播或广播拒绝记录保留为观测，不启动事故调查':
    'Internal multicast or broadcast deny records kept as observation; no incident investigation opened',
  '受管网关': 'managed gateway',
  '同网段观测': 'Observed in same netblock',
  '地址冲突': 'address conflict',
  '地址归属': 'address ownership',
  '大量流量被策略拦截': 'heavily policy-denied',
  '客户端特征 · 以主动外发为主': 'client-like · mostly outbound',
  '工作站': 'workstation',
  '已核实业务影响': 'Verified business impact',
  '已核实入侵证据': 'Verified compromise evidence',
  '常访问:': 'visits:',
  '广播喧哗 · 发现/组播为主': 'broadcast-noisy · discovery traffic',
  '当前观测中没有该对象。': 'Object is absent from the current observation.',
  '当前观测中没有该设备或网段。': 'The device or segment is absent from the current observation.',
  '当前观测窗口中没有该公网来源。': 'The public source is absent from the current observation window.',
  '摄像头': 'camera',
  '无流量': 'no flows',
  '暴露': 'exposure',
  '服务': 'service',
  '服务器': 'server',
  '服务端特征 · 以被动连接为主': 'server-like · mostly inbound',
  '未知动作': 'unknown action',
  '未知来源': 'unknown source',
  '未知目标': 'unknown destination',
  '核心节点': 'core node',
  '模型推断：': 'Model inference: ',
  '模型返回了无效分析结果。': 'The model returned an invalid analysis result.',
  '汇聚节点': 'aggregation node',
  '移动端': 'mobile device',
  '端口': 'port',
  '纯内网通信': 'internal-only',
  '组流量': 'group traffic',
  '节点': 'node',
  '被动 syslog 拥塞/压力信号(流量/deny率/活跃),非网络延迟':
    'Passive syslog congestion/pressure signal (flows/deny rate/activity), not network latency',
  '观测到拒绝或阻断记录，入侵结果待核实': 'Deny or block records observed; intrusion outcome unverified',
  '观测到攻击尝试，入侵结果与业务影响待核实':
    'Attack attempts observed; intrusion outcome and business impact unverified',
  '设备': 'device',
  '调查这起网络事件的根因': 'Investigate the root cause of this network event',
  '边缘节点': 'edge node',
  '边缘采集节点': 'edge collector',
  '这个动作没有可计算的影响面，按需要人工评估处理。':
    'This action has no computable blast radius; handle it by manual assessment as needed.',
  '采样窗内无流量 · 仅 DHCP 可见': 'no flows in sample window · DHCP-only',
  '门禁对讲': 'door intercom',

  // Cost.
  '成本按本地费率表估算，权威数字以服务商账单为准；token 数来自 API 返回。':
    'Costs are estimated from the local rate table; the provider invoice is authoritative. Token counts come from the API response.',
}

/* Sentences composed at request time. Every capture is a measurement, an
 * address or an identifier, and each one is carried through unchanged. */
export const PATTERNS: [RegExp, string][] = [
  [/^(\d+) 小时活动量达到过去 (\d+) 天日均的 ([\d.]+) 倍$/,
    '$1-hour activity is $3× the $2-day daily average'],
  [/^访问 (\d+) 个目标，高于过去 (\d+) 天日均$/,
    'Reached $1 $1{destination|destinations}, above the $2-day daily average'],
  [/^拒绝占比 (\d+)%，高于历史日均$/,
    'Denied share $1%, above the historical daily average'],
  [/^过去 (\d+) 天缺少活动基线，24 小时内新增 (\d+) 条被拒绝记录$/,
    'No activity baseline over the past $1 days; $2 denied $2{record|records} in the last 24 hours'],
  [/^已确认 (\S+) 存在地址归属冲突$/,
    'Confirmed address-ownership conflict on $1'],
  [/^地址已稳定落到无租约的 (\S+) 手上 —— 最近 (\d+) 次采集里,租约持有者 (\S+) 完全失去了这个地址$/,
    'The address has settled on $1, which holds no lease — across the last $2 samples the lease holder $3 lost it entirely'],
  [/^复核窗口内在 (\d+) 个 MAC 之间换手 (\d+) 次$/,
    '$2 $2{handover|handovers} between $1 $1{MAC|MACs} during the review window'],
  [/^(\S+) 在 (\d+) 个 MAC 间发生 (\d+) 次归属切换，当前 L2 采集与 DHCP 绑定不一致。$/,
    '$1 changed owner $3 $3{time|times} across $2 $2{MAC|MACs}; the current L2 samples disagree with the DHCP binding.'],
  [/^候选条件 (\S+) 缺少哪项决定性现场结果？$/,
    'Which decisive on-site result is missing for candidate condition $1?'],
  [/^已完成 (\d+) 项观察，仍有 (\d+) 个候选条件缺少决定性结果。结束原因：(.+)$/,
    '$1 $1{observation|observations} completed; $2 candidate $2{condition|conditions} still $2{lacks|lack} a decisive result. Stop reason: $3'],
  [/^策略 (\S+) · (\S+)$/, 'Policy $1 · $2'],
  [/^设备记录 · (\S+)$/, 'Device record · $1'],
  [/^处置处于安全冷却，剩余 (\d+) 秒$/, 'Remediation is in safety cooldown, $1 $1{second|seconds} remaining'],
  [/^(\d+) 个所需数据源读取失败，案件保留当前 (\d+) 条观察并继续调查。$/,
    '$1 required $1{data source|data sources} could not be read; the case keeps its $2 current $2{observation|observations} and stays open.'],
  [/^案件已有 (\d+) 条当前观察，正在形成关闭、处置或升级决定。$/,
    'The case holds $1 current $1{observation|observations} and is forming a decision to close, remediate or escalate.'],
  [/^已取得 (\d+) 条当前观察，仍有 (\d+) 个候选原因等待区分。$/,
    '$1 current $1{observation|observations} collected; $2 candidate $2{cause|causes} still need separating.'],
  [/^已确认(.+)遭遇分布式登录攻击$/,
    'Confirmed a distributed sign-in attack against $1'],
  [/^事件窗口内记录 (\d+) 次失败登录，来自 (\d+) 个来源地址，触发 (\d+) 次锁定。$/,
    '$1 failed $1{sign-in|sign-ins} recorded in the event window, from $2 source $2{address|addresses}, triggering $3 $3{lockout|lockouts}.'],
  [/^已确认：(.+)$/,
    'Confirmed: $1'],
  [/^候选原因 (\S+) 在当前对象上是否成立？$/,
    'Does candidate cause $1 hold for this object?'],
  [/^还有 (\d+) 个候选原因未完成检查，案件保持调查中。$/,
    '$1 candidate $1{cause remains|causes remain} unchecked; the case stays under investigation.'],
  [/^实时检测记录 (\d+) 次认证失败、(\d+) 个来源；同一设备和时间窗口归档到 (\d+) 次失败、(\d+) 次锁定、(\d+) 个来源。$/,
    'Live detection recorded $1 authentication $1{failure|failures} from $2 $2{source|sources}; the archive for the same device and window holds $3 $3{failure|failures}, $4 $4{lockout|lockouts} and $5 $5{source|sources}.'],
  [/^(\d+) 次失败登录，(\d+) 个来源，事件标识 (\d+) 个$/,
    '$1 failed $1{sign-in|sign-ins}, $2 $2{source|sources}, $3 event $3{id|ids}'],
  [/^(.+) (\S+) 已执行，观察结果为 (\S+)。$/,
    '$1 $2 executed; observed outcome $3.'],
  [/^(.+?) · 已完成 (.+?) 次健康回读$/,
    '$1 · $2 health readbacks completed'],
  [/^(.+?) · 来源隔离决策$/,
    '$1 · Source-isolation decision'],
  [/^(.+?) 在最近 (.+?) 内对 SSH 失败登录 (.+?) 次。来源隔离策略已进入安全门判定。$/,
    '$1 in the last $2 made $3 failed SSH login attempts. The source-isolation policy has entered safety-gate adjudication.'],
  [/^(.+?) 处于 failed 状态，服务进程不可用。目标属于受控单元白名单，已进入重启前置校验。$/,
    '$1 is in the failed state and its service process is not running. The target is on the controlled-unit allowlist and has entered restart preflight checks.'],
  [/^(.+?) 已提交，动作回执已记录$/,
    '$1 committed, action receipt recorded'],
  [/^(.+?) 当前巡检已不再发现该故障条件。$/,
    'The current check no longer detects this fault condition on $1.'],
  [/^(.+?) 配了地址 (.+?) 但没有载波，说明它本来该承载流量、现在断了。$/,
    '$1 has address $2 configured but no carrier, which means it was meant to carry traffic and is now down.'],
  [/^候选动作：(.+?)（未执行）$/,
    'Candidate action: $1 (not executed)'],
  [/^同一目标暂不重复执行，剩余 (.+?) 秒$/,
    'Repeat action on the same target is held, $1 seconds remaining'],
  [/^处置处于安全冷却，剩余 (.+?) 秒$/,
    'Remediation is in safety cooldown, $1 seconds remaining'],
  [/^自动封禁条件未满足：来源 (.+?) 尚未完成归属确认与管理地址豁免校验；当前防火墙动作未同时提供封禁 TTL、提交后回读和超时自动回滚。策略保持现有配置，并将事件转入人工处置队列。$/,
    'Automatic block conditions are not met: the source $1 has not completed ownership confirmation or management-address exemption checks; the current firewall action does not provide a block TTL, a post-commit readback and a timed automatic rollback together. The policy keeps its existing configuration, and the incident is moved to the operator queue.'],
  [/^观察期 (.+?) 次采样 · (.+?)$/,
    'Observation window, $1 samples · $2'],
  [/^(.+?) · (.+?) 秒内 (.+?) 次认证失败 · (.+?) 个公网来源$/,
    '$1 · $2-second window, $3 authentication failures · $4 public sources'],
  [/^(.+?) 发往 (.+?) 的 (.+?) 已由本机策略拒绝。$/,
    'From $1 to $2, $3 was denied by the local policy.'],
  [/^(.+?) 在 Tailscale 路径上，是从外面进入这个内网的唯一通道。任何动作都不许碰它——断了就再也连不进来。$/,
    '$1 is on the Tailscale path and is the only way into this network from outside. No action may touch it: if it goes down, there is no way back in.'],
  [/^(.+?) 条(.+?)记录：(.+?) → (.+?)，(.+?)。$/,
    '$1 $2 events: $3 -> $4, $5.'],
  [/^(.+?) 次\/(.+?) 秒$/,
    '$1 in $2 seconds'],
  [/^(.+?) 正在承载流量（(.+?) 个地址、(.+?) 条路由），前置条件会拒绝这个动作。$/,
    '$1 is carrying traffic ($2 addresses, $3 routes); the preflight checks will refuse this action.'],
  [/^(.+?) 正在运行，前置条件会拒绝重启。$/,
    '$1 is running; the preflight checks will refuse a restart.'],
  [/^关联监听端口：(.+?)。$/,
    'Associated listening ports: $1.'],
  [/^关闭 (.+?):(.+?) 会移除这个端口的全部访问。当前观察到 (.+?) 条已建立连接；过去 (.+?) 天用过它的来源数需要查流量库，未查到之前按“有人在用”对待。$/,
    'Closing $1:$2 removes all access to this port. $3 established connections are observed right now; the number of sources that used it over the past $4 days has to be looked up in the traffic store, and until that lookup returns it is treated as in use.'],
  [/^只影响 (.+?) 一个口。它当前无载波、无 IP、无路由经过，所以断开重连不会中断任何现有连接。$/,
    'Affects only the one interface $1. It currently has no carrier, no IP and no route through it, so bouncing it will not interrupt any existing connection.'],
  [/^固定端口高频心跳 · :(.+?)$/,
    'fixed-port heartbeat · :$1'],
  [/^实时活跃 · (.+?) 会话 · (.+?) 个目标$/,
    'live-active · $1 sessions · $2 destinations'],
  [/^影响范围：(.+?) 单一服务进程；当前状态：(.+?)。$/,
    'Blast radius: the $1 service process alone; current state: $2.'],
  [/^指纹角色: (.+?)$/,
    'fingerprint role: $1'],
  [/^有外联 · (.+?) 个外部端点$/,
    'talks to WAN · $1 external peers'],
  [/^这台机器上没有 (.+?) 这个网口，动作不会执行。$/,
    'This host has no interface named $1; the action will not run.'],
]

/* Known Chinese inside otherwise-English text: stored records, receipts and
 * anything the backend truncated mid-sentence. Longest first, so a full
 * sentence wins over either of its halves. */
const FRAGMENTS: [RegExp, string][] = [
  [/… \[略去 ([\d,]+) 字符\] …/g, '… [$1 characters omitted] …'],
  [/处于 failed 状态，服务进程不可用。/g, 'is in the failed state and its process is not running. '],
  [/目标属于受控单元白名单，已进入重启前置校验。/g,
    'The target is on the controlled-unit allowlist and has entered restart preflight checks.'],
  [/处于 failed 状态，服务进程不/g, 'is in the failed state and its process is not'],
  [/：处置服务暂不可用，请稍后重试/g, ': the remediation service is unavailable; please retry shortly'],
  // Model-provider failures, as the provider layer now phrases them.
  [/模型服务账户余额不足，充值后点「重试深度研判」/g, 'The model provider account has insufficient balance; top up, then use "Retry deep assessment"'],
  [/模型服务 API key 无效或已过期，请更新密钥/g, 'The model provider API key is invalid or expired; update the key'],
  [/模型服务请求被限流，稍后重试/g, 'The model provider rate-limited the request; retry later'],
  [/配置的模型名在服务商已不存在，请更新模型名/g, 'The configured model name no longer exists at the provider; update it'],
  [/模型服务端暂时故障，稍后重试/g, 'The model provider is temporarily failing; retry later'],
  [/模型服务拒绝了本次请求/g, 'The model provider refused this request'],
  [/，schema /g, ', schema '],
  [/。原始响应：/g, '. Raw response: '],
]

/* `$2{handover|handovers}` picks the form that agrees with capture 2, so a
 * count of one does not read as "1 handovers".
 *
 * A capture is not always a number: `已确认：{root cause}` captures a fault
 * label that is itself in this lexicon, so each capture holding Chinese is
 * resolved again before it is placed. */
const fill = (target: string, match: RegExpMatchArray, depth: number): string => target
  .replace(/\$(\d)\{([^|}]*)\|([^}]*)\}/g, (_, index: string, one: string, many: string) =>
    Number(match[Number(index)]) === 1 ? one : many)
  .replace(/\$(\d)/g, (_, index: string) => {
    const capture = match[Number(index)] ?? ''
    if (!HAN.test(capture) || depth <= 0) return capture
    return EXACT[capture.trim()] ?? patternHit(capture.trim(), depth - 1) ?? capture
  })

const patternHit = (text: string, depth = 2): string | null => {
  for (const [pattern, target] of PATTERNS) {
    const match = text.match(pattern)
    if (match) return fill(target, match, depth)
  }
  return null
}

/** English for one display string, or null when Chinese would survive.
 *
 * Every path is checked on the way out rather than trusted: a pattern whose
 * capture did not resolve, or a segment list that only half matched, would
 * otherwise put Chinese on an English page under the guise of a translation. */
export function englishFor(value: string): string | null {
  const rendered = resolve(value)
  return rendered !== null && !HAN.test(rendered) ? rendered : null
}

function resolve(value: string): string | null {
  const text = value.trim()
  if (!text || !HAN.test(text)) return value
  const exact = EXACT[text]
  if (exact) return exact
  const pattern = patternHit(text)
  if (pattern) return pattern
  // Some payload notes ship both languages in one string, joined by ' / '. A
  // single line only: command output is full of paths and must not be halved.
  const halves = text.split(' / ')
  if (!text.includes('\n') && text.length < 400 && halves.length === 2
      && HAN.test(halves[0]) && halves[1].trim() && !HAN.test(halves[1])) return halves[1].trim()
  // `A · B · C` reads as separate labels; translate each and keep the joiner.
  if (text.includes(' · ')) {
    const parts = text.split(' · ')
    const rendered = parts.map(part => (HAN.test(part) ? EXACT[part.trim()] ?? patternHit(part.trim()) : part))
    if (rendered.every(part => part !== null && part !== undefined)) return rendered.join(' · ')
  }
  let mixed = text
  for (const [pattern, target] of FRAGMENTS) mixed = mixed.replace(pattern, target)
  if (HAN.test(mixed)) return null
  // Splicing a sentence in leaves a stray space before the next clause. Tidying
  // it is only safe on prose: command output aligns its columns with runs of
  // spaces, and collapsing those would corrupt the evidence being quoted.
  if (/ {2,}/.test(text)) return mixed
  return mixed.replace(/\s+([.,;])/g, '$1').replace(/ {2,}/g, ' ').trim()
}

/** The string as this language should show it; unchanged when no English exists. */
export function display(value: string, lang: Lang): string {
  if (lang !== 'en') return value
  return englishFor(value) ?? value
}
