# 动态记忆索引生命周期：研究选型与实现

## 结论

本项目保留 FAISS HNSW 作为不可变基础索引，在它外面增加精确增量层、文档版本表、删除标记、后台压缩和原子代际切换。BM25 使用同样的分段生命周期，但维护全局文档数、文档频率和平均文档长度，确保分段前后的分数与单体 BM25 一致。

这一方案适合当前百万级、Python、BM25 与 FAISS 的代码基础。它没有修改 HNSW 图，也没有复刻一套未经验证的磁盘向量数据库。FAISS 官方文档明确指出 HNSW 不支持删除节点，因为删除会破坏图结构；因此，旧版本必须先由版本表过滤，再由后台重建物理回收。[FAISS 索引能力说明，2025-07-28](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes)

## 一手研究与生产实现

FreshDiskANN 将持续变更写入内存临时索引，查询合并长期索引和临时索引，删除由列表过滤，达到容量阈值后执行流式合并。论文在十亿级数据上报告了实时插入、删除和查询能力，证明“基础索引加增量索引加后台合并”能够长期维持召回率。[FreshDiskANN，2021](https://www.microsoft.com/en-us/research/publication/freshdiskann-a-fast-and-accurate-graph-based-ann-index-for-streaming-similarity-search/)

SPFresh 的 LIRE 协议直接分裂过大的向量分区，并重新分配邻近分区的边界向量。它适合十亿级、高更新率的磁盘分区索引；本项目只有百万级数据，采用其分区内原地更新会引入 SPTAG/C++ 存储层，工程成本超过收益。[SPFresh，SOSP 2023](https://www.microsoft.com/en-us/research/publication/spfresh-incremental-in-place-update-for-billion-scale-vector-search/)

2025 年的 Greator 只定位并修改受更新影响的图页面。论文报告小批量更新吞吐比 FreshDiskANN 高 2.39 至 5.96 倍。这是更前沿的磁盘图研究方向，但公开实现是 C++，代码仓库没有明确可复用许可证，当前项目只参考机制，不复制实现。[Greator，PVLDB 19(3)](https://www.vldb.org/pvldb/vol19/p495-yu.pdf)

Quake 根据分区大小和访问频率动态分裂或合并分区，并在线调整扫描分区数以满足召回目标。OSDI 2025 的实验显示，它在动态偏斜负载下优于静态 HNSW、DiskANN 和 ScaNN。该算法针对千万级动态访问分布；在没有真实查询偏斜数据之前，本项目不引入自适应分区器。[Quake，OSDI 2025](https://www.usenix.org/conference/osdi25/presentation/mohoney)

生产系统采用了相同的生命周期边界。Milvus 把实时数据写入增长段，把完成持久化的历史数据封存为不可变段；查询同时访问两者，压缩完成后执行交接并释放冗余段。[Milvus 数据处理架构](https://milvus.io/docs/data_processing.md) Qdrant 先以删除标记隐藏记录，当单段删除比例达到 20% 且至少包含 1000 个向量时启动 Vacuum Optimizer；重建期间旧段继续服务，新写入进入写时复制段。[Qdrant Optimizer](https://qdrant.tech/documentation/operations/optimizer/)

另一种路线是让 pgvector 承担事务内向量检索。pgvector 官方建议 HNSW 清理缓慢时先并发重建索引再执行 VACUUM，并用精确搜索持续监测召回率。[pgvector 官方文档](https://github.com/pgvector/pgvector) 当前实现只把 PostgreSQL 用作记忆事实源和有序事件源，向量仍走独立生命周期，这样事务一致性与检索引擎演进互不绑死。

## 当前实现

```text
PostgreSQL 当前状态 + 只追加有序事件
    │
    ├── 分段 BM25：热增量倒排 + 不可变封存段
    │                 │
    │                 └── 全局 N、df、avgdl 统一评分
    │
    └── 向量索引：精确 Flat 增量层 + 不可变 HNSW 基础代际
                      │
                      └── doc_id/version 过滤旧版本和删除记录

达到阈值后
    后台捕获一致视图 → 锁外构建 → 校验无并发写入 → 短锁原子切换
                                              │
                                              └── 回收旧版本和删除标记
```

`SegmentedBM25Index` 的封段只处理小增量，不在写请求中执行全量压缩。压缩由 `IndexMaintenanceWorker` 定期检查或主动唤醒；失败、并发中止、耗时和成功次数都可观测。快照采用规范化 JSON、SHA-256 校验、文件刷盘和 `os.replace` 原子替换，并保存 `generation` 与 `applied_offset`。

`VectorIndexLifecycle` 的基础代际使用 HNSW，增量层使用精确 Flat。更新相同 `doc_id` 时写入更高版本，查询只接受版本表中的当前版本；删除立即对查询不可见。压缩在锁外重新构建 HNSW，只有事件序号和源代际未变化时才安装。持久化先写完整快照目录和文件清单，再原子替换 `CURRENT` 指针，并保留上一代用于回滚。

`PostgresMemoryRepository` 在一个事务中更新完整记忆状态并追加全量快照事件。记录版本用于拒绝丢失更新，全局事务锁保证事件偏移按提交顺序可见，数据库触发器禁止更新、删除或截断历史事件；索引检查点只能单调推进且不能越过已提交高水位。`TieredMemoryStore` 启动时从事实表恢复完整记录，再重建本地派生索引；每轮通过验证的后台整合完成后批量提交，未变化记录不制造空事件。

BM25 不能直接合并各段局部分数，因为每段的文档数、文档频率和平均长度不同。当前实现只让各段倒排表产生候选，最终使用活跃集合的全局统计统一评分。150 次随机增删改测试逐步与重新构建的单体 BM25 对照，排名和分数完全一致。

## 压缩触发策略

当前默认值采用 Qdrant 的生产起点，而不是把实验负载误写成通用结论：物理条目至少 1000 条且无效比例达到 20%，或者不可变段达到 8 个时，BM25 标记为需要压缩；向量索引在增量达到一万条、增量达到基础索引的 10%，或者无效向量达到 15% 时标记为需要压缩。

维护线程默认每 60 秒检查一次。检查不等于重建，只有指标越过阈值才执行。线上还应加入磁盘剩余空间、最近一次构建耗时、查询 P95 和固定评测集 Recall@10 门禁。当预计再次达到阈值的时间短于最近一次压缩耗时，说明维护吞吐追不上写入，需要增加构建资源或更换 DiskANN3、Qdrant 等持续更新后端。

更换嵌入模型、向量维度、距离函数、文本切分规则或 HNSW 构建参数必须创建完整新代际，不能通过普通增量更新处理。

## 十万规模变更测试

稀疏索引使用十万条合成文档，随后更新一万条并删除一万条。旧查询路径每次重新创建 BM25，5 次采样的 P95 为 908.26 ms；增量倒排查询 P95 为 11.57 ms，快 78.49 倍。压缩前有十二万条物理版本和九万条有效文档，压缩后物理条目降为九万，回收 25%。压缩耗时 0.59 秒，重启加载耗时 1.90 秒；压缩前、压缩后和重启后的 Top-10 均与单体 BM25 完全一致。

向量索引使用十万条 128 维确定性向量，更新一万条并删除一万条。压缩前有十一万条物理向量和九万条有效向量，压缩后回收两万条旧向量。查询 P95 从 1.34 ms 降到 0.98 ms，吞吐从 751.30 QPS 提升到 1252.19 QPS；Recall@10 从 0.898 变为 0.912。HNSW 重建耗时 19.79 秒，快照加载耗时 0.72 秒，重启后的查询结果逐项一致。

这些数字衡量索引引擎和生命周期，不代表自然语言检索质量。向量来自合成分布，正式发布仍需使用 384 维业务嵌入和持续更新流重复测量。

复现命令：

```bash
python3 -m core.eval.index_lifecycle_benchmark \
  --size 100000 --updates 10000 --deletes 10000 --queries 100

python3 -m core.eval.vector_lifecycle_benchmark \
  --size 100000 --dimension 128 --updates 10000 --deletes 10000 --queries 50
```

## 部署边界

已实现并在 PostgreSQL 17 上验证事实表、事件表、并发版本冲突、事务回滚、重启恢复、事件回放和检查点约束。只有配置 `AUTOPOIESIS_MEMORY_DSN` 的部署使用跨会话事实持久化；未配置时仍是便于确定性评测的进程内模式。BM25/HNSW 快照始终是派生数据，损坏时应由 PostgreSQL 状态或事件重建，不能反向覆盖业务事实。
