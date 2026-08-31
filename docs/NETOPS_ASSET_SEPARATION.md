# NetOps 资产冻结与 Autopoiesis 生产边界

## 切割结论

NetOps 保留为论文与历史实验资产。Autopoiesis 负责当前内网数据采集、已知异常检测、案件调查、记忆、处置和页面输出。生产进程不导入 NetOps 源码，不读取 NetOps 磁盘输出，也不消费 `netops.*` 主题。

2026-08-31 切割前现场保存在：

```text
/data/archives/netops-paper/20260831T124059Z
```

源码恢复点：

```text
Autopoiesis tag: pre-netops-separation-20260831T124059Z
NetOps tag:       netops-paper-freeze-20260831T124059Z
```

冻结目录包含两套 Git bundle、NetOps 未提交补丁与未跟踪论文文件、K3s 资源和受保护 Secret 清单、自建镜像、ClickHouse shadow 快照、Redpanda 两个数据卷的停机快照、主题与消费者位点，以及两台节点的数据目录清单。旧 ClickHouse 和 Redpanda 卷的回收策略已设为 `Retain`。

切换完成后，旧 `netops.*` 主题、ClickHouse `netops` 数据库、NetOps 应用 Pod、空的 `edge` namespace、systemd 单元和两台节点上的自建镜像已从在线环境退出。两台节点的 NetOps 工作树已移入冻结区，活动源码入口只保留 Autopoiesis。生产 PostgreSQL 记忆对象写入 `autopoiesis_production` schema，不再读写旧 `public` schema 中的记忆表。

Redpanda 和 ClickHouse 的 StatefulSet、头部 Service 及 PVC 保留创建时的 K3s 对象名，这些对象承载现有数据盘和稳定网络身份。它们已标记 `app.kubernetes.io/part-of=autopoiesis` 与 `asset.autopoiesis.io/origin=netops-frozen`。Autopoiesis 应用只连接 `autopoiesis-redpanda` 和 `autopoiesis-clickhouse` 适配服务，旧对象名不再构成应用契约。

## 当前生产链

```text
R230 FortiGate 追加日志
        |
        v
autopoiesis-facts-ingest
  |  稳定 event_id、来源分类、本地发送箱
  +---------------------------> ClickHouse autopoiesis.facts
  +---------------------------> ClickHouse autopoiesis.security_events
  +---------------------------> autopoiesis.events.raw.v1
                                        |
                                        v
                           autopoiesis-event-pipeline
                             | 质量门、去重、事件时间窗口
                             +------> autopoiesis.alerts.v1
                             +------> ClickHouse autopoiesis.alerts
                             +------> 生产告警文件
                                        |
                                        v
                              InvestigationCase + case_id
                                        |
                     当前证据、只读探针、历史事故与知识检索
                                        |
                                        v
                              BusinessDecision 状态机
                                        |
                       白名单动作、幂等键、观察窗口、结果回读
                                        |
                                        v
                        IncidentDossier / RiskPattern / 记忆
```

已知条件检测只负责触发案件。开放根因、下一项探针、历史记忆使用和动作选择由 Autopoiesis 案件链完成。旧模型建议消费者、论文标注检测器和离线回放 Pod 已退出生产定义。

## 数据与输出所有权

| 对象 | 生产所有者 | 生产名称或位置 | 约束 |
|---|---|---|---|
| 原始事件主题 | Autopoiesis | `autopoiesis.events.raw.v1` | 只接收 `source_kind=real` |
| 告警主题 | Autopoiesis | `autopoiesis.alerts.v1` | 稳定 `alert_id`，至少一次投递 |
| 历史事实 | Autopoiesis | `autopoiesis.facts` | 时间范围精确查询 |
| 安全事件 | Autopoiesis | `autopoiesis.security_events` | `event_id` 归并 |
| 告警记录 | Autopoiesis | `autopoiesis.alerts` | `alert_id` 归并 |
| 案件与会话 | Autopoiesis | `/data/autopoiesis-production/investigations` | 页面、调查与动作共用同一 `case_id` |
| 页面告警输入 | Autopoiesis | `/data/autopoiesis-production/stream` | 网关只读 |
| 受控验收 | 测试资产 | `/data/autopoiesis-test-artifacts` | 不进入生产案件和记忆 |
| 旧论文与回放数据 | NetOps 冻结资产 | 冻结目录与 Retain 卷 | 只读恢复源 |

原 `/data/netops-runtime` 与切换前 `/data/autopoiesis-runtime` 在确认无进程打开、无生产配置引用后，整体移入冻结目录的 `data/retired-live-paths` 下。当前在线输出唯一根目录为 `/data/autopoiesis-production`。

R230 上的旧 `/data/netops-runtime` 已经跨机打包为 `data/retired-live-paths/r230-netops-runtime.tar.zst`，通过 zstd 完整性和 SHA-256 校验后，远端目录改名为 `/data/netops-paper-frozen-20260831`。R230 的 `/data/fortigate-runtime` 是当前原始日志源，持续由 Autopoiesis 采集服务读取。

受控单元前缀 `autopoiesis-acceptance-` 与 `bvaccept-` 被标记为 `controlled_test`。生产案件同步只接收 `observed`。测试进程的写入路径由 pytest 会话目录接管，写入生产目录会直接失败。

## 资源控制

后台长期对象刷新默认只读取最近 7 天，最大允许 30 天。旧实现每轮读取 90 天安全事件，在当前数据量下会扫描千万级记录。新窗口保留近期复发与调查关联，同时压低 ClickHouse 扫描、Python 反序列化和网关内存占用。完整历史仍由按案件触发的精确时间查询访问。

事件处理 Pod 的资源上限为 500m CPU 和 256MiB 内存；采集服务上限为 512MiB；网关上限为 1GiB。状态文件分别记录采集位点、发送箱积压、已接受事件、拒绝原因和已持久化告警数。

ClickHouse 生产身份、K3s Secret、StatefulSet 初始化参数、网关和采集服务已统一为 `autopoiesis`。凭据文件唯一路径为 `/etc/autopoiesis.env`，旧配置文件已移入受保护的冻结目录。

2026-08-31 切换实测中，网关常驻内存约 207 MiB，采集服务约 18 MiB；事件检测消费组的 6 个分区总积压为 0。冷启动 Redpanda 后消费位点继续向前，没有回退到主题起点。生产告警文件使用稳定 `alert_id` 命名和原子创建，切换快照的重复数为 0。

## 切换验收与回退

切换前后必须核对：

1. 两张历史表的行数、最大事件时间和按日分桶数量。
2. 新原始事件主题持续增加，消费者积压在可恢复范围内。
3. 同一受控真实日志批次在新链产生稳定告警标识，并同时到达主题、ClickHouse 和页面文件。
4. 网关健康接口正常，真实案件自动建立，测试案件不进入默认列表。
5. 旧 NetOps 应用 Pod 停止后，新主题、数据库和页面时间戳继续前进。
6. 源码、systemd、K3s 应用配置和输出目录不再引用 NetOps 应用名、主题、数据库或旧写入路径。设备真实主机名与创建时绑定数据盘的 K3s 对象名作为现场身份保留，它们不参与 Autopoiesis 应用连接契约。

回退时先停止 Autopoiesis 采集和事件处理，恢复切割前 systemd 单元与 K3s 清单，再从冻结清单核对主题位点。源码可直接从两个 tag 或 bundle 恢复；数据库可从 ClickHouse shadow 快照恢复；旧持久卷保持 `Retain`。
