# 演示稿 · 记忆从哪来、何时作废、何时让系统停手

第一幕直接在长轨迹页回答「这台机器现在记住了什么」，第二幕让一次真实故障进入在线记忆，
第三幕演事实变更和时间旅行，第四幕演复发升级，最后一幕用消融回答「记忆到底有没有用」。

时间紧就演第一、二、三、五幕。第四幕约 9 分钟，只有需要证明记忆会改变动作时再跑。

浏览器主路径只有一个：

```text
http://192.168.1.27:2026 → 长轨迹
```

所有 JSON 端点都放在「他要是不信，当场跑这条」的位置。现场先指界面，再给接口原值。

---

## 开演前 · 三个条件不满足就停（1 分钟）

### 开演前验什么

```bash
cd /data/Autopoiesis-AgentSys

python3 - <<'PY'
import json
from urllib.request import urlopen

health = json.load(urlopen("http://127.0.0.1:8026/api/healthz", timeout=5))
memory = json.load(urlopen(
    "http://127.0.0.1:8026/api/rca/memory?include_quarantined=true&limit=1000",
    timeout=5,
))
print("status=", health["status"])
print("durableMemory=", health.get("durableMemory"))
print("memory.durable=", memory["durable"])
print("counts=", memory["counts"])
print("budget=", memory["budget"])
print("retention=", memory["retention"])
assert health["status"] == "ok", health
assert health.get("durableMemory") is True, "没有持久化记忆，哨兵事故不会写入在线库"
assert memory["durable"] is True, "线上记忆页当前只能拿到进程内数据"
PY

./scripts/inject_incident.sh status

gateway_pid=$(systemctl show autopoiesis-gateway -p MainPID --value)
tr '\0' '\n' < "/proc/${gateway_pid}/environ" | grep '^AUTOPOIESIS_SENTINEL=1$'
```

第一段必须显示 `status=ok`、两个持久化字段都是 `True`；容量、活跃数、隔离数和保留状态都以
这次输出为准。最后一行必须打印 `AUTOPOIESIS_SENTINEL=1`。

浏览器打开首页，切到「长轨迹」，确认从上到下能找到：

1. 实时态势面板。
2. 「记忆观测舱」，抬头有「离线基准重放 · 留出集 6 案例 × 4 轮 · 记忆从空开始」。
3. 横幅第二行「这不是线上记忆 → 看线上记忆」，链接可点。
4. 链接落到标题「线上记忆 · 这台机器现在真的记得什么」。

专用演示机如果留有上一轮复发时间线，开演前重置演示事故和时间线：

```bash
./scripts/inject_incident.sh cleanup
rm -f /data/autopoiesis-production/sentinel-timeline.jsonl
systemctl restart autopoiesis-gateway
```

时间线是审计日志，这三行只用于开演前重置专用演示环境。`cleanup` 不会清在线记忆。

### 这一刻该说哪句话

> 我先把证明条件说清楚。下面看的必须是持久化在线记忆；进程内列表重启就丢，不能拿来证明
> 持续学习。页面上的每个数量都来自这台机器当前的持久化库，我不背固定数字，现场读取。

---

## 第一幕 · 在界面上打开线上记忆（1 分钟）

### 在界面上指给他看

浏览器进入「长轨迹」。先停在「记忆观测舱」抬头，逐字指出数据源横幅：

```text
离线基准重放 · 留出集 6 案例 × 4 轮 · 记忆从空开始
这不是线上记忆 → 看线上记忆
```

点「看线上记忆」，页面跳到 `#live-memory`。这一屏从左到右看：

- 抬头的「活跃」「隔离」「BM25 可检索文档」是当前接口原值，现场念页面数字。
- 左栏「活跃」按语义、程序、情景、资产画像四层分组，卡片直接给正文、置信、重要度和强度。
- 中栏「隔离」按真实原因分组，每组直接展开。
- 点左栏或中栏任一条，右栏「线上记忆 · 逐字段审计」给全文、`VALID_FROM`、`VALID_TO`、
  首次和最近观测、标签、资产、来源轨迹、证据、链接、关系与隔离原因。

领域先验的来源可以在右栏看 `seed` 标签。仓库知识文件当前固定定义 5 条，线上库是否已经装入，
以左栏实际记录和右栏标签为准。总数不能证明来源，逐条看来源轨迹和标签。

### 这一刻该说哪句话

> 你问它现在记住了什么，我就滚到「线上记忆 · 这台机器现在真的记得什么」。左边是当前可检索
> 的活跃记忆，中间是已经撤回的记录，右边审计所选记录的字段和来源。页面数字来自持久化在线库。
> 带 `seed` 的是人工领域先验；核实后写入、带来源轨迹的事故记录才是在线吸收的经验。

### 隔离这一栏怎么讲

先念中栏当前的隔离总数，再念三个分组标题。数量全部按页面显示，理由保留原文：

```text
BENCHMARK REPLAY ARTIFACT: THE OFFLINE HARNESS RAN AGAINST THE LIVE STORE
FORGOTTEN
VERIFICATION PROBE, NOT A REAL MEMORY
```

三组对应这台机器真实发生过的三类事故：

1. 离线基准曾连接在线库。四轮重放写入在线库，也读取哨兵记录，造成写污染和读污染；每轮末尾还在
   在线记忆上执行衰减节拍，把哨兵学到的记录全部隔离。修复后，离线流显式禁用环境中的在线库连接，
   页面横幅和 `/api/rca/evolution` 也明确标记离线重放。
2. 衰减默认参数是 `retention=0.55`、`floor=0.4`，空闲两拍后强度低于地板。生产保留路径现已使用
   `forget=False`：强度仍可下降并影响排序，年龄不再触发退休。年龄不足以退休一条运维事实；事实变更
   走撤销，容量压力走有理由的淘汰。
3. 装库时留下了一条验证探针。它同时暴露初始化判断曾使用 `records()`：一条已隔离探针也会让库看起来
   有内容，5 条领域先验因此一直没有进入在线库。判断现已改为 `active()`。

这一屏的准确定位是「可审计的事故记录」。数据库审计事件流由触发器强制只追加；一条记忆写入后，
系统没有删除路径，唯一撤回路径是隔离，并把原因追加到完整记录。页面仍展示正文、时间和来源，活跃
检索同时排除它。

> 这三组是这台机器踩过的坑、造成的污染和对应修法。记录留在这里，证明撤回发生过，也允许别人
> 复查原因。一个愿意展示活跃记忆、撤回记录和修复证据的系统，可信度高于只展示活跃结果的系统。

### 他要是不信，当场跑这条

先核对页面总数与三个隔离分组：

```bash
curl -fsS 'http://127.0.0.1:8026/api/rca/memory?include_quarantined=true&limit=1000' |
python3 -c '
import json, sys
p = json.load(sys.stdin)
reasons = {}
for row in p["records"]:
    if not row["quarantined"]:
        continue
    tags = [tag for tag in row["tags"] if tag.startswith("quarantine:")]
    reason = tags[-1].removeprefix("quarantine:") if tags else "NO REASON RECORDED"
    reasons[reason] = reasons.get(reason, 0) + 1
print("durable=", p["durable"])
print("counts=", p["counts"])
print("budget=", p["budget"])
print("retention=", p["retention"])
for reason, count in reasons.items():
    print(count, reason)
'
```

选中一条后，把页面上的 ID 代入。详情证明右栏字段来自哪里，影响接口回答这条记忆参与过哪些决定；
没有影响时必须返回空数组，不能编：

```bash
memory_id='把页面上的 memory_id 粘到这里'
curl -fsS "http://127.0.0.1:8026/api/rca/memory/${memory_id}" | python3 -m json.tool
curl -fsS "http://127.0.0.1:8026/api/rca/memory/${memory_id}/influence" | python3 -m json.tool
```

被追问检索排序时再验权重：

```bash
python3 - <<'PY'
from core.memory import store

print("asset_hit=", store._W_ASSET_HIT)
print("entity_hit=", store._W_ENTITY_HIT)
print("dense_route_coef=", store._DENSE_ROUTE_COEF)
print("structural_rerank_coef=", store._STRUCT_COEF)
PY
```

> 第一段是查询相关分：全文和标签走 BM25，精确资产和精确运维对象各加 2。可选向量召回按最强精确
> 或词法分的 0.35 倍缩放。生命周期结构分只在最后做有界重排，上限按最高基础分的 0.15 缩放，
> 所以它能调整近似并列，压不过明确的词法或资产命中。

---

## 第二幕 · 注入一次真故障，在线记忆吸收这次事故（约 3 分钟）

### 怎么演

浏览器先停在「线上记忆」，记下抬头的活跃数和相关记录的「最近观测」。另开终端运行七步真机验证：

```bash
cd /data/Autopoiesis-AgentSys
./scripts/verify_memory_loop.sh
```

脚本实测通过，依次验证：前置条件、冻结基线、真实故障注入、持久化吸收、ASCII 检索、幂等、
详情溯源和清理。脚本运行期间回到浏览器最上方的实时态势面板，指着同一事故从发现走到核实恢复；脚本结束
后刷新长轨迹页，再点「看线上记忆」。

这次事故可能新建记录，也可能被高相似度路由吸收到已有记录。页面上按真实结果讲：

- 新建时，左栏出现与 `demo-collector` 相关的卡片，抬头活跃数按实际差值变化。
- 吸收时，原卡片的最近观测、来源轨迹、访问或强度等字段发生真实更新，不能把正常合并说成没写入。
- 右栏必须能看到非空来源轨迹，并能审计标签、资产和证据索引。

### 这一刻该说哪句话

> 这一步是「持续学习」唯一的实证。刚才冻结了库的基线，现在这次真实故障已经新建或更新了在线
> 记录，右侧能追到本次哨兵链的轨迹和证据。故障、处置、90 秒观察期和回读都真实发生；只有核实
> 通过的链能进入成功经验路径。页面动画、离线重放和一段更长的提示词都不能替代持久化写入证据。

> 这里的记忆先影响排查顺序，帮助回答「先看哪里」和「以前这样过吗」。它没有取得动作授权，
> 后面的复发账本才会直接拦住一个动作。

### 他要是不信，当场跑这条

页面展示是主路径。对方要求原始数据时，先用列表找到相关记录，再查详情和影响：

```bash
curl -fsS 'http://127.0.0.1:8026/api/rca/memory?include_quarantined=true&limit=1000' |
python3 -c '
import json, sys
p = json.load(sys.stdin)
print("counts=", p["counts"], "budget=", p["budget"])
for row in p["records"]:
    haystack = " ".join([row["text"], *row["tags"], *row["asset_ids"]]).lower()
    if "demo-collector" in haystack:
        print(row["tier"], row["memory_id"], row["last_observed_at"], row["source_trace_ids"])
'
```

哨兵未自动巡检时才手动推进。这个 POST 是触发一次真实轮询的备用动作：

```bash
curl -fsS -X POST http://127.0.0.1:8026/api/rca/sentinel/poll | python3 -m json.tool
```

---

## 第三幕 · 同一个事实变了，旧值撤销但不删除（2 分钟）

当前环境事实采集器尚未接入在线调用点。本幕的证据范围是按 `(subject, relation)` 键撤销与
`retrieve(as_of=...)` 历史重建；在线哨兵的当前生产路径没有这类事实采集调用。

### 在界面上指给他看

滚到「线上记忆」，点任一记录，在右侧「当前状态」直接指出并排的 `VALID_FROM` 和 `VALID_TO`。
这两个字段是在线详情接口原值。`VALID_TO=∅` 表示世界有效期尚未写入结束时间；当前检索还会同时
排除已隔离记录。有结束时间的记录保留在审计历史中，检索按有效区间过滤。

界面负责展示真实在线记录的有效区间。下面的本地例子在新建内存库上运行，用来证明撤销算法和
`as_of` 查询；在线库保持只读：

```bash
python3 - <<'PY'
from datetime import datetime, timezone
from core.evolve.memory_ops import observe_fact
from core.memory.store import TieredMemoryStore

store = TieredMemoryStore()
t0 = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
t1 = datetime(2026, 8, 22, 10, 5, tzinfo=timezone.utc)

old_id = observe_fact(
    store, subject="eth2", relation="link_state", value="up", observed_at=t0,
)
new_id = observe_fact(
    store, subject="eth2", relation="link_state", value="down", observed_at=t1,
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

输出必须是：

```text
eth2 link_state up   valid_from=2026-08-22T10:00:00+00:00 valid_to=2026-08-22T10:05:00+00:00
eth2 link_state down valid_from=2026-08-22T10:05:00+00:00 valid_to=None
10:00 ['eth2 link_state up']
10:05 ['eth2 link_state down']
records=2 old_kept=True new_kept=True
```

核心键是同一个 `(subject, relation)`。新值追加后，旧值写入 `valid_to`；有效区间是
`[valid_from, valid_to)`。查询过去时刻只能看到旧值，查询变更时刻只能看到新值。

### 这一刻该说哪句话

> 一个事实以前是真的，现在变了，系统保留两条记录。右侧能看到旧记录的 `VALID_TO` 和新记录的
> `VALID_FROM`。当前检索只使用新值，`retrieve(as_of=过去时刻)` 仍能重建当时的信念。这里表达的
> 是世界发生变化；同一世界里的证据纠错走另一套修正语义。

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

### 怎么演

```bash
./scripts/inject_incident.sh cleanup
rm -f /data/autopoiesis-production/sentinel-timeline.jsonl
systemctl restart autopoiesis-gateway
./scripts/inject_incident.sh status
./scripts/inject_incident.sh recurring
```

脚本实测通过。它临时把 24 小时窗口压到 1 小时，把 600 秒基础冷却压到 30 秒，拒绝阈值保持 3。
`status` 展示网关进程实际读到的值，不能用配置文件猜进程状态。

终端只负责驱动四幕。观众始终看浏览器最上方的实时态势面板：前三次完整显示

```text
发现 → 二次确认 → 前置校验 → 执行 → 观察期 → resolved
```

第四次显示：

```text
发现 → 二次确认 → escalated，拒绝执行，转人工
```

态势页会把事故标成红色「要人工」。剧场事故卡显示窗口内复发次数，并逐条列出此前何时修好又复发。
所有数量按页面当时值讲。

### 这一刻该说哪句话

> 前三次都真的修好了，第四次系统拒绝再做同一个动作。决定来自追加式时间线的确定性聚合，
> 没有 embedding，也没有模型判断。页面里的 `prior_cycles` 明确列出哪些历史让这次动作停下。

> **不许说"系统越用越聪明"**。它学会的是何时停手，方向是更保守。

> 这是记忆改变动作的唯一一处。普通事故记忆只调整先查哪些探针，不能跳过新鲜证据、前置校验
> 或影响面测量。复发账本只增加一个拒绝理由，也没有扩张动作集合。

### 他要是不信，当场跑这条

```bash
curl -fsS http://127.0.0.1:8026/api/rca/sentinel/recurrence | python3 -m json.tool
```

端点按 `(detector, subject, action)` 聚合，计的是「修好后又复发」周期；动作执行次数和未修好的失败
不会混进计数。

演完立刻收回时间压缩：

```bash
./scripts/inject_incident.sh cleanup
./scripts/inject_incident.sh status
```

`status` 必须显示「演示覆盖：未安装」。

---

## 第五幕 · 消融评测台，主对比盯 A2（2 分钟）

### 跑评测台

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
print("design=", {
    "cases": report["design"]["cases"],
    "repeats": report["design"]["repeats"],
    "paired_instances": report["design"]["paired_instances"],
    "primary_baseline": primary,
    "llm_calls": report["design"]["llm_calls"],
})
balance = report["design"]["sham_memory_balance"]
print("A2_balance=", {
    "all_retrieved_counts_equal": balance["all_retrieved_counts_equal"],
    "all_within_10_percent": balance["all_within_10_percent"],
    "max_relative_difference": balance["max_relative_difference"],
})
print("M1_vs_A2=", json.dumps(
    report["metrics"]["M1"]["comparisons"][primary], ensure_ascii=False,
))
print("A2_vs_A1=", json.dumps(
    report["metrics"]["A2_vs_A1_negative_control"], ensure_ascii=False,
))
PY
```

一条记忆臂加四条基线臂：

```text
M_memory          经核实后更新的记忆
A0_empty          不执行
A1_no_memory      同一推理器和工具，关闭记忆
A2_sham_memory    等条数、等 token、同格式、语义无关的历史
A3_static_runbook 固定且不更新的人工 runbook
```

现场先看 `design.primary_baseline`，它必须是 `A2_sham_memory`；再看 A2 配平、`M1_vs_A2` 完整配对表
和 `A2_vs_A1` 负对照。案例数、重复数、配对数、差值和 p 值全部念这次命令的输出。

### 这一刻该说哪句话

> 我证明记忆有用时，主对比用 M 对 A2。A1 关掉记忆后提示词会变短，M 对 A1 会把语义信息和
> 上下文体积混在一起。A2 取同样条数、同样 token 量、同样格式的历史，只把内容换成经过重叠阈值
> 过滤的无关语义。M 赢 A2，才支持「相关记忆有用」；A2 对 A1 单独估计纯上下文体积效应。

> 当前命令如果仍给配对差值 0，就直接报 0。这个结果约束当前基准下的主张：在确定性只读路径上，
> 两臂都可能触及天花板，记忆主要重排探针顺序，最终答案会收敛到同一结果。

> 功效声明：在记忆臂赢 20%、输 5%（OR=4）的合理不一致分布下，n=40 功效约 38%，n=60 约 65%；
> 本评测规模检测不出 10pp 以下的效应。默认命令的 ACT 配对数以现场输出为准；小样本下测出 0
> 约束当前基准范围，真实效应仍需更大样本估计。

---

## 能力边界 · 这四句主动说（1 分钟）

先用当前源码的能力声明验一次，避免拿旧结论上台：

```bash
python3 - <<'PY'
from core.evolve.observatory import CAPABILITY_STATUS
from core.orchestrator.evolving_service import memory_retention_wiring
from frontend.gateway.app.main import _PRODUCTION_MEMORY_BUDGET

print("capability_status=", CAPABILITY_STATUS)
print("gateway_retention=", memory_retention_wiring(
    memory_budget=_PRODUCTION_MEMORY_BUDGET,
))
print("gateway_budget=", _PRODUCTION_MEMORY_BUDGET)
PY
```

当前声明中，衰减和容量淘汰都是 `implemented=true`、`production_wired=true`。网关的容量预算以命令
输出为准；衰减在核实后写回事务里按 86,400 秒检查点运行，生产路径使用 `forget=False`。反证隔离的
当前声明是 `implemented=true`、`production_wired=false`，台上只说「已经实现，尚未接入生产」。根因
变更的 `SUPERSEDE` 是另一条已接入能力，它和「两次新鲜反证后隔离」不能混称。

接着原样说：

> **不许说"系统越用越聪明"**。它学会的是何时停手，方向是更保守。

> **记忆可以回答"先看哪里"和"以前这样过吗"，不可以回答"现在能不能动手"。**
> 影响面每次现测，绝不缓存：AWS 2025-10 全球故障的机理正是自动化用了一份过期计划。

> 事实鲜度只影响排查顺序。动作前置校验、影响面、动作后回读和观察期都读取当前环境，历史证据
> 只能解释为什么先查某处。

> 反证隔离已经实现，按当前能力声明还没有进入生产回路。当前生产口径只包括已经接入的能力。

最后回到「记忆观测舱」抬头，直接指横幅：

> 观测舱明确写着「离线基准重放」「留出集 6 案例 × 4 轮」「记忆从空开始」和「这不是线上记忆」。
> 观测舱里的生命周期事件和记录只属于这次离线重放。线上证据只看下面的「线上记忆」；两者绝不能
> 混着讲。

他要是不信数据模式，当场跑：

```bash
curl -fsS http://127.0.0.1:8026/api/rca/evolution |
python3 -c 'import json,sys; p=json.load(sys.stdin); print({
    "dataMode": p.get("dataMode"),
    "onlineMemory": p.get("onlineMemory"),
    "benchmark": p.get("benchmark"),
})'
```

必须看到 `dataMode=offline_benchmark_replay` 和 `onlineMemory=false`。

---

## 三个问题的短答

**Q1「给我看看它现在记住了什么」**

> 我现在滚到「线上记忆 · 这台机器现在真的记得什么」。左边按层展示当前可检索记录，中间按原因
> 展示已撤回记录，点任一条后右边给完整字段和来源审计。页面抬头的活跃、隔离和可检索数量就是
> 这台机器当前值；需要原始 JSON 时，我再查列表、详情和 influence 端点。

**Q2「一个事实以前是真的，现在不是了，会怎样」**

> 我先在右侧指出 `VALID_FROM` 和 `VALID_TO`。系统按 `(subject, relation)` 键撤销：新值追加，旧值
> 写结束时间并保留。当前检索只用新值，`retrieve(as_of=过去时刻)` 能重建旧信念。界面给在线记录的
> 双时间字段，本地例子现场证明时间旅行查询。

**Q3「你怎么知道记忆有用，而不是提示词变长了」**

> 我看消融评测台的 M 对 A2 假记忆臂。A2 保持条数、token 和格式，只换成语义无关历史；M 对 A2
> 是主对比，A2 对 A1 只估计上下文体积效应。结果是多少就报多少，并主动报功效声明：n=40 约 38%，
> n=60 约 65%，当前规模检测不出 10pp 以下效应。

---

## 现场答问端点

这些端点只用于核对页面和回答追问：

```text
GET /api/rca/memory
GET /api/rca/memory/{id}
GET /api/rca/memory/{id}/influence
GET /api/rca/sentinel/recurrence
GET /api/rca/evolution
```

`/memory/{id}/influence` 回答「这条记忆参与过哪些决定」；空数组表示没有可归因影响。
`/evolution` 必须返回 `dataMode: offline_benchmark_replay`。

---

## 出问题时

| 现象 | 当场判断 | 处理 |
|---|---|---|
| 「线上记忆」整屏读取失败 | 网关或页面代理不可用 | 查 `/api/healthz`，必要时重启 `autopoiesis-gateway` |
| 页面显示「当前没有持久化线上记忆」 | 接口返回 `durable=false` | 停止第二幕，不做持续学习声明 |
| 观测舱没有数据源横幅或链接不跳转 | 前端版本不对 | 停止演示，确认部署版本含 `#live-memory` |
| 事故 `resolved` 后页面没变化 | 页面未重载、持久化写回失败或哨兵未接共享库 | 先刷新页面，再查七步脚本输出和网关日志 |
| 七步脚本报「没有新建」 | 先看是否吸收进已有记录 | 以脚本的新增或更新判据为准，不能要求复发次次复制 |
| 第四幕第一次就升级 | 时间线留有旧复发周期 | 按第四幕开头清演示时间线并重启网关 |
| 第四幕没升级 | 时间压缩未进入网关进程 | 跑 `./scripts/inject_incident.sh status`，看实际窗口、阈值、冷却和巡检值 |
| 消融主指标仍为 0 | 当前基准触及确定性只读路径天花板 | 如实报 0、配对表和功效边界，不换主对比 |
| 把观测舱数字当成线上库 | 数据源边界讲错 | 指横幅，再点「看线上记忆」回到在线库 |

收尾只需要：

```bash
./scripts/inject_incident.sh cleanup
./scripts/inject_incident.sh status
```
