# 演示稿 · 记忆从哪来、何时作废、何时让系统停手

第一幕回答「现在记住了什么」，第二幕证明一次真实故障会写入在线记忆，第三幕演事实变更和
时间旅行，第四幕演复发升级，最后一幕用消融回答「记忆到底有没有用」。

时间紧就演第一、二、三、五幕。第四幕约 9 分钟，只有需要证明记忆会改变动作时再跑。

在线接口是 `http://192.168.1.27:2026/api/rca/memory`，本机命令走
`http://127.0.0.1:8026/api/rca/memory`。

---

## 开演前 · 三个条件不满足就停（1 分钟）

### 敲什么命令

```bash
cd /data/Autopoiesis-AgentSys

python3 - <<'PY'
import json
from urllib.request import urlopen

health = json.load(urlopen("http://127.0.0.1:8026/api/healthz", timeout=5))
memory = json.load(urlopen("http://127.0.0.1:8026/api/rca/memory", timeout=5))
print("status=", health["status"])
print("durableMemory=", health.get("durableMemory"))
print("memory.durable=", memory["durable"])
print("counts=", memory["counts"])
print("budget=", memory["budget"])
assert health["status"] == "ok", health
assert health.get("durableMemory") is True, "没有持久化记忆，哨兵事故不会写入在线库"
assert memory["durable"] is True, "这个接口当前只展示进程内数据"
PY

./scripts/inject_incident.sh status

gateway_pid=$(systemctl show netops-ops-console-backend -p MainPID --value)
tr '\0' '\n' < "/proc/${gateway_pid}/environ" | grep '^AUTOPOIESIS_SENTINEL=1$'
```

再确认这台演示机以前没有学过同一个演示事故：

```bash
python3 - <<'PY'
import json
from urllib.request import urlopen

payload = json.load(urlopen(
    "http://127.0.0.1:8026/api/rca/memory?include_quarantined=true&limit=1000",
    timeout=5,
))
rows = [
    row for row in payload["records"]
    if "root:sentinel.failed_units" in row["tags"]
]
print("已有 sentinel.failed_units 记忆：", len(rows))
for row in rows:
    print(row["memory_id"], row["tier"], row["last_observed_at"])
PY
```

### 屏幕上会出现什么

第一段应显示 `status=ok`、两个持久化字段都是 `True`。网关默认容量预算是 64，接口的
`budget.configured` 会给出本实例实际值。最后一行必须打印 `AUTOPOIESIS_SENTINEL=1`。
第二段在干净演示库上应显示 0。

专用演示机如果留有上一轮时间线，先按现有演示稿的规则清掉，只让这次事故参与复发投影：

```bash
./scripts/inject_incident.sh cleanup
rm -f /data/autopoiesis-runtime/sentinel-timeline.jsonl
systemctl restart netops-ops-console-backend
```

时间线是审计日志，这三行只用于开演前重置专用演示环境。

如果第二段大于 0，这次再跑相同事故可能更新旧记录，无法证明「新长出一条」。保留在线库，
换到预先准备的干净演示库再演。`cleanup` 只清故障注入和演示覆盖，它不会清记忆。

### 这一刻该说哪句话

> 我先把证明条件说清楚。下面看的必须是持久化在线记忆；进程内列表重启就丢，不能拿来证明
> 持续学习。这个演示事故也必须是第一次进入这份库，否则看到的是复用和强化，证据问题已经变了。

---

## 第一幕 · 先打开在线记忆库（1 分钟）

### 敲什么命令

浏览器现在就打开：

```text
http://192.168.1.27:2026/api/rca/memory
```

终端同时给出一份容易念的摘要：

```bash
python3 - <<'PY'
import json
from urllib.request import urlopen

payload = json.load(urlopen("http://127.0.0.1:8026/api/rca/memory", timeout=5))
print("durable=", payload["durable"])
print("counts=", payload["counts"])
print("budget=", payload["budget"])
print("retention=", payload["retention"])
for row in payload["records"]:
    print(row["tier"], row["memory_id"], row["text"])
PY
```

五条领域先验从哪里来，也当场验：

```bash
python3 - <<'PY'
from domains.network_rca.factory import load_memory_records

rows = load_memory_records()
print("领域先验：", len(rows))
for row in rows:
    print(row.memory_id, row.tier, "seed" in row.tags)
PY
```

### 屏幕上会出现什么

接口顶部先给 `counts`，随后是每条记录。列表项能看到层级、正文、资产、置信度、重要度、
强度、访问次数、双时间轴和来源轨迹数量。需要追一条证据时，把它的 `memory_id` 接在
`/api/rca/memory/` 后面，详情接口会给完整来源轨迹 ID、关系和证据 ID 数量。

第二条命令固定打印 5 条，并且每条最后都是 `True`。这 5 条来自仓库里的人工知识文件：
1 条设备画像、1 条网络关系、3 条排查顺序。

在线库只有这 5 条时，表示还没有学到事故记录。在线库多于 5 条时，新增部分才可能来自核实后写回；
逐条看 `source_trace_ids` 和详情接口，不能拿总数猜来源。

### 这一刻该说哪句话

> 你问它现在记住了什么，我就在事故发生前打开这个接口。这里列的是在线库，`durable=true`
> 表示来源是持久化快照。最初的 5 条是人写的领域先验，系统没有学出这 5 条。我只把核实后新增、
> 带来源轨迹的记录叫作学到的记忆。

### 被追问「怎么检索」时再敲

```bash
python3 - <<'PY'
from core.memory import store

print("asset_hit=", store._W_ASSET_HIT)
print("entity_hit=", store._W_ENTITY_HIT)
print("dense_route_coef=", store._DENSE_ROUTE_COEF)
print("structural_rerank_coef=", store._STRUCT_COEF)
PY
```

> 第一段是查询相关分：全文和标签走 BM25，精确资产和精确运维对象各加 2。可选的向量召回只按
> 最强精确或词法分的 0.35 倍缩放。生命周期结构分只在最后做有界重排，上限按最高基础分的 0.15
> 缩放，所以它能调近似并列，压不过一个明确的词法或资产命中。

---

## 第二幕 · 注入一次真故障，在线记忆长出事故记录（约 3 分钟）

### 敲什么命令

先记住注入前的数量，随后制造真实的 systemd 故障：

```bash
python3 - <<'PY'
import json
from urllib.request import urlopen

p = json.load(urlopen("http://127.0.0.1:8026/api/rca/memory", timeout=5))
print("注入前 active=", p["budget"]["active"], "counts=", p["counts"])
PY

./scripts/inject_incident.sh service-down
```

等态势页那条事故从「刚发现」走到「已自愈」。如果不想等下一轮巡检，可以手动推进一次，
每次命令都是真实轮询：

```bash
curl -fsS -X POST http://127.0.0.1:8026/api/rca/sentinel/poll | python3 -m json.tool
```

看到 `resolved` 后，回到第一幕已经打开的 `/api/rca/memory`，浏览器刷新。终端再验一次：

```bash
python3 - <<'PY'
import json
from urllib.request import urlopen

base = "http://127.0.0.1:8026/api/rca/memory"
p = json.load(urlopen(base + "?include_quarantined=true&limit=1000", timeout=5))
rows = [row for row in p["records"] if "root:sentinel.failed_units" in row["tags"]]
print("注入后 active=", p["budget"]["active"], "counts=", p["counts"])
print("本事故相关记录：", len(rows))
for row in rows:
    print(row["tier"], row["memory_id"], "traces=", row["source_trace_ids"])

episodic = next(row for row in rows if row["tier"] == "episodic")
detail = json.load(urlopen(base + "/" + episodic["memory_id"], timeout=5))["record"]
print("事故正文=", detail["text"])
print("来源轨迹=", detail["source_trace_ids"])
print("证据索引=", detail["evidence_snapshot"])
PY
```

### 屏幕上会出现什么

干净演示库会新增一条 `episodic` 事故记录，标签含 `root:sentinel.failed_units`，详情里有非空的
来源轨迹和证据索引。首次通过核实的事故还会形成可复用的语义模式和排查步骤；到底新增几条，
现场以注入前后的 `active` 和 `counts` 差值为准。

写入门槛能从时间线直接复核：必须有处置通过、观察期通过和最终 `resolved`。拒绝、升级、
回退未核实的链只保留「该动作没有被证明有效」这一类负向记录，不进入成功经验路径。

### 这一刻该说哪句话

> 这一步是「持续学习」唯一的实证。刚才先看了库，现在同一个在线接口真的多出了一条事故记录，
> 而且详情能追到这次哨兵链的轨迹和证据。故障、处置、90 秒观察期和回读都真实发生；只有核实
> 通过的链能成为成功经验。页面动画、离线重放和一段更长的提示词都不能替代这条写入证据。

> 这里的记忆先影响排查顺序，帮助回答「先看哪里」和「以前这样过吗」。它没有取得动作授权，
> 后面的复发账本才会直接拦住一个动作。

---

## 第三幕 · 同一个事实变了，旧值撤销但不删除（2 分钟）

当前环境事实采集器尚未接入在线调用点，所以这一幕直接执行仓库里的 `observe_fact` 和
`retrieve(as_of=...)`。它证明按键撤销与历史重建已经实现；它不证明在线哨兵正在自动采集这类事实。

### 敲什么命令

先核对调用边界。当前命令只会找到函数定义：

```bash
rg -n 'observe_environment\(' core domains frontend
```

再运行按键撤销和时间旅行查询：

```bash
python3 - <<'PY'
from datetime import datetime, timezone
from core.evolve.memory_ops import observe_fact
from core.memory.store import TieredMemoryStore

store = TieredMemoryStore()
t0 = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
t1 = datetime(2026, 8, 22, 10, 5, tzinfo=timezone.utc)

old_id = observe_fact(
    store,
    subject="eth2",
    relation="link_state",
    value="up",
    observed_at=t0,
)
new_id = observe_fact(
    store,
    subject="eth2",
    relation="link_state",
    value="down",
    observed_at=t1,
)

for record in store.records():
    print(
        record.text,
        "valid_from=", record.valid_from.isoformat(),
        "valid_to=", record.valid_to.isoformat() if record.valid_to else None,
    )

for label, instant in [("10:00", t0), ("10:05", t1)]:
    rows = store.retrieve(
        ["link_state"], ["eth2"], limit_per_tier=5, as_of=instant
    )["semantic"]
    print(label, [record.text for record in rows])

print(
    "records=", len(store.records()),
    "old_kept=", store.get(old_id) is not None,
    "new_kept=", store.get(new_id) is not None,
)
PY
```

### 屏幕上会出现什么

```text
eth2 link_state up   valid_from=2026-08-22T10:00:00+00:00 valid_to=2026-08-22T10:05:00+00:00
eth2 link_state down valid_from=2026-08-22T10:05:00+00:00 valid_to=None
10:00 ['eth2 link_state up']
10:05 ['eth2 link_state down']
records=2 old_kept=True new_kept=True
```

核心是同一个 `(subject, relation)`。新值先追加，旧值再写 `valid_to`；有效区间是
`[valid_from, valid_to)`。查询过去时刻只能看到旧值，查询变更时刻只能看到新值。

### 这一刻该说哪句话

> 一个事实以前是真的，现在变了，系统保留两条记录。旧记录有了结束时间，当前检索不再使用它；
> `retrieve(as_of=过去时刻)` 仍能重建当时的信念。这是 Zep 的 bi-temporal 模型，字段上把世界有效
> 时间和观察时间分开。理论根基是 Katsuno 与 Mendelzon 在 KR'91 讨论的「更新」，对应世界发生
> 变化；AGM 的「修正」处理同一个世界里信念集合的纠错。不是你原来错了，是世界变了。

### 顺手把鲜度边界讲清楚

```bash
python3 - <<'PY'
from core.evolve.memory_ops import _STALE_AFTER_SEC
print(_STALE_AFTER_SEC)
PY
```

> 鲜度只降权，不删除历史。设备现状 300 秒没有重新确认就完全陈旧；事故、模式和排查步骤按
> 2,592,000 秒，也就是 30 天分层。被撤销的记录鲜度直接是 1，但仍留在库里供审计和过去时刻查询。

---

## 第四幕 · 同一处置复发三次，第四次拒绝执行（约 9 分钟）

第二幕留下的 `resolved` 会影响复发计数。先清演示时间线，让这一幕从第一级开始；这一步只清
演示审计日志，不清在线记忆。

### 敲什么命令

```bash
./scripts/inject_incident.sh cleanup
rm -f /data/autopoiesis-runtime/sentinel-timeline.jsonl
systemctl restart netops-ops-console-backend
./scripts/inject_incident.sh status
./scripts/inject_incident.sh recurring
```

脚本会临时把 24 小时窗口压到 1 小时，把 600 秒基础冷却压到 30 秒，拒绝阈值保持 3。
这些数字由 `status` 展示网关进程实际读到的值，命令不会靠配置文件猜进程状态。

### 屏幕上会出现什么

前三次都完整走过：

```text
发现 → 二次确认 → 前置校验 → 执行 → 观察期 → resolved
```

第四次只走到确认：

```text
发现 → 二次确认 → escalated，拒绝执行，转人工
```

态势页会把这条事故标成红色「要人工」。剧场事故卡会显示窗口内复发 3 次，并逐条列出前三次
何时修好又复发。终端用同一份不可变时间线复算：

```bash
python3 - <<'PY'
import json
import time
from pathlib import Path
from core.remediate.recurrence import project

path = Path("/data/autopoiesis-runtime/sentinel-timeline.jsonl")
events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
histories = project(events, now=time.time(), window_sec=3600)
for key, history in histories.items():
    if "demo-collector.service" in key:
        print(key, "recurrences=", history.recurrences)
        for cycle in history.cycles:
            print(cycle.as_dict())

escalated = [event for event in events if event.get("kind") == "escalated"]
print("escalated=", len(escalated))
print(json.dumps(escalated[-1], ensure_ascii=False, indent=2))
PY
```

屏幕会给出 `recurrences=3`，最后一条 `escalated` 里有 `prior_cycles` 引用链。计数键是
`(detector, subject, action)`，计的是「修好后又复发」周期；动作执行次数和未修好的失败不混进来。

### 这一刻该说哪句话

> 前三次都真的修好了，第四次系统拒绝再做同一个动作。决定来自追加式时间线的确定性聚合，
> 没有 embedding，也没有模型判断。`prior_cycles` 明确列出是哪三次历史让这次动作停下。

> **不许说"系统越用越聪明"**。它学会的是何时停手，方向是更保守。

> 这是记忆改变动作的唯一一处。普通事故记忆只调整先查哪些探针，不能跳过新鲜证据、前置校验
> 或影响面测量。复发账本只增加一个拒绝理由，也没有扩张动作集合。

演完立刻收回时间压缩：

```bash
./scripts/inject_incident.sh cleanup
./scripts/inject_incident.sh status
```

`status` 必须显示「演示覆盖：未安装」。

---

## 第五幕 · 消融评测台，主对比盯 A2（2 分钟）

### 敲什么命令

```bash
python3 - <<'PY'
import json
from core.eval.memory_ablation import DEFAULT_ARMS, run_ablation

report = run_ablation().to_dict()
primary = report["design"]["primary_baseline"]

print(report["power_statement"])
print("arms=")
for arm in DEFAULT_ARMS:
    print(" ", arm.name, arm.mode, arm.description)
print(
    "design=",
    {
        "cases": report["design"]["cases"],
        "repeats": report["design"]["repeats"],
        "paired_instances": report["design"]["paired_instances"],
        "primary_baseline": primary,
        "llm_calls": report["design"]["llm_calls"],
    },
)
balance = report["design"]["sham_memory_balance"]
print(
    "A2_balance=",
    {
        "all_retrieved_counts_equal": balance["all_retrieved_counts_equal"],
        "all_within_10_percent": balance["all_within_10_percent"],
        "max_relative_difference": balance["max_relative_difference"],
    },
)
print(
    "M1_vs_A2=",
    json.dumps(report["metrics"]["M1"]["comparisons"][primary], ensure_ascii=False),
)
print(
    "A2_vs_A1=",
    json.dumps(report["metrics"]["A2_vs_A1_negative_control"], ensure_ascii=False),
)
PY
```

### 屏幕上会出现什么

一条记忆臂加四条基线臂：

```text
M_memory          经核实后更新的记忆
A0_empty          不执行
A1_no_memory      同一推理器和工具，关闭记忆
A2_sham_memory    等条数、等 token、同格式、语义无关的历史
A3_static_runbook 固定且不更新的人工 runbook
```

默认命令现场会给 5 个案例、每个重复 3 次，共 15 个配对实例，其中 ACT 主任务 10 对。
当前确定性只读评测的 `M1_vs_A2` 是双方 10/10，配对差值 0，McNemar mid-p 为 1。
A2 与记忆臂的检索条数全部相等，注入的每条记忆行按同一个 token 计数器配平；完整上下文差异全部
在 10% 内。每次运行都以命令实际输出为准。

### 这一刻该说哪句话

> 我证明记忆有用时，主对比用 M 对 A2。A1 关掉记忆后提示词会变短，M 对 A1 会把语义信息和
> 上下文体积混在一起。A2 取同样条数、同样 token 量、同样格式的历史，只把内容换成经过重叠阈值
> 过滤的无关语义。M 赢 A2，才支持「相关记忆有用」；A2 对 A1 单独估计纯上下文体积效应。

> 当前结果就是 0。两边在 10 个 ACT 配对上都核实通过，记忆没有提高正确率。这个 0 有意义：
> 它挡住了把确定性小样本的满分结果包装成记忆增益。当前案例允许同一规则推理器跑完整只读探针，
> 记忆主要重排先后顺序，最后答案容易撞到同一个天花板。

> 功效声明：在记忆臂赢 20%、输 5%（OR=4）的合理不一致分布下，n=40 功效约 38%，n=60 约 65%；
> 本评测规模检测不出 10pp 以下的效应。默认命令实际只有 10 个 ACT 配对，证据强度更低。测出 0
> 约束了当前基准下的主张，不能推出真实效应精确等于 0。

---

## 能力边界 · 这四句主动说（1 分钟）

先用当前源码的能力声明验一次，避免拿旧结论上台：

```bash
python3 - <<'PY'
from core.evolve.observatory import CAPABILITY_STATUS
from core.orchestrator.evolving_service import memory_retention_wiring
from frontend.gateway.app.main import _PRODUCTION_MEMORY_BUDGET

print("capability_status=", CAPABILITY_STATUS)
print(
    "gateway_retention=",
    memory_retention_wiring(memory_budget=_PRODUCTION_MEMORY_BUDGET),
)
print("gateway_budget=", _PRODUCTION_MEMORY_BUDGET)
PY
```

当前声明中，衰减和容量淘汰都是 `implemented=true`、`production_wired=true`。网关实例把容量预算
配置为 64，所以淘汰路径可触发；衰减在核实后写回事务里按 86,400 秒检查点运行。反证隔离的
当前声明是 `implemented=true`、`production_wired=false`，台上只说「已经实现，尚未接入生产」。根因变更的
`SUPERSEDE` 是另一条已接入能力，它和「两次新鲜反证后隔离」不能混称。

接着原样说：

> **不许说"系统越用越聪明"**。它学会的是何时停手，方向是更保守。

> **记忆可以回答"先看哪里"和"以前这样过吗"，不可以回答"现在能不能动手"。**
> 影响面每次现测，绝不缓存：AWS 2025-10 全球故障的机理正是自动化用了一份过期计划。

> 事实鲜度只影响排查顺序。动作前置校验、影响面、动作后回读和观察期都读取当前环境，历史证据
> 只能解释为什么先查某处。

> 反证隔离已经实现，按当前能力声明还没有进入生产回路，我不拿测试和代码分支冒充线上能力。

最后把离线观测舱和在线记忆分开，数字也现场验：

```bash
python3 - <<'PY'
from frontend.gateway.app.rca_reader import load_evolution

payload = load_evolution(None, 4)
observatory = payload.get("observatory") or {}
print("dataMode=offline_benchmark_replay")
print("passes=", payload.get("passes"))
print("lifecycle_events=", len(observatory.get("events") or []))
print("records=", len(observatory.get("records") or []))
PY
```

> 观测舱那 185 次生命周期事件是**离线基准重放**，和线上记忆无关，两者绝不能混着讲。
> 在线证据只看 `/api/rca/memory`；离线接口会明确标成 `offline_benchmark_replay`。

---

## 三个问题的短答

**Q1「给我看看它现在记住了什么」**

> 我在故障注入前就打开 `/api/rca/memory`。顶部看是否持久化、各层数量、容量和保留状态；列表看
> 正文、时间和来源轨迹数量；点具体 `memory_id` 的详情接口看来源轨迹、关系和证据索引。5 条 seed
> 是人工先验，核实后新增的事故记录才是在线学习。

**Q2「一个事实以前是真的，现在不是了，会怎样」**

> 同一 `(subject, relation)` 的新值追加后，旧值写 `valid_to`，记录继续保留。当前查询只见新值，
> `retrieve(as_of=过去时刻)` 仍能重建旧信念。这个边界表达的是世界变化。

**Q3「你怎么知道记忆有用，而不是提示词变长了」**

> 主对比是 M 对 A2 假记忆臂。A2 保持条数、token 和格式，只换成语义无关历史。当前主指标增益
> 是 0，我会直接报 0；同时主动报 n=40 时约 38% 的 McNemar 功效和 10pp 以下效应检测不出来的
> 边界。

---

## 出问题时

| 现象 | 当场判断 | 处理 |
|---|---|---|
| `/api/rca/memory` 连不上 | 网关未启动 | `systemctl restart netops-ops-console-backend`，再查 `/api/healthz` |
| `durable=false` | 当前只有进程内记忆 | 停止第二幕，不做持续学习声明 |
| 注入前已有 `root:sentinel.failed_units` | 这次会复用或更新旧记录 | 换干净演示库；保留原库，不现场删生产记忆 |
| 事故 `resolved` 后接口没变化 | 持久化写回失败或哨兵未接到共享库 | 查 `journalctl -u netops-ops-console-backend -n 80`，并重新验 `durableMemory=true` |
| 第四幕第一次就升级 | 时间线留有旧复发周期 | 按第四幕开头清演示时间线并重启网关 |
| 第四幕没升级 | 时间压缩未进入网关进程 | `./scripts/inject_incident.sh status`，看窗口、阈值、冷却和巡检实际值 |
| 消融仍然是 0 | 当前基准触及确定性只读路径的天花板 | 如实报 0、配对表和功效边界，不换主对比 |
| 看到 185 次生命周期事件 | 这是离线重放结果 | 回到 `/api/rca/memory` 展示在线库 |

收尾只需要：

```bash
./scripts/inject_incident.sh cleanup
./scripts/inject_incident.sh status
```
