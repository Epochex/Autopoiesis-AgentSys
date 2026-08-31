# 复发感知升级 · 接口契约

写在实现之前,让并行开发的几方对齐。**这是契约,不是提案** —— 改动请先改本文件。

## 为什么是这个机制,而不是"检索相似历史事故"

调研(2026-08)给出的否定证据:

- **arXiv 2606.15017**(ACL 2026, Xiong et al.):agent 存在 *experience-following*,输入相似度与输出相似度 Pearson r≈1 —— 它在**抄**检索到的案例而不是从中推理。天真累积**比冻结记忆更差**(EHRAgent 16.75% → 13.05%)。
- **arXiv 2606.15017**(ÉTS/ServiceNow, 2026-06):AWM / ASI / ReasoningBank 三个已发表方法,在 **token 对齐**的基线面前增益全部消失。
- **arXiv 2605.23058**(Odmark, 2026-05):K8s 运维记忆的预注册消融,结果 **null**。天真分析的 +19pp 在加入确定性 embedder 对照臂后塌到 **+3.9pp 且不显著**。

在 6 个案例的规模上,一次幸运检索与真实增益不可区分。所以本次实现的是一个
**可以给人看 SQL 的机制**:append-only 日志上的一次聚合,没有 embedding、
没有 LLM 判断、没有幻觉面。

## 现有代码的洞

`core/remediate/sentinel.py`,处置成功后:

```python
if verdict.get("outcome") == "passed":
    self._cooldown_until.pop(detection.key, None)
    self._streak.pop(detection.key, None)
```

服务挂 → 修好 → 二十分钟后再挂 → 系统**完全不记得修过**。而且 `_streak` /
`_cooldown_until` 是进程内 dict,重启即失。这是调研覆盖的所有机制里最宽松的
重置规则:

| 系统 | 重置条件 |
|---|---|
| systemd | 人工 `systemctl reset-failed` |
| Kubernetes | 连续健康 10 分钟 |
| Pacemaker | 静默期内无新失败,且**全清或不清** |
| 本系统(改前) | 成功一次就全忘 |

## 数据来源:时间线就是事实源

`/data/autopoiesis-production/sentinel-timeline.jsonl` 本来就是 append-only 的。
复发计数是它的**派生投影**,进程启动时重建,不新增存储。

这样做的理由(MemSecBench, arXiv 2607.27080):记忆污染的选择性修复失败率
44.9%,所以要设计成**能从不可变日志全量重建**,而不是就地修补一份状态。

## 契约

### 计数键

```
key = (detector, subject, action)
```

**不是**只按 subject。"重启 X 老是不生效"和"X 老出不同的毛病"是两回事——
这是 Pacemaker `start-failure-is-fatal` 和 K8s Job `podFailurePolicy` 的区分。

### 计什么

**resolve-then-recur 周期**,不是原始动作次数:

```
一个周期 = remediated(outcome=passed) → resolved → (之后) 同 key 再次 detected
```

"修了但没生效"和"修生效了但又坏了"是不同信号,只计后者。

### 阶梯

fail2ban `bantime.multipliers` 的形状,medik8s `escalatingRemediations` 的语义:

| 窗口内已复发 | 动作 | 冷却 |
|---|---|---|
| 0 | 执行 | `COOLDOWN_SEC`(600s) |
| 1 | 执行 | `COOLDOWN_SEC × 2` |
| 2 | 执行 | `COOLDOWN_SEC × 4` |
| ≥ `LIMIT`(默认 3) | **拒绝执行** | 记 `escalated`,转人工 |

冷却上限 `COOLDOWN_MAX_SEC`(默认 4h)。窗口 `WINDOW_SEC`(默认 24h)滑动。

环境变量:`AUTOPOIESIS_RECURRENCE_WINDOW` / `AUTOPOIESIS_RECURRENCE_LIMIT` /
`AUTOPOIESIS_RECURRENCE_COOLDOWN_MAX`。

演示要压缩时间,光有这三个不够——还得动哨兵自己的两个(不同模块、不同前缀):
`AUTOPOIESIS_SENTINEL_COOLDOWN`(`sentinel.py`)和 `AUTOPOIESIS_SENTINEL_INTERVAL`
(`sentinel_wiring.py`)。

**下限是 90 秒观察期**:`DEFAULT_BAKE_IN` 在 `frontend/gateway/app/remediation.py`
里写死,不可通过环境变量调。所以每一级台阶至少 ~95 秒,整幕压不到 4 分钟以下。

### 新的时间线事件:`escalated`

```json
{
  "at": "2026-08-22T20:11:03.412+00:00",
  "kind": "escalated",
  "subject": "demo-collector.service",
  "detector": "failed_units",
  "action": "restart_unit",
  "recurrences": 3,
  "window_hours": 24,
  "prior_cycles": [
    {"at": "...", "outcome": "passed", "samples": 12},
    {"at": "...", "outcome": "passed", "samples": 12},
    {"at": "...", "outcome": "passed", "samples": 12}
  ],
  "reason": "同一处置在 24 小时内已经生效过 3 次又复发。重启治不好它——反复被弄坏说明另有原因，转人工。"
}
```

`prior_cycles` 是**引用链**:这条决定是被哪几条历史造成的。这是 AutoManual
(NeurIPS 2024, arXiv 2405.16247)的 `Related Rules:` 字段在运维场景的形式,
调研里唯一提供"哪条记忆改变了这个动作"审计线索的设计。

### Python API — `core/remediate/recurrence.py`

```python
@dataclass(frozen=True)
class Cycle:
    at: str            # resolved 的时刻
    outcome: str
    samples: int

@dataclass(frozen=True)
class History:
    key: str
    cycles: tuple[Cycle, ...]     # 窗口内的 resolve-then-recur 周期，旧→新
    last_resolved_at: str | None

    @property
    def recurrences(self) -> int: ...

def project(events: Iterable[dict], *, now: float, window_sec: float) -> dict[str, History]
    """把时间线事件投影成每个 key 的复发历史。纯函数，无 IO。"""

def history_for(detector: str, subject: str, action: str, *, now: float | None = None) -> History
    """读当前时间线并投影出一个 key 的历史。"""

def cooldown_for(recurrences: int, base_sec: float) -> float
def should_escalate(recurrences: int) -> bool
```

**`project` 必须是纯函数**,这样测试不碰磁盘、演示可回放、口径可复核。

### 前端契约

| 位置 | 要求 |
|---|---|
| `LiveAlerts` | 新相位 `escalated`,标签「**要人工**」,红色,排在最前 |
| `RemediationProgress` | 终态 `escalated`,链条停在「已确认」之后 |
| 剧场事故卡 | 显示复发次数 + 引用链(前几次分别在什么时候修好又复发) |
| `ExecutionLog` | `escalated` 作为阶段分隔线 |
| `sentinel_projection` | `verdict_status="escalated"`,`priority="P1"`,`planStatus="blocked"`,`approvalRequired=True` |

**引用链必须可见。**「凭什么第三次就不修了」这个问题,答案要在屏幕上,不在文档里。

### 演示脚本契约

`scripts/inject_incident.sh` 新增 `recurring` 场景:反复注入同一个故障,
让阶梯在一场演示里走完。为此需要能压缩时间——通过环境变量把窗口和冷却调小,
**不能改代码里的默认值**。

## 边界:不许越过的口径

1. **不许说"系统越用越聪明"。** 它学会的是**在什么时候停手**,方向是更保守。
2. **不许把这个叫"自演化记忆"。** 这是一个持久化计数器加一个阈值,先例是
   systemd/Pacemaker/BGP RFD。说它是"把成熟运维机制补进 LLM agent",比说
   "新颖的自演化"更强也更真。
3. **RFC 7196 的教训要记住。** BGP 路由抖动抑制的原始常数过于激进,严重惩罚
   连接良好的站点,运维界大规模关闭它,IETF 花了十五年才发文说"常数设错了"。
   所以**本次不做惩罚分数**,只做能一句话说清的滑动窗口。

## 参考

- Bainbridge, L. (1983). *Ironies of Automation*. Automatica 19(6):775–779.
  自动控制会"伪装"系统故障,趋势直到失控才显现 —— 本机制针对的正是这个。
- systemd.unit(5) `StartLimitIntervalSec=` / `StartLimitBurst=` / `StartLimitAction=`
- Pacemaker `migration-threshold` / `failure-timeout` / `start-failure-is-fatal`
- Kubernetes KEP-5734 *Automated Pod Hard Reset Policy for Persistent CrashLoopBackOff*
- medik8s Node Health Check Operator `escalatingRemediations`(order + timeout)
- fail2ban `bantime.increment` / `bantime.multipliers`
- RFC 2439 / RFC 7196 BGP Route Flap Damping
- Chen et al. *AutoManual*. NeurIPS 2024, arXiv:2405.16247(`Related Rules:` 字段)
- Xiong et al. ACL 2026, arXiv:2505.16067(experience-following;天真累积更差)
