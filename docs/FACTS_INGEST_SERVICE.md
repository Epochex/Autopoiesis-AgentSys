# 单机实时事实与告警服务

`autopoiesis-facts-ingest.service` 是实时数据链的唯一常驻进程。它按本地日志的
`device:inode + byte offset` 读取新增内容，在同一个批次中完成四项工作：

1. 生成稳定 `event_id` 并写入 ClickHouse `facts_v2`。
2. 提取认证和管理面事件并写入 `security_events`。
3. 在进程内完成质量检查、时间窗口检测和稳定 `alert_id` 生成。
4. 写入 ClickHouse `alerts` 和网关读取的原子告警文件。

四类写入全部成功后才推进源文件位置。任何写入失败都会保留原位置，下一次读取重放
相同批次；事实表和告警表使用稳定标识归并，告警文件按稳定标识原子创建。

## 安装

```bash
sudo install -m 0644 \
  /data/Autopoiesis-AgentSys/frontend/deploy/systemd/autopoiesis-clickhouse.service \
  /etc/systemd/system/autopoiesis-clickhouse.service
sudo install -m 0644 \
  /data/Autopoiesis-AgentSys/frontend/deploy/systemd/autopoiesis-facts-ingest.service \
  /etc/systemd/system/autopoiesis-facts-ingest.service
sudo install -m 0644 \
  /data/Autopoiesis-AgentSys/frontend/deploy/systemd/autopoiesis-knowledge-sync.service \
  /etc/systemd/system/autopoiesis-knowledge-sync.service
sudo install -m 0644 \
  /data/Autopoiesis-AgentSys/frontend/deploy/systemd/autopoiesis-knowledge-sync.timer \
  /etc/systemd/system/autopoiesis-knowledge-sync.timer
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/autopoiesis-facts-ingest.service
sudo systemctl enable --now autopoiesis-clickhouse.service
sudo systemctl enable --now autopoiesis-facts-ingest.service
sudo systemctl enable --now autopoiesis-knowledge-sync.timer
```

`/etc/autopoiesis.env` 提供源日志与 ClickHouse 连接配置。单机源使用：

```text
FORTIGATE_SOURCE_LOCAL=1
R230_LOG=/data/fortigate-runtime/input/fortigate-live.log
CLICKHOUSE_URL=http://127.0.0.1:8123
CLICKHOUSE_USER=<user>
CLICKHOUSE_PASSWORD=<password>
AUTOPOIESIS_CLICKHOUSE_DB=autopoiesis
```

实时告警文件默认进入 `/data/autopoiesis-production/stream/alerts`，可通过
`AUTOPOIESIS_ALERT_DIRECTORY` 修改。

## 验收

```bash
sudo /usr/bin/python3 \
  /data/Autopoiesis-AgentSys/frontend/gateway/ingest/facts_ingest.py --status
sudo journalctl -u autopoiesis-facts-ingest.service -n 100 --no-pager
python3 -m json.tool /run/autopoiesis-facts-ingest/status.json
```

状态文件集中记录源位置、事实和安全事件写入数、检测接受与拒绝数、告警写入数和最后
事件时间。`source_committed_offset_bytes` 应持续追上 `source_eof_bytes`，
`source_gap_detected` 应为 `false`，`last_error_type` 应为空。

## 历史回填与查询加速

`backfill` 和 `security-backfill` 只写历史表，不触发生产告警。`optimize` 为已保留分区
补齐案件查询使用的索引和投影。大表物化会产生集中 I/O，应在维护窗口执行。

[`frontend/deployments/12-facts-idempotent-cutover.sql`](../frontend/deployments/12-facts-idempotent-cutover.sql)
保留旧事实表并用读取视图连接历史段和 `facts_v2`。回退脚本位于同目录的
`12-facts-idempotent-rollback.sql`。
