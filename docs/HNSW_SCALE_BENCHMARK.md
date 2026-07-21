# FAISS Flat 与 HNSW 十万、百万规模压测

## 结论

HNSW 在百万规模下显著降低查询延迟，但它不是免费的精确索引替代品。当前生产参数
`M=32`、`efConstruction=200`、`efSearch=128` 在 100 万条随机高维向量上只有
0.532 Recall@10。将 `efSearch` 提高到 1024 后，Recall@10 达到 0.846，P95 为
21.37 毫秒，仍快于 Flat 的 36.42 毫秒。

冷构建是更大的工程成本。100 万条 HNSW 建图耗时 909.70 秒，序列化索引为
784.13 MB；同规模 Flat 构建 0.31 秒，索引为 512.00 MB。HNSW 索引持久化后加载
只需 0.57 秒，因此服务端必须采用离线构建、持久化文件和索引代际切换，不能在请求
路径重建。

## 测量口径

- 数据集：确定性生成并归一化的 float32 高斯向量，查询由语料内向量加入 0.02 噪声得到。
- 规模：100,000 和 1,000,000 条，每条 128 维，100 个查询。
- 真值：FAISS `IndexFlatIP` 的精确 top-10。
- HNSW：`IndexHNSWFlat`，内积距离，`M=32`，`efConstruction=200`。
- 延迟：单线程逐查询统计 P50、P95、P99；吞吐使用 8 线程批量查询，取 3 次中位数。
- 构建：8 线程；索引体积取 FAISS 序列化后的真实文件大小。
- Recall@10：HNSW top-10 与 Flat 精确 top-10 的平均交集比例。
- 机器：24 逻辑 CPU、14 GiB 内存；Python 3.10.12、NumPy 2.2.6、FAISS 1.14.3。

这组测试只隔离近邻索引性能，不衡量文本嵌入模型质量，也不代表生产流量。自然语言
相关性仍由真实语料评测负责。

## 构建与内存

| 规模 | 原始向量 | Flat 构建 | HNSW 构建 | Flat 索引 | HNSW 索引 | 进程峰值内存 |
|---:|---:|---:|---:|---:|---:|---:|
| 100,000 | 51.20 MB | 0.0346 s | 30.9938 s | 51.20 MB | 78.42 MB | 354.78 MB |
| 1,000,000 | 512.00 MB | 0.3097 s | 909.6985 s | 512.00 MB | 784.13 MB | 2.11 GB |

数据量扩大 10 倍时，HNSW 冷构建耗时扩大约 29.4 倍。百万规模索引比 Flat 大约
53%，但在线查询能获得明显加速。

## 十万条查询曲线

| 索引 | efSearch | Recall@10 | P95 | 单请求 QPS | 8 线程批量 QPS |
|---|---:|---:|---:|---:|---:|
| Flat | 不适用 | 1.000 | 4.8131 ms | 247.64 | 1,230.10 |
| HNSW | 32 | 0.856 | 0.2360 ms | 5,051.27 | 13,184.39 |
| HNSW | 64 | 0.869 | 0.3761 ms | 2,969.13 | 6,866.22 |
| HNSW | 128 | 0.899 | 0.6626 ms | 1,583.95 | 3,628.06 |
| HNSW | 256 | 0.944 | 1.2620 ms | 820.30 | 1,918.60 |

十万规模下默认 `efSearch=128` 已达到 0.899 Recall@10，P95 约为 Flat 的七分之一。

## 一百万条查询曲线

| 索引 | efSearch | Recall@1 | Recall@10 | P95 | 单请求 QPS | 8 线程批量 QPS |
|---|---:|---:|---:|---:|---:|---:|
| Flat | 不适用 | 1.000 | 1.000 | 36.4152 ms | 27.93 | 102.12 |
| HNSW | 32 | 0.910 | 0.443 | 0.9714 ms | 1,551.70 | 4,855.24 |
| HNSW | 64 | 0.990 | 0.495 | 1.6191 ms | 891.07 | 3,056.68 |
| HNSW | 128 | 1.000 | 0.532 | 2.3303 ms | 505.34 | 1,744.05 |
| HNSW | 256 | 1.000 | 0.604 | 4.0353 ms | 262.33 | 863.00 |
| HNSW | 512 | 1.000 | 0.708 | 9.1712 ms | 121.87 | 399.92 |
| HNSW | 1024 | 1.000 | 0.846 | 21.3665 ms | 51.88 | 185.50 |

百万规模下，`efSearch=128` 能稳定找回精确第一名，但 top-10 邻居覆盖不足。需要
0.8 以上 Recall@10 时，本次数据上的可用档位是 1024；如果业务要求接近精确 top-10，
应继续提高搜索深度或直接使用 Flat，而不是把 HNSW 的 top-1 正确率当成完整召回质量。

## 回归测试

快速契约测试覆盖：

- 数据集与查询的确定性和归一化；
- Recall@k 与延迟分位数计算；
- HNSW 参数透传；
- Flat 与 HNSW 端到端指标结构；
- 索引缓存首次构建、再次加载和召回一致性；
- JSON 原子落盘。

```bash
python -m pytest tests_py/test_vector_index_benchmark.py -q
```

十万和百万性能回归默认跳过，显式开启后检查：最高档 Recall@10 不低于 0.80、存在
满足该召回门槛且 P95 与批量吞吐均优于 Flat 的 HNSW 档位、召回随 `efSearch`
单调增长，以及序列化索引不超过原始向量体积两倍。

```bash
RUN_VECTOR_SCALE_BENCH=1 \
VECTOR_INDEX_CACHE_DIR=/tmp/autopoiesis-index-cache \
python -m pytest -m performance tests_py/test_vector_index_scale.py -q -s
```

本次结果：`2 passed in 51.37s`，百万索引命中缓存。

## 完整复现

```bash
python -m venv .venv-vector-bench
.venv-vector-bench/bin/pip install '.[vector-bench]'
.venv-vector-bench/bin/python -m core.eval.vector_index_benchmark \
  --sizes 100000 1000000 \
  --dim 128 \
  --queries 100 \
  --ef-search 32 64 128 256 512 1024 \
  --index-cache-dir benchmark_results/indexes \
  --output benchmark_results/vector_index_100k_1m.json
```

原始结果文件：

- [`benchmark_results/vector_index_100k.json`](../benchmark_results/vector_index_100k.json)
- [`benchmark_results/vector_index_1m.json`](../benchmark_results/vector_index_1m.json)
