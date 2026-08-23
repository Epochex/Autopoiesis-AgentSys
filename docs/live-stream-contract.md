# 实时流接入 · 接口契约

写在实现之前,让并行开发的几方对齐。**这是契约,不是提案** —— 改动请先改本文件。

## 为什么现在做这个,而不是继续加记忆机制

一个 **1.1 GB、每秒 3 条、此刻仍在增长**的真实内网日志就在 R230 上,而系统读的
是一份 6 案例的留出集。所有"记忆有没有用"的争论在没有真实数据流之前都是空的:
留出集上每例固定 2 个探针、规则推理器本来就 100% 正确,**没有余量可省**,所以
cold/warm 对照测出 Δ=0 是必然的。

先把水通进来,再谈哪个组件有用。

## 数据源实况(已核实,不是设想)

```
FortiGate FG100E (192.168.1.1)  ── syslog ──▶  R230 (192.168.1.23)
  devid FG100ETK20014183                        /data/fortigate-runtime/input/fortigate.log
  两网段 1.0/24(52台) + 16.0/24(123台)          1.1 GB · ~3 条/秒 · 近 3000 条含 338 个源 IP
  凭据 FGT_BASE / FGT_USER / FGT_PASS           轮转为 fortigate.log-YYYYMMDD-HHMMSS.gz
  (只读 REST)                                    R230_SSH / R230_PASS / R230_LOG
```

日志类型分布(近 3000 条):`traffic` 2498 · `local` 2498 · `event` 498 ·
`system` 262 · `vpn` 236 · `ssl` 125。

## 真实样本(照这个写 parser,不要靠猜)

```
Aug 23 15:55:51 _gateway date=2026-08-23 time=15:55:51 devname="DAHUA_FORTIGATE" devid="FG100ETK20014183" logid="0001000014" type="traffic" subtype="local" level="notice" vd="root" eventtime=1787493351468806055 tz="+0200" srcip=192.168.16.96 srcport=28689 srcintf="port5" srcintfrole="lan" dstip=255.255.255.255 dstport=28689 dstintf="unknown0" dstintfrole="undefined" sessionid=127657661 proto=17 action="deny" policyid=0

Aug 23 15:56:05 _gateway date=2026-08-23 time=15:56:05 devname="DAHUA_FORTIGATE" devid="FG100ETK20014183" logid="0100032002" type="event" subtype="system" level="alert" vd="root" eventtime=1787493365290855265 tz="+0200" logdesc="Admin login failed" sn="0" user="mike" ui="https(45.74.28.226)" method="https" srcip=45.74.28.226 dstip=77.236.99.125 action="login" status="failed" reason="name_invalid" msg="Administrator m...

Aug 23 15:55:52 _gateway ... type="event" subtype="vpn" level="information" logdesc="SSL VPN new connection" action="ssl-new-con" tunneltype="ssl" remip=185.136.15.82 user="N/A" msg="SSL new connection"
```

格式要点:
- 前缀是 syslog 头 `Mon DD HH:MM:SS _gateway `,后面才是 FortiOS 的 KV
- KV 用空格分隔,值可能带引号也可能不带;**带引号的值里可以有空格**
- `eventtime` 是**纳秒**级 epoch;`tz="+0200"` 要用上,别当本地时间
- `N/A` 是 FortiOS 的空值约定,应当规范化成 None

## 解析后的事件形状

```python
@dataclass(frozen=True, slots=True)
class FortiEvent:
    at: datetime            # 由 eventtime + tz 得出，UTC aware
    logid: str
    type: str               # traffic | event | utm | ...
    subtype: str            # local | forward | system | vpn | ...
    level: str              # notice | information | warning | alert
    action: str | None      # deny | accept | login | ssl-new-con | ...
    src_ip: str | None
    dst_ip: str | None
    src_port: int | None
    dst_port: int | None
    proto: int | None
    src_intf: str | None
    dst_intf: str | None
    user: str | None
    logdesc: str | None
    msg: str | None
    status: str | None
    sent_bytes: int | None
    rcvd_bytes: int | None
    raw: Mapping[str, str]  # 其余字段原样保留，不丢信息
```

**解析必须是纯函数**:`parse_line(line) -> FortiEvent | None`。无 IO、无时钟。
解析不了的行返回 None 而不是抛异常——一行坏日志不该中断一条流。

## 尾随器(tailer)的硬要求

1. **默认从文件末尾开始**。1.1 GB 全量重放不是接入,是自杀。回填必须是显式的、
   有界的(例如"回填最近 N 分钟"或"最近 N MB")。
2. **检查点是 (inode, offset)**,不是行号。轮转后 inode 变化 → 从新文件头开始,
   并且要记一条明确的轮转事件,不要静默跳过。
3. **断行安全**:读到一半的行留在缓冲里等下次,不要当成坏行丢掉。
4. **背压**:消费不过来时丢弃**最旧**的而不是最新的,并计数丢了多少。
   丢弃必须可见,静默丢弃等于数据在说谎。
5. **只读**。绝不写、绝不删、绝不轮转源文件。

## 设备画像的形状

```python
@dataclass
class DeviceProfile:
    ip: str
    first_seen: datetime
    last_seen: datetime
    peers: Counter[str]          # dst_ip -> 会话数
    ports: Counter[int]          # dst_port -> 会话数
    interfaces: Counter[str]     # src_intf -> 计数
    accepted: int
    denied: int
    sent_bytes: int
    rcvd_bytes: int
    hourly: dict[str, int]       # ISO 小时 -> 会话数，用于基线
```

**画像是纯统计,不调模型。** 它回答的是"这台设备平时什么样",而"平时"必须是
可计算的:滑动窗口 7 天,窗口外的自然淘汰。

判定"异常"的口径必须能一句话说清,例如:
- 出现了过去 7 天从未出现过的对端
- 会话数超过该设备自身过去 7 天同一时段中位数的 N 倍
- 第一次被 deny / 第一次出现在某个接口上

**不要用 z-score 之类需要解释的统计量。** 运维要能一眼看懂为什么这算异常。

## 命名口径

不用认知科学的分类(情景/语义/程序)。用运维看名字就懂的:

| 组件 | 一句话 |
|---|---|
| 设备画像 | 这台设备平时什么样 |
| 网络拓扑 | 谁挂在哪个口、怎么走 |
| 正常态 | 哪些告警不用管 |
| 病历 | 这台/这条链出过什么事、怎么修的 |
| 有效检查 | 这类问题查什么最快出结论 |
| 变更账 | 谁在什么时候改了什么 |

## 红线

1. **FortiGate 只读。** 有管理凭据不等于可以写。任何 POST/PUT/DELETE 一律不做。
2. **R230 只读。** 只 tail,不写不删不轮转。
3. **绝不切断 Tailscale。** 任何涉及接口、路由、策略的动作都必须先排除
   `tailscale0` / `100.64.0.0/10` / UDP 41641 / DERP。这是远程作业的唯一通道。
4. **记忆不为动作提供依据。** 画像可以说"这台设备今天不正常",不能说
   "所以可以安全重启它"——影响面永远现测。
