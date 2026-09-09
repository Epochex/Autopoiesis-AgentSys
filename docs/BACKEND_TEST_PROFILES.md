# 后端测试分层

开发阶段执行核心业务闭环：

```bash
python3 scripts/test_backend.py core
```

该入口覆盖事件进入、异常检测、持久化建案、证据调查、自动接案、动作安全、结果回读、复发记忆和业务价值计量。清单位于 `ci/test_suites/core_business.txt`，每一项都对应闭环中的明确状态转换或失败边界。新增或修改闭环节点时，应在清单中增加能够证明该节点输入、持久状态和输出的测试。

版本发布前执行完整回归：

```bash
python3 scripts/test_backend.py release
```

完整回归收集 `tests_py` 下的全部测试，继续覆盖性能测试、公开数据评测、协议边界、存储实现和大规模参数组合。GitHub Python CI 与版本镜像发布均调用该入口，避免本地开发命令和发布准入口径分叉。

两个入口都接受 pytest 参数，例如：

```bash
python3 scripts/test_backend.py core -x --durations=15
python3 scripts/test_backend.py release -q
```

分布式架构模块删除时，同步删除只验证已下线传输、跨节点投影或远端一致性机制的测试。业务事实解析、幂等建案、故障处置与回读测试继续保留。无法确认消费者的测试保留在完整回归中，经过调用关系和状态所有权审计后再处理。
