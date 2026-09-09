# 设备配置变更与审批

## 执行流程

前端入口：证据与处置 → 处置记录 → 配置变更与审批。地址归属时间线也有设备配置变更入口。

1. 管理员登录审批会话，选择已注册设备、操作及目标，填写业务目的。
2. 读取设备序列号、版本、受影响配置与健康基线。保存原值、目标值及校验摘要。
3. 展示逐字段 Diff、目的、验证范围、观察窗口和恢复方式。预览有效期 15 分钟。
4. 人工审批绑定该摘要，或匹配服务器配置中的精确预授权规则。
5. 执行前再次检查设备身份、配置及健康状态。配置变化使原审批失效。
6. 保存操作意图，再提交命令；逐步读取配置，持续采样业务及管理连接。
7. 连续三次目标检查通过且整个观察窗口满足条件，才记录验证通过。
8. 异常恢复原配置。当前值被其他人修改时停止覆盖，保留证据并升级人工。

观察期间可查看逆向 Diff，申请停止后续动作并恢复。临时封禁到期后由后台任务发起恢复。瞬时操作（重启、ARP 或租约清理、端口重启）没有可恢复的瞬时状态，异常升级人工；界面会在执行前注明。

## 实现文件

- `core/remediate/change_control.py`：SQLite 变更单、审批、设备锁、回执、恢复、精确预授权与到期恢复。
- `frontend/gateway/app/device_changes.py`：FortiOS 6.2 配置操作和命令生成，目标路径由服务端构建。
- `frontend/gateway/app/host_changes.py`：已注册 Linux 主机的服务、网卡、Netplan 限时变更。
- `frontend/gateway/app/change_routes.py`：审批会话、接口与后台观察任务。
- `frontend/src/components/ChangeWorkbench.tsx`：目的、Diff、审批、观察记录和恢复预览。

变更数据默认保存于 `/data/autopoiesis-production/device-changes.sqlite3`，文件权限 0600。事件记录采用追加方式。案件关联使用 `case_id`，结果回写案件事件；配置验证通过后案件保留业务复核状态。

## 设备接入与保护配置

配置文件：`AUTOPOIESIS_DEVICE_CHANGE_POLICY`，默认 `/data/autopoiesis-production/device-change-policy.json`。该文件必须由服务器维护，禁止组或其他用户写入。登录口令从现有设备凭据中读取，不存入变更单。远程审批登录使用 HTTPS，也可经 SSH 转发使用 localhost。

FortiGate 必填：`serial`、`certificate_sha256`、`vdom`、`business_checks`。证书指纹绑定在实际 TLS 连接上，设备身份变化会中止执行。

按目标配置额外条件：

| 操作 | 接入条件 |
| --- | --- |
| DHCP 地址保留与转移 | 唯一 DHCP 服务、池内地址、已观测或已登记 MAC |
| DHCP / ARP 清理 | 可用 SSH 连接，已核对并保存设备主机公钥 |
| 接入口 / PoE / VLAN | 真实受管交换机、`access_ports` 清单，排除上联、堆叠和共享端口 |
| 精确策略 / 临时封禁 | 已存在的精确地址与服务对象、业务效果检查；新规则需要 `before_policy` 指定位置 |
| 管理员允许来源 | 独立恢复通道已核实，新的允许范围保留当前管理来源 |
| 主机服务恢复 | 注册主机及明确的服务单元清单 |
| 主机网络配置 | 注册接口与独立管理连接、可用 Netplan D-Bus 接口 |

当前环境核验：FortiGate `FG100ETK20014183`，FortiOS `v6.2.7`，管理员配置为 `super_admin`。实际读到了 DHCP 服务与保留项；受管 FortiSwitch 配置列表为空。设备写权限与接入对象是否存在分别判断。

初始策略未开放交换机接入口、主机网络接口或自动预授权。Linux 服务清单包含专用验收单元 `autopoiesis-acceptance-change.service`。已有主机自动恢复通道继续使用原有动作预算和暂停开关。

审批写入使用独立暂停开关，页面登录后可填写原因并启用或暂停。启用仅允许已审批方案提交，原有自动恢复通道的状态保持不变。已存在的全局暂停同时约束审批写入。首次部署默认暂停。

`effect_checks` 按操作保存规则列表，每项包含完整 `target`、完整 `params`、`checks`。效果探测只适用于精确匹配的变更目标和参数；匹配结果随方案保存并参与审批摘要。每个检查包含 `host`、`port`、`reachable`。检查点应位于能验证目标策略效果的访问路径。

当前管理接口允许 HTTPS，未启用 SSH。DHCP 租约和 ARP 清理需要先单独核实 SSH 接入与设备公钥。

## 本机受控演示

运行 `frontend/.venv/bin/python scripts/demo_approved_change.py`，创建专用失败服务。在“证据与处置 → 处置记录 → 配置变更与审批”选择本机和服务恢复，填写脚本给出的目标。页面生成原状态与目标状态对照，审批后执行，按 10 秒间隔保存 12 次观察。

演示结束运行 `frontend/.venv/bin/python scripts/demo_approved_change.py --cleanup`。只清理该专用服务与标记，审批、命令和观察记录保留。

2026-09-07 本机实测：变更 `e9996e02a32e4b8a852eb0c03b58a458` 经浏览器核对 Diff、审批并提交，专用服务从 failed 恢复 active，12 次连续观察通过。验收完成后暂停审批写入，清理专用单元和标记，保留回执。FortiGate DHCP 变更 `9ce64aef3fef441783b81cd0e158bb66` 完成真实配置读取与差异预览后取消，无设备写入。恢复逻辑的异常、超时、配置漂移覆盖由隔离适配器测试验证。

## 恢复边界

- DHCP 保留配置的逆向操作只修改本次条目，不清空整个池。
- 新防火墙规则先禁用创建，再移到审批位置，最后启用。恢复包括规则位置与配置。
- Netplan 使用 D-Bus Config / Set / Try，240 秒未确认时由主机侧恢复；观察通过后 Apply。恢复仍需读取配置和连通性。
- 管理通道失联且恢复命令无法送达时，状态保持需要人工处理，设备锁保留。
- 服务进程重启会把中断中的变更标记为需要人工处理，禁止自动重放不确定命令。
- DHCP 客户端获址情况与配置读回分别记录；静态 IP 冲突可能需要终端改址。主机重启后的持久性需要独立重启验收。

## 预授权规则

`preauthorizations` 的每项必须包含：`id`、`device_id`、`action`、完整 `target`、完整 `params`、`plan_digest`、Unix 时间 `expires_at`、`max_uses`。

`plan_digest` 是 `core.remediate.change_control.digest(plan)` 的结果。匹配包含备份原值和目标值，配置变化会使匹配失败。次数上限写入数据库，进程重启保留。浏览器不能提交或修改规则定义。

## 验证

核心检查：

```bash
frontend/.venv/bin/python -m pytest tests_py/test_device_change_control.py tests_py/test_ownership_decisions.py -q
cd frontend
npm run build
npm run lint
```

设备真实写入验收需选定受控目标并在页面批准。生产配置的只读预览、隔离适配器测试、真实设备提交结果分别记录。

协议依据：

- [FortiOS 6.2.7 DHCP 配置](https://docs.fortinet.com/document/fortigate/6.2.7/cli-reference/59620/config-system-dhcp-server)
- [Fortinet REST 对象创建、修改与移动](https://github.com/fortinet-solutions-cse/fortiosapi/blob/master/fortiosapi/fortiosapi.py)
- [Netplan D-Bus 配置事务](https://netplan.readthedocs.io/en/0.106/dbus-config/)
