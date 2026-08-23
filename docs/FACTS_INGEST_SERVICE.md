# 实时 FortiGate facts 采集服务

`netops-facts-ingest.service` 常驻执行现有的 `facts_ingest.py live` 管线。它通过 SSH 在 R230 上只读执行 `tail -n0 -F`，解析 FortiOS KV 日志，并批量写入 ClickHouse `netops.facts`。服务日志只进入 journald。

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

人工试运行和检查：

```bash
sudo systemctl start netops-facts-ingest.service
sudo /usr/bin/python3 \
  /data/Autopoiesis-AgentSys/frontend/gateway/ingest/facts_ingest.py --status
sudo journalctl -u netops-facts-ingest.service -n 100 --no-pager
```

`--status` 输出采集进程和 SSH 子进程状态、最近一次成功批写时间、本次启动后的写入行数、远端源文件的 `device:inode@byte-size`、ClickHouse 累计行数及最新事件时间。`source_offset` 是独立只读 SSH 探测得到的当前文件末端字节位置，可用于识别文件轮转和源文件是否继续增长。`tail -F` 没有持久化消费 checkpoint。

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

当前实时路径的唯一输出端是 ClickHouse。脚本没有 Redpanda producer，因此不会向 `netops.facts.raw.v1` 发布。接入该 topic 会同时扩大依赖、失败处理、消息契约和验证范围，应作为单独变更实施。
