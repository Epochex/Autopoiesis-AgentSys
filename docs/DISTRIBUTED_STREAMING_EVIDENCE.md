# 分布式流处理部署证据

项目的分布式实现保存在 Git 分支 `archive/distributed-redpanda-20260907`。该分支包含：

- FortiGate 采集端的持久发送箱与 Redpanda 发布逻辑。
- Redpanda 消费组驱动的事件质量门、时间窗口检测和告警写入。
- K3s Namespace、Deployment、Service 与数据平面配置。
- 事件处理容器镜像构建文件和对应后端测试。

答辩前执行：

```bash
./scripts/show_distributed_evidence.sh
```

脚本验证分布式分支中的实现文件，并检查以下真实部署材料：

- K3s 节点、资源清单、持久卷映射和服务状态快照。
- Redpanda 主题与事件检测消费组快照。
- R450、R230 两份 Redpanda 数据卷的 SHA-256 校验。
- 本机恢复的 Redpanda 持久卷。

归档根目录为 `/data/archives/netops-paper/20260831T124059Z`。Secret 内容不进入展示脚本输出。

面试或答辩中的准确表述：系统以 Redpanda 的分区日志和消费组解耦采集、检测与案件处理，ClickHouse 保存长期事实并提供时间范围查询；稳定事件键、生产者发送箱和消费者位点共同承担失败重放与幂等处理。
