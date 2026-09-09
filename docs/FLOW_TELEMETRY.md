# 流量采集组件

项目部署包包含中心流量接收、规范化入库和 Linux 端点探针三部分：

| 部件 | 固定版本 | 输入 | 输出 |
|---|---:|---|---|
| GoFlow2 | 2.2.6 | UDP 2055 NetFlow v5/v9、IPFIX，UDP 6343 sFlow | JSON Lines |
| `flow_ingest.py` | 随项目版本 | GoFlow2 JSON Lines 和持久化文件位置 | `facts_v2`、窗口告警和案件事件 |
| pmacctd | 1.7.9 | 指定 Linux 网卡上的数据包头 | 发往中心接收端的 IPFIX v10 |

镜像同时固定版本标签和内容摘要。GoFlow2负责协议解码，项目代码负责字段归一化、稳定
事件标识、断点续传、ClickHouse 写入和采集状态。网关或交换机原生支持 NetFlow/IPFIX
时，直接把导出目的地设为中心接收端，无需安装端点探针。
安装器把采集目录固定授予镜像内 GoFlow2 用户 `100:65533`，规范化进程仍以主机 root
只读该源文件，避免容器改用 root 权限。

## 中心接收端

```bash
sudo /data/Autopoiesis-AgentSys/frontend/script/install-flow-telemetry.sh collector
```

监听地址和端口可在 `/etc/autopoiesis-flow.env` 中设置：

```text
FLOW_LISTEN_ADDRESS=192.168.1.20
FLOW_NETFLOW_PORT=2055
FLOW_SFLOW_PORT=6343
FLOW_METRICS_ADDRESS=127.0.0.1:9091
```

生产环境应在主机防火墙中只允许已登记的交换机、网关和端点探针向这些 UDP 端口发送。

## Linux 端点或镜像口探针

在实际观察流量的主机执行：

```bash
sudo /data/Autopoiesis-AgentSys/frontend/script/install-flow-telemetry.sh \
  endpoint workstation-49 eth2 192.168.1.20 192.168.16.0/20
```

参数依次为探针名称、网卡、中心接收端 IP 和可选采集 CIDR。配置生成器验证网卡名、
IP、端口和 CIDR，随后生成 pmacctd 配置。服务只获得抓包所需的 `NET_RAW` 与
`NET_ADMIN` 能力，根文件系统只读。

端点探针适合主机自身流量或交换机 SPAN 镜像口。普通接入口无法看见同交换机其他
设备之间的流量。全网设备关系需要在网关、三层交换机配置原生流量导出，或把交换机
镜像流量送到专用探针。

## 验收与数据边界

```bash
systemctl status autopoiesis-flow-collector autopoiesis-flow-ingest
python3 /data/Autopoiesis-AgentSys/frontend/gateway/ingest/flow_ingest.py --status
journalctl -u autopoiesis-flow-collector -u autopoiesis-flow-ingest -n 100 --no-pager
```

GoFlow2 接收 NetFlow/IPFIX 模板后才可解码对应数据记录。状态文件记录源文件、已提交
位置、入库行数和拒绝记录数。页面只有在 ClickHouse 中读到真实流量记录后才绘制设备
关系；采集断流会继续显示为观测盲区。

配置参数由类型化生成器产生。案件记忆可依据历史断流、设备角色和调查证据推荐部署点
与观测时长；容器参数、监听地址、网卡、CIDR 和特权能力始终由部署配置与校验规则决定。

## 事实、事件与业务记忆

采集记录进入 `facts_v2` 实时事实层，用于设备连线、会话方向、字节量、端口和接口关系。
采集进程同时把同一批记录交给固定窗口检测器；字节突增等规则命中后，告警原子写入
ClickHouse `alerts` 和网关事件目录，随后由案件同步服务形成持久案件。源文件位置只有在
事实与告警两个写入阶段都成功后才推进。

业务记忆刷新吸收聚合告警，形成有界 `RiskPattern` 和 `NetworkFeature`；已确认调查与完成
处置继续沉淀为 `IncidentDossier`。普通连接仅保留在事实层，全量原始流不会逐条写入
业务记忆。探针过滤自身发往中心 2055/UDP 的 IPFIX 流，避免自采集回路。
探针把通用流过期时间控制在 30 秒、最长生命周期控制在 60 秒，并每 5 秒检查过期，
使页面关系与窗口检测可以持续获得已结束的流记录。
