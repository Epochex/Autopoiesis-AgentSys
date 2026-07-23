# 独立评审智能体：证据约束配对评测

## 定位与边界

`core.eval.llm_grounding_judge` 是 Autopoiesis 的离线评测链路，不进入在线诊断主路径。它让一个与被评系统解耦的模型读取“逐条结论—引用证据原文”，给出严格结构化的语义支持判定，并比较同一留出案例上的两个候选输出：

- `full`：开启证据约束、引用核验、契约核验、语义评审和拒答门控；
- `baseline`：关闭上述约束，用来回答“这些架构机制相对无约束输出是否减少了无依据结论”。

这属于 **LLM-as-judge（模型评审）**。它可以替代大规模逐条人工标注，形成可复现的代理指标，但不是人工金标，也不能消除评审模型自身的偏差。未进行人工抽样校准时，报告只能写“模型评审判定的无依据断言率”，不能写成绝对幻觉率或人工准确率。

## 输入契约

每个 `PairedJudgeCase` 必须满足：

1. `split` 固定为 `heldout`，避免拿训练案例自证；
2. 明确 `expected_answerable`；
3. 每条证据都包含 `evidence_id`、来源和未经摘要替换的 `raw_text`；
4. 每个候选输出显式记录是否拒答，并将回答拆成带 `claim_id` 的逐条结论；
5. 每条引用必须能解析到本案例的证据原文，未知引用在调用评审模型前直接报错；
6. 同一案例必须恰好包含完整架构和基线两个输出，并携带各自的运行版本指纹。

评审模型看不到“完整架构/基线”的开关标签，只看到待评结论和证据，避免按实验组名称迎合预期。

## 缺证据负例与正确拒答率

正确拒答率不能在全可回答样本上计算。评测套件强制至少包含一个 `withheld_key_evidence` 负例：从真实留出案例中移除或遮蔽决定性证据，将 `expected_answerable` 设为 `false`，然后让完整架构与基线分别在遮蔽后的输入上重新运行。

使用 `build_withheld_evidence_negative` 时必须传入 `outputs_from_masked_run`。函数不会复用原可回答案例的输出；如果遮蔽证据仍通过引用泄漏到候选输出，模型校验会拒绝该案例。

## 评审输出与共识

评审响应必须满足 `JudgeResponse/1`，且对每个输入 `claim_id` 恰好返回一次：

- `supported`：引用原文直接支持结论；
- `unsupported`：引用与结论矛盾，或明确不能支持结论；
- `insufficient`：给出的引用不足以推出结论。

默认对每个候选输出独立评审三次，三次调用分别缓存。达到 `minimum_agreement` 后采用多数结论；平票或未达到阈值时按失败关闭，最终记为 `insufficient` 并设置 `disputed=true`。调用失败、JSON 不合法、结论遗漏、重复或越权增加结论时，整次评测抛出 `JudgeRunError`，不生成部分指标。

## 指标口径

- **语义引用准确率**：被评为 `supported` 的已引用结论数 ÷ 全部已引用结论数。无引用结论不进入这个分母，但会进入无依据断言率。
- **无依据断言率**：`unsupported + insufficient` ÷ 全部输出结论数。该口径保守地把“证据不足以推出”也视为未被证据支撑，同时单独输出 `explicit_unsupported_rate` 区分明确矛盾。
- **正确拒答率**：在 `expected_answerable=false` 的缺证据负例中，结构化 `refused=true` 的案例数 ÷ 缺证据负例数。
- **拒答决策准确率**：可回答时回答、不可回答时拒答的案例占比。
- **错误拒答率**：在可回答案例中错误拒答的比例。

配对报告给出完整架构相对基线的语义引用准确率增益、无依据断言率降幅、正确拒答率增益和拒答决策准确率增益。评测器只评分调用方提供的真实候选输出，不自行生成或伪造基线数字。

## DeepSeek Pro 接入

评审后端只依赖 `JudgeBackend` 协议。生产评测可将单独配置的 DeepSeek Pro/OpenAI 兼容客户端注入 `LLMJsonJudgeBackend`：

```python
from core.eval import LLMJsonJudgeBackend

judge = LLMJsonJudgeBackend(
    separately_configured_deepseek_client,
    provider_id="deepseek",
    model_id="deepseek-pro",
)
```

密钥由组合根中的客户端持有。本模块不读取 `.env`，不会把密钥写入请求、缓存、版本指纹或评测报告。自动化测试使用确定性假实现，不访问外部模型。

## 缓存与版本指纹

`FileJudgeCache` 以评审请求内容、评审模型/提示词/响应模式版本、评测器版本和重复轮次共同计算 SHA-256 键。缓存只保存结构化响应和无敏感信息的评审指纹，使用临时文件刷盘后原子替换。缓存损坏不会回退为未校验结果，而是失败关闭。

最终报告同时保留：

- 评审模型、提示词、响应模式和评测器版本指纹；
- 被评输出的系统版本指纹与架构开关；
- 每轮判定、共识率和分歧标记；
- 明确的 `annotation_type=llm_as_judge`、`is_human_gold=false` 边界。

## 最小调用流程

```python
from core.eval import FileJudgeCache, run_paired_llm_judge, write_paired_judge_report

report = run_paired_llm_judge(
    heldout_cases,
    judge,
    full_variant_id="full",
    baseline_variant_id="baseline",
    cache=FileJudgeCache("artifacts/judge-cache"),
    repeats=3,
)
write_paired_judge_report(report, "artifacts/paired-grounding-report.json")
```

只有在 `heldout_cases` 确实包含两个版本的实际运行输出时，报告中的增益数字才有意义。
