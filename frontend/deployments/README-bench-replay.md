# 基准场景 · 隔离旁路自愈管线 (bench replay self-heal pipeline)

场景2「基准」长轨迹的**实时自愈**由一条与生产完全隔离的旁路管线驱动:注入的故障事件
经真实的 running pod (correlator → alerts-sink → aiops-agent) 消费,产出真实告警与
RCA 建议,落到隔离磁盘目录,再由 gateway 只读暴露给前端 LiveSituation。

## 隔离边界(生产一个资源不碰)
| 维度 | 旁路 | 生产 |
|---|---|---|
| facts topic | `netops.facts.replay.v1` | `netops.facts.raw.v1` |
| alerts topic | `netops.alerts.replay.v1` | `netops.alerts.v1` |
| suggestions topic | `netops.aiops.suggestions.replay.v1` | `netops.aiops.suggestions.v1` |
| consumer group | `*-replay` | `*-v1 / *-v2` |
| 磁盘 sink | `/data/netops-runtime-replay/{alerts,aiops}` | `/data/netops-runtime/{alerts,aiops}` |
| pod | `core-{correlator,alerts-sink,aiops-agent}-replay` | `core-{correlator,alerts-sink,aiops-agent}` |

> correlator / alerts-sink / aiops-agent 的**代码在另一个仓库** `/data/Netops-causality-remediation`
> (真实 Kafka 管线);本仓库(Autopoiesis)只读磁盘 sink。镜像 `netops-core-app`,`IfNotPresent`,r450 本地已有。

## 部署 / 启停
```bash
# 起(默认 template provider,免费、确定性 RCA)
kubectl apply -f 20-bench-replay-pipeline.yaml
kubectl -n netops-core get pods -l tier=replay-sidecar -o wide

# 停(释放资源;隔离 topic/目录留着无害)
kubectl delete -f 20-bench-replay-pipeline.yaml
```

## 触发故障注入 + 看自愈
1. 前端:场景「基准 · ITBENCH+LME」→「长轨迹」→ 点 `注入故障 → REDPANDA`。
2. 或命令行:`curl "http://127.0.0.1:8026/api/rca/replay?inject=1"`
3. 注入后 correlator 对 **6 个 case 全部产 alert**(事件带 `fault_context` → 命中
   correlator 的 `annotated_fault_v1` 单事件规则,不依赖 deny 阈值),aiops 产 RCA 建议。
4. 长轨迹顶部「实时自愈」面板显示 running-pod 产出(告警数 / 建议数 / 逐条诊断)。

## 验证
```bash
kubectl -n netops-core logs deploy/core-correlator-replay --tail=10   # alert emitted rule=annotated_fault_v1 ...
ls -la /data/netops-runtime-replay/alerts /data/netops-runtime-replay/aiops
curl -s "http://127.0.0.1:8026/api/rca/bench-live-situation?lang=zh" | python3 -m json.tool | head
```

## 切换 RCA 引擎:template(免费) ⇄ deepseek(真 LLM,花钱)
```bash
# → 真 LLM 深推理(每次注入触发 DeepSeek 调用,用生产同款 deepseek-api secret)
kubectl apply -f 21-bench-replay-aiops-deepseek.yaml
# → 切回免费 template
kubectl apply -f 20-bench-replay-pipeline.yaml   # 其中的 aiops 段即 template 版
```

## 诚实降级项
- `AIOPS_CLICKHOUSE_ENABLED=false` → `recent_similar_1h=0`、历史富化为空(不影响诊断结论)。
- template provider 是确定性模板 RCA,非 LLM 深推理;要 LLM 质量切 deepseek(见上)。
- alert 的 `alert_ts` 用注入事件的确定性时钟(2026-07-27T00:00:xx);suggestion 用真实
  wall-clock。前端 LiveSituation 按 mtime 取最新文件,不受影响。

## 后端 / gateway
- gateway 由 systemd `netops-ops-console-backend.service` 守护(`Restart=always`),改后端后
  **必须** `systemctl restart netops-ops-console-backend`,不要手动 nohup/kill(会抢 :8026)。
- 端点:`GET /api/rca/bench-live-situation`(读 `netops-runtime-replay`),复用 `runtime_reader.load_runtime_snapshot(settings, lang, runtime_dir=...)`。
