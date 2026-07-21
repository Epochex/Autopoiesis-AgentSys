# 长轨迹逐节点可观测性

在线运行以本地追加式节点事件流为事实源。每个节点在执行前写入 `started`，结束后写入
`finished`；两者通过 `trace_id`、`span_id` 和 `parent_span_id` 还原调用树。进程中断时，
没有结束事件的节点会被明确标成未完成，不会因为缺少耗时数据而从轨迹里消失。

`run_id` 对应一次诊断轨迹，`session_id` 把同一事故或长期任务的多次运行关联起来。
当前在线链路覆盖记忆召回、演变链分析、技能筛选、每个工具调用、上下文编译、推理、
引用核验、记忆整合、索引投影、索引维护触发、后台压缩以及业务事件持久化。节点输出
只保存有界摘要，密钥、令牌和认证字段在落盘前脱敏。

## 两条事件流的职责

`core/trace/ledger.py` 保存影响业务事实与学习结果的类型化事件，并在诊断完成、步骤核验、
技能晋升和回滚等边界执行刷盘。`core/observability/ledger.py` 保存高频节点开始/结束事件，
用于性能分析和交互展示。它采用进程安全追加，但不为每个节点重复执行同步刷盘，避免观测
系统制造业务延迟；机器突然掉电时可能丢失操作系统缓冲区中的尾部观测，业务事实仍由前一条
事件流保证。两条流不能互相替代。

`TraceAnalyzer` 一次读取后按轨迹分组，输出失败、部分完成、未完成节点、最慢节点、瓶颈、
节点指标和同一会话的演变序列。后台索引维护使用独立轨迹，并用
`triggered_by_run_id` 保留与前台运行的因果关联，因为后台任务可能晚于请求结束。

## 查询接口

- `GET /api/rca/observability/traces`：最近轨迹，可按 `session_id` 过滤。
- `GET /api/rca/observability/traces/{trace_id}`：一条轨迹的完整父子节点和指标。
- `GET /api/rca/observability/sessions/{session_id}`：跨运行性能、失败和演变趋势。

诊断接口分别返回 `diagnosisVerified` 和 `memoryCommitted`。只有核验成功且记忆提交成功时
`ok` 才为真，避免把“给出了诊断但学习落库失败”报告成完整成功。

## Langfuse 的位置

后端正确性不依赖 Langfuse。设置 `AUTOPOIESIS_ENABLE_LANGFUSE=1` 后，本地完整轨迹通过
有界后台队列投影到 Langfuse，用于跨会话查询、人工评分和团队仪表盘；远端变慢、队列已满
或导出失败只增加健康计数，不阻塞诊断，也不改变本地事实。默认不开启该投影。

安装与配置：

```bash
pip install -e '.[observability]'
export AUTOPOIESIS_ENABLE_LANGFUSE=1
export LANGFUSE_PUBLIC_KEY=...
export LANGFUSE_SECRET_KEY=...
export LANGFUSE_BASE_URL=...
```

服务关闭时会在限定时间内刷新导出队列；未能导出的数量仍可从 observer 健康状态读取。
