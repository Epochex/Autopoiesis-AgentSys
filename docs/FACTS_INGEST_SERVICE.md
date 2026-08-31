# 实时 FortiGate facts 采集服务

`autopoiesis-facts-ingest.service` 常驻执行 `facts_ingest.py live` 管线。它通过 SSH 按字节位置只读 R230 的 FortiOS KV 日志，解析完成后先把批次写入本地持久发送箱并发布到 Redpanda `autopoiesis.events.raw.v1`，随后批量归档到 ClickHouse `autopoiesis` 数据库。Redpanda 暂时不可用时批次保留并重试，ClickHouse 继续接收历史事实。服务日志只进入 journald。

ClickHouse 保存按设备和时间查询的事实历史；Redpanda 把新事件立即交给关联、告警和调查建议服务。离线回填只写 ClickHouse，回放只写隔离话题，两者都不会进入生产事实话题。

## 安装和校验

单元安装与启动分开执行，便于先完成配置校验。当前生产机已启用该单元。

```bash
sudo install -m 0644 \
  /data/Autopoiesis-AgentSys/frontend/deploy/systemd/autopoiesis-facts-ingest.service \
  /etc/systemd/system/autopoiesis-facts-ingest.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/autopoiesis-facts-ingest.service
```

确认 `/etc/autopoiesis.env` 已配置 `R230_SSH`、`R230_PASS`、`R230_LOG`、`CLICKHOUSE_URL`、`CLICKHOUSE_USER`、`CLICKHOUSE_PASSWORD` 和 `CLICKHOUSE_DB`。凭据只从该文件进入进程环境。

生产事件发布还需要：

```text
REDPANDA_PUBLISH_ENABLED=1
REDPANDA_PROXY_URL=http://<autopoiesis-redpanda-http ClusterIP>:8082
REDPANDA_TOPIC_RAW=autopoiesis.events.raw.v1
REDPANDA_OUTBOX_DIR=/var/lib/autopoiesis-facts-ingest/outbox
```

先应用 `frontend/deployments/11-data-plane-services.yaml`，再读取 `autopoiesis-redpanda-http` 的稳定 ClusterIP。宿主机采集服务使用 HTTP 入口；集群内事件处理使用 `autopoiesis-redpanda:9093` 原生 Kafka 入口。

人工试运行和检查：

```bash
sudo systemctl start autopoiesis-facts-ingest.service
sudo /usr/bin/python3 \
  /data/Autopoiesis-AgentSys/frontend/gateway/ingest/facts_ingest.py --status
sudo journalctl -u autopoiesis-facts-ingest.service -n 100 --no-pager
```

`--status` 输出采集进程、最近一次成功批写时间、本次启动后的写入行数、Redpanda 发布数和待重试队列、远端源文件末端、已提交字节位置、轮转恢复或缺口状态、ClickHouse 累计行数及最新事件时间。持久位置保存在 `/var/lib/autopoiesis-facts-ingest/source-checkpoint.json`，键为远端文件的 `device:inode` 与已提交字节数。进程只在一个批次的发送箱、流量事实和安全事件全部写入完成后推进位置。普通 SSH 中断会从该位置继续；文件轮转时先按 inode 排空未压缩旧文件，再从新文件字节零开始。旧文件已经压缩且无法定位时，状态中的 `source_gap_detected` 置为 true，后续使用稳定事件键执行补数。

确认试运行结束时需要停服务：

```bash
sudo systemctl stop autopoiesis-facts-ingest.service
```

## 启用生产采集

配置校验通过后执行：

```bash
sudo systemctl enable --now autopoiesis-facts-ingest.service
```

采集端默认每 5 秒检查源文件位置；SSH 中断后按相同间隔重试，进程退出后 systemd 每 10 秒重启。五秒窗口把源端到 Redpanda 的延迟维持在告警窗口以内，同时降低频繁 SSH 建连的 CPU 开销。`MemoryHigh=384M` 提前施加内存压力，`MemoryMax=512M` 提供硬上界。

## Redpanda失败处理

每批事件先写入 `/var/lib/autopoiesis-facts-ingest/outbox`，完成文件刷盘和原子改名后再写ClickHouse并发布Redpanda。发布成功才删除队列文件。网络中断或Redpanda重启期间，文件按顺序保留并在后续批次重试。事件编号由来源、观察时间和原始日志内容稳定计算，重复补发可以被下游质量门识别。

队列默认上限512MiB。达到上限后采集服务明确报错，避免静默丢弃。该队列解决 ClickHouse 写入完成而 Redpanda 短时不可用造成的流式事件丢失；源文件位置解决 SSH 中断窗口的补读，`facts_v2.event_id` 解决补读批次的重复归档。

## ClickHouse 切换与回滚

[`frontend/deployments/12-facts-idempotent-cutover.sql`](../frontend/deployments/12-facts-idempotent-cutover.sql) 保留原 `facts` 为 `facts_legacy`，创建带稳定事件键的 `facts_v2`，再用同名读取视图联合两段数据。采集服务切换后只写 `facts_v2`，现有查询继续读取 `autopoiesis.facts`。执行 DDL 时需短暂停止采集服务，并在重启前保存源文件位置。

回滚脚本位于 [`frontend/deployments/12-facts-idempotent-rollback.sql`](../frontend/deployments/12-facts-idempotent-rollback.sql)。回滚只恢复旧表名，切换后已经写入 `facts_v2` 的记录继续保留，可在修复后重新接回读取视图。
