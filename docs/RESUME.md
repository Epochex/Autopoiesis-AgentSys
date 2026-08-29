# Autopoiesis-AgentSys 项目叙事草稿

公开简历以 `/data/cv_jianke/AgentDev_v5.tex` 为主，本文件只记录能够由当前代码和部署状态支撑的项目事实，不保存固定小样本分数。

## 中文

面向混合设备私有网络构建持续事件调查系统。采集端把安全事件和网络事实写入 Redpanda 与 ClickHouse，检测结果聚合为可持久化案件；调查服务在同一案件内联合召回历史故障档案、资产画像、执行记忆和厂商知识，并调用主机、ClickHouse 与 FortiGate 只读工具补充现场证据。每轮检索、探针、引用、假设修正和人工处置均写回案件时间线，会话快照支持服务重启后的继续调查。

- 统一检索来自执行记忆、历史事件档案、风险模式、网络画像和知识文档的候选，保存来源、定位符、分数组成、匹配字段及是否进入模型上下文。
- 将实时告警和关联建议按稳定来源标识合并为案件，避免轮询和重复投递产生重复工单，并把交互调查绑定到同一案件标识。
- 对模型请求的命令执行只读白名单校验，对证据引用执行存在性校验；缺失现场证据时继续采集或保留未决结论。
- 通过 FortiGate Cookie 认证读取接口、设备、策略和配置变更元数据；凭据只存在于服务环境，调查证据不包含账号与密码。
- 业务评测采用跨时间案件轨迹，比较完整系统、关闭记忆、等量无关历史和固定操作手册，测量稳定根因、反证修正、重启恢复、重复探针、人工升级与动作回读。

## English

Built a persistent incident-investigation system for heterogeneous private networks. The ingestion path writes security events and network facts to Redpanda and ClickHouse, correlated detections become durable cases, and each investigation recalls operational history, indexed memory, asset context, and vendor documentation before collecting fresh read-only evidence from hosts, ClickHouse, and FortiGate. Retrieval receipts, probes, citations, hypothesis revisions, and operator dispositions remain attached to one case, while durable session snapshots allow an investigation to continue after a service restart.

- Unified retrieval across indexed memory, incident dossiers, risk patterns, network features, and reference documents with source locators, component scores, matched fields, and context-selection receipts.
- Merged repeated alert deliveries and later correlation suggestions into stable incident cases using source-level idempotency.
- Enforced a read-only command allowlist and verified that cited evidence identifiers exist in the active investigation.
- Added FortiGate cookie-authenticated read adapters for interfaces, devices, policies, and configuration-change metadata without placing credentials in evidence records.
- Defined business evaluation over temporal case traces, comparing full memory, no memory, irrelevant history, and static runbooks on root-cause stability, contradiction revision, restart recovery, repeated probes, escalation, and action read-back.
