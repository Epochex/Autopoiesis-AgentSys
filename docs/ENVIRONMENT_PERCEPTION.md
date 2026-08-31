# 环境感知与传感器覆盖

`domains/network_rca/incidents.py` 回放**已经被写下来的**事故。本文档描述的
`domains/network_rca/environment.py` 做相反的事:扫描原始网关语料,在任何人把事故
记下来之前指出环境哪里不对。

接口:`GET /api/rca/environment`(`?refresh=1` 强制重扫)。界面在 **`渗透`** 页::环境扫描、只读侦察与归档事故合并在同一页、同一条判定账本里,因为它们回答的是同一个问题:这张网现在到底哪里不对,以及怎么验证和修。

## 口径:实时 + 全历史

报告不再声称自己是"历史语料扫描"。它同时读:

| 源 | 性质 | 说明 |
| --- | --- | --- |
| `l2_identity_history` | 实时 | 采集账本,每 5 分钟一次 |
| `arp_snapshot` | 实时 | 最新一次点对点表 |
| `flow_store` | 实时管道 | ClickHouse `autopoiesis.facts`,6180 万条转发会话的全量历史 |
| `gateway_syslog` | 历史 | committed 语料,固定窗口 |

关键点:**"实时"是最新一行的属性,不是管道的属性**。一条昨天就停止入库的管道照样能应答查询、照样看起来是连着的;把它算作实时,等于把一张没人在看的网渲染成有人在看。所以每个源都带 `flowing`,停写的会在界面上标红并写明"INGESTION STALLED"。

## 实时复核:已经消失的判定会被清掉

每条判定在出报告前都要对着**还在写**的源复核一次,结果落在 `verification.state`:

- `confirmed` :: 实时源确认现在仍然成立。
- `resolved` :: 实时源确认已经消失。**移出 findings**,进入 `resolved[]` 并计数。
- `unverifiable` :: 没有能复核它的实时源,原样保留,并写明缺哪个源。

只有实时源正面证明消失了才会被清掉,**绝不因为"旧"而清掉**。这两件事必须分开:"DHCP 语料停在 6 月"不是"6 月那个故障已经修好"的证据,把它当证据就等于把一个没人监控的故障变成一份干净的报告::而这正是这个模块存在的原因。

## 为什么要有"覆盖"这一半

这个模块的起点是一次真实失败。192.168.1.23 被一台大华设备与 netops-node2 服务器
同时占用,现象是 DNS 超时、SSH 卡住、Tailscale 中继反复重连,持续数周,而平台**一条
都没报**。

原因不是规则写少了,是结构性的:平台唯一的身份来源是网关自己的 DHCP 服务器,而占用
方用的是**手工配置的静态地址,从头到尾没发过一个 DHCP 包**。在 DHCP 日志上再加多少
规则,都看不见一个第二持有者从不说话的地址。

所以报告有两半,两半同等重要:

- `findings[]` :: 现有数据源**能**证明的,附测量值与证据条数。
- `coverage[]` :: 现有数据源**不能**证明的,按故障类点名,并写明补哪个传感器能补上。

一个检测器跑不出来,和一个网络是干净的,在界面上长得一模一样。`coverage` 存在就是为了
把这两件事分开。

## 数据源

| 源 | 来自 | 打开方式 |
| --- | --- | --- |
| `dhcp_ack` | FortiOS `DHCP Ack log`,含 ip/mac/lease/hostname | 语料自带 |
| `dhcp_stats` | FortiOS `DHCP statistics`,每个作用域 total/used | 语料自带 |
| `session_clash` | FortiOS `session clash` | 语料自带 |
| `admin_auth` | FortiOS 管理员登录失败 / 登录禁用 | 语料自带 |
| `l3_flow` | 任何带 srcip/dstip 的事件 | 语料自带 |
| `l2_identity` | ARP / neighbour 表 | `AUTOPOIESIS_ARP_SNAPSHOT_PATH` |
| `l2_identity` 历史 | 逐次追加的采集账本(JSONL) | `AUTOPOIESIS_L2_LEDGER_PATH` |

`l2_identity` 解析三种运维当场就能产出的输出:FortiOS `get system arp`、FortiOS
`diagnose ip arp list`、Linux `ip neigh`。

## 一张 ARP 表不够

采集器接进来之后仍然漏报了一次,原因值得单独记:

```
15:29  ip neigh   192.168.1.23 -> d4:43:0e:1a:c5:88   (占用方)
15:59  采集器      192.168.1.23 -> 50:9a:4c:87:29:b3   (服务器,与租约一致)
16:02  采集器      192.168.1.23 -> d4:43:0e:1a:c5:88   (占用方)
```

两台设备争一个地址时,**不会同时出现在 ARP 表里**::它们轮流持有,每一张单独的快照
看上去都完全正常。15:59 那张快照里 .23 的 MAC 和 DHCP 租约完全对得上。

所以点对点比对(`detect_identity_contradiction`)只能抓到"抓拍时恰好是占用方在位"
的情况,漏报率取决于运气。真正可靠的是跨快照的归属漂移
(`detect_l2_ownership_drift`):同一地址在采集序列里换过持有者,每一次换手就是一段
回包发错设备的时间窗。

采集账本因此是独立的数据源,`autopoiesis-arp-collect.timer` 每 5 分钟落一次。

ARP 表是一个**时刻**。快照的采集时间随证据一起进入每条判定
(`measured.snapshot_captured_at`),超过 `ARP_SNAPSHOT_STALE_SECONDS`(900 秒)的快照
会在 `cannot_prove` 里明说"不能证明冲突现在还在",避免派人去追一个已经不存在的故障。

## 检测器

| 检测器 | 故障类 | 归并粒度 |
| --- | --- | --- |
| `dhcp_duplicate_ip` | 地址重复,双方都走 DHCP | 每地址 |
| `identity_contradiction` | 地址重复,一方静态(点对点) | 每地址 |
| `l2_ownership_drift` | 地址重复,一方静态(跨快照) | 每地址 |
| `unmanaged_address` | 作用域内有流量、无租约绑定 | 每地址 |
| `lease_churn` | 租约反复重建 | 每网段 |
| `host_multi_address` | 单主机多地址 | 每 MAC |
| `unmanaged_identity` | 随机 MAC,无法归属台账 | 每网段 |
| `pool_pressure` | 地址池接近耗尽 | 每作用域 |
| `session_clash` | 会话元组冲突 | 每地址 |
| `mgmt_bruteforce` | 管理面凭据攻击 | 整个管理面一条 |

归并粒度是刻意选的。凭据攻击会轮换末位八位组,按源地址一行会把一次战役拆成几百条
一模一样的记录,而它们的处置动作是同一个::这就是判断它们该不该分开的标准。同理,
一个网段上 121 台主机里 89 台在反复重建租约,那是**网段**的毛病,不是 89 个问题。

每条判定都带 `cannot_prove`(这条证据不能证明什么)和 `next_probe`(下一步取哪个证)。

## 仍然是盲区的

| 故障类 | 补什么 |
| --- | --- |
| `gateway_reachability` | 主机侧探针,上报网关 ping / ARP 解析 |
| `resolver_failure` | 主机侧探针,上报解析延迟与超时计数 |
| `host_config_drift` | 主机侧预检,读 netplan / cloud-init 持久配置 |
| `l2_loop_macflap` | 周期性 `diagnose switch mac-address list` |
| `rogue_dhcp` | 主机侧 DHCP 探针,记录应答的 server ID |

`host_config_drift` 就是 192.168.1.27 重启后 cloud-init 覆盖静态配置那一类。三个
`host_probe` 类可以由同一个主机侧代理一次补齐。

## 差分归属探测:ping 证明不了地址重复

两个占用方**都会**应答 ICMP,所以对争用地址 ping 永远是 0% 丢包。实测:

```
.23 owner=d4:43:0e:1a:c5:88  loss=0%  rtt≈0.2ms
.23 owner=50:9a:4c:87:29:b3  loss=0%  rtt≈0.2ms
```

可达性测试在这里完全无效。真正能区分两方的是**服务画像**::两台不同的机器开放的端口不同。`ownership_probe.py` 采样"当前 ARP 归属 + 一小组白名单端口",34 次采样零歧义:

| ARP 归属 | 22/ssh | 37777(大华 SDK) | 10250(kubelet) |
| --- | --- | --- | --- |
| `d4:43:0e:1a:c5:88` | open | **open** | **refused** |
| `50:9a:4c:87:29:b3` | open | **refused** | **open** |

连上 192.168.1.23 落到哪台机器,取决于谁赢了上一次 ARP 交换。只有一方提供的服务,在另一方持有地址期间就是不可达的::这个比例就是可用性代价。

只读:一次 TCP connect 随即关闭,加一次本地 neighbour 表读取。端口走白名单、采样数有上限,网关侧还限制目标必须落在本次扫描覆盖的网段内,否则这个端点就是一个带 REST 接口的端口扫描器。

## 只读验证步骤直接执行

判定展开后的验证/修复 playbook,分两类。**只读 recon 步骤**(`nc` 连通、`nmap -sV`
版本探测、`curl -I`、`openssl s_client` 读证书)带一个"执行"按钮,点了直接在网关侧跑,
返回真实结果::不用复制粘贴到终端。**修复/入侵步骤**(改配置、`mysql`/`hydra` 等)没有
执行入口,只展示不运行。

安全模型是这个端点的全部意义,因为"运行报告里印出来的命令"离一个命令注入服务只差一步:

- 命令文本**从不进 shell**。它被重新解析成一个类型化动作,只有白名单动词能被构造出来;
  带 shell 元字符、未知动词、或带副作用标志的一律解析失败而拒绝执行。
- 每个动作用解析出的参数**重建成固定 argv**,真正跑的字节是我们的,不是调用方的字符串。
- `nmap -sV` 不外调 nmap(不为读个 banner 去装包),而是进程内 connect + 读 banner::
  同样的只读操作,且 SYN/UDP/脚本扫描的代码路径根本不存在。
- 目标必须是**私网、且在本次扫描覆盖的网段内**;命令还必须**逐字匹配**服务端当前 playbook
  里的某个只读步骤,调用方无法执行平台没有主动展示的探测。

接口:`POST /api/rca/environment/run-step`,body `{"command": "<步骤原文>"}`。实现见
`domains/active_recon/probe_exec.py`。

## 传感器接入

被动本地采集(只读内核 neighbour 表,不发探测包,不登录任何设备):

```bash
python3 -m domains.network_rca.collect_arp \
  /data/autopoiesis-production/arp-snapshot.txt \
  --ledger /data/autopoiesis-production/l2-identity-history.jsonl
```

覆盖范围随之而来:neighbour 表只有本机实际通信过的邻居。要覆盖整个网段,网关自己的
ARP 表是更好的源::把 `get system arp` 的输出写进同一个文件,检测器读法完全相同。
