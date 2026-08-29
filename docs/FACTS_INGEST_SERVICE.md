# 实时 FortiGate facts 采集服务

`netops-facts-ingest.service` 常驻执行 `facts_ingest.py live` 管线。它通过 SSH 在 R230 上只读执行 `tail -n0 -F`，解析 FortiOS KV 日志，先把批次写入本地持久发送箱并发布到 Redpanda `netops.facts.raw.v1`，随后批量归档到 ClickHouse。Redpanda 暂时不可用时批次保留并重试，ClickHouse 继续接收历史事实。服务日志只进入 journald。

ClickHouse 保存按设备和时间查询的事实历史；Redpanda 把新事件立即交给关联、告警和调查建议服务。离线回填只写 ClickHouse，回放只写隔离话题，两者都不会进入生产事实话题。

## 安装和校验

单元文件有意保留为未启用状态。安装单元不会启动服务，也不会建立到 R230 的连接。

```bash
sudo install -m 0644 \
  /data/Autopoiesis-AgentSys/frontend/deploy/systemd/netops-facts-ingest.service \
  /etc/systemd/system/netops-facts-ingest.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/netops-facts-ingest.service
```

确认 `/etc/selfevo-console.env` 已配置 `R230_SSH`、`R230_PASS`、`R230_LOG`、`CLICKHOUSE_URL`、`CLICKHOUSE_USER`、`CLICKHOUSE_PASSWORD` 和 `CLICKHOUSE_DB`。凭据只从该文件进入进程环境。

生产事件发布还需要：

```text
REDPANDA_PUBLISH_ENABLED=1
REDPANDA_PROXY_URL=http://<netops-redpanda-http ClusterIP>:8082
REDPANDA_TOPIC_RAW=netops.facts.raw.v1
REDPANDA_OUTBOX_DIR=/var/lib/netops-facts-ingest/outbox
```

先应用 `frontend/deployments/11-redpanda-http-proxy-service.yaml`，再用 `kubectl -n netops-core get svc netops-redpanda-http` 读取稳定 ClusterIP。HTTP入口只负责把经过确认的真实事件写入生产话题。

人工试运行和检查：

```bash
sudo systemctl start netops-facts-ingest.service
sudo /usr/bin/python3 \
  /data/Autopoiesis-AgentSys/frontend/gateway/ingest/facts_ingest.py --status
sudo journalctl -u netops-facts-ingest.service -n 100 --no-pager
```

`--status` 输出采集进程和 SSH 子进程状态、最近一次成功批写时间、本次启动后的写入行数、Redpanda发布数和待重试队列、远端源文件的 `device:inode@byte-size`、ClickHouse累计行数及最新事件时间。`source_offset` 是独立只读SSH探测得到的当前文件末端字节位置，可用于识别文件轮转和源文件是否继续增长。`tail -F` 目前仍没有持久化源文件检查点。

确认试运行结束时需要停服务：

```bash
sudo systemctl stop netops-facts-ingest.service
```

## 人工启用

只有在运维人员确认允许常驻连接生产设备后，才执行以下明确的启用命令：

```bash
sudo systemctl enable --now netops-facts-ingest.service
```

SSH 中断后，脚本每 5 秒重新连接；进程退出后，systemd 每 10 秒重启。`MemoryHigh=384M` 提前施加内存压力，`MemoryMax=512M` 提供硬上界。

## Redpanda失败处理

每批事件先写入 `/var/lib/netops-facts-ingest/outbox`，完成文件刷盘和原子改名后再写ClickHouse并发布Redpanda。发布成功才删除队列文件。网络中断或Redpanda重启期间，文件按顺序保留并在后续批次重试。事件编号由来源、观察时间和原始日志内容稳定计算，重复补发可以被下游质量门识别。

队列默认上限512MiB。达到上限后采集服务明确报错，避免静默丢弃。该队列解决ClickHouse写入完成而Redpanda短时不可用造成的流式事件丢失；源文件检查点仍需在后续变更中补齐。
