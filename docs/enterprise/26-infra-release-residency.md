# 26 · 基础设施与发布面（A6/A12/G5/H4/I4/H1）

> 2026-09-19 · 状态：**设计与落地清单已落**；执行面依赖内网 K8s/GPU/多地域/流水线环境——各票 🟡（外部依赖前的大设计冻结）
> 原则：这些票的共同点是"代码在 hub 侧已就绪或不需要新代码，堵点在部署环境"。本文档把每个堵点拆成可执行的检查单。

## 1 · A6 弹性扩缩容（Hub 层 HPA）

**结论**：runtime per-tenant **不扩副本**（会话粘性=有状态），只扩 Hub 控制面。

- 指标就绪：`qwenpaw_hub_requests_total` 等已在 `/metrics`（07 §4，手写 registry 无额外依赖）。
- HPA 模板（K8s 面）：

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: qwenpaw-hub}
spec:
  scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: qwenpaw-hub}
  minReplicas: 2
  maxReplicas: 6
  metrics:
  - type: Resource
    resource: {name: cpu, target: {type: Utilization, averageUtilization: 65}}
```

- **前置条件**（当前不满足，故 ❌→🟡）：Hub 无状态化确认——control.db 是 SQLite，**多副本前必须切外部 Postgres 或改单写多读**；这是 A6 的真实堵点，不是 HPA 本身。
- runtime 容量：走 per-tenant 惰性启停（已有 offload 策略）而非副本扩缩。

## 2 · A12 GPU 资源配额与亲和调度

- runtime 侧已有 docker provisioner（`runtime_environment` 注入）；GPU 节点亲和在 K8s provisioner 侧补：
  - node label：`gpu=true`、`gpu-type=A100|L40S…`
  - runtime pod 模板加 `nodeSelector`/`tolerations`（按 agent 配置声明 `gpu: required|preferred`）
  - 配额：namespace ResourceQuota 限 `requests.nvidia.com/gpu`；hub 侧 G3 配额已限 token/request，GPU 数量配额挂 K8s admission（LimitRange+Quota）不在 hub 重复实现
- **堵点**：内网 GPU 节点与 device-plugin 未就绪；模板字段设计已冻结（上述），K8s provisioner 落地时直接取用。

## 3 · G5 远程 runtime 后端（跨机）

- provisioner 抽象已存在（`local/docker/k8s` 三实现）；远程=**第四个 provisioner**（`remote-ssh` 或 agent 模式）：
  1. SSH provisioner：`asyncssh` 连远程主机起容器/进程，回注册 hub（反向心跳）
  2. 或轻 agent：远程跑 `qwenpaw agent --hub-url ...`，hub 只记账路由（推荐——复用既有 runtime 代理链路，改动面最小）
- 安全：远程 agent ↔ hub 用 mTLS 或 hub 签发的 agent token（凭据入 vault，EP-2-4 惯例）
- **堵点**：跨机网络打通（内网防火墙）；agent 模式的 token 签发端点待票。

## 4 · H4 数据驻留（多地域不跨区）

- QwenPaw 单 hub 数据面=hub root 目录（control.db/vault/secrets）+ runtime 工作区。**不跨区的落地=分域部署**：
  - 每地域独立 hub 实例（`QWENPAW_HUB_DIR` 分域）
  - 用户归属地域=入口路由（DNS/网关按地域分流），hub 间**零数据同步**（避免跨区复制即避免违规）
  - 凭据/审计/用量全部域内闭环（现有单 hub 语义天然满足）
- **堵点**：多地域入口路由是网络侧工程；hub 侧无需新代码——这是四票里唯一"零代码"的。

## 5 · I4 发布流水线（开发→审核→灰度→全量）

- 已有件：canary runbook（`runbook-canary-upgrade.md`）、D5 审批流（模板面）、审计链（H2）。
- 补齐设计（4 阶段门）：
  1. **开发**：分支+全绿门槛（本仓纪律：pytest+hooks+console 三套）
  2. **审核**：D5 审批流类比——PR 附测试证据，reviewer 批准（人工门）
  3. **灰度**：canary 升级（既有 runbook：单 runtime 先行+指标观察窗）
  4. **全量**：分批 rollout（K8s 滚动 or 逐 tenant 重启）
- 流水线载体：内网 CI（待 IT）；本仓侧各阶段门的脚本化已隐含在 Makefile/test 纪律中。
- **堵点**：内网 CI runner；设计已冻结可挂接。

## 6 · H1 记忆/知识三档共享

- 现状：记忆纯 runtime 侧 per-agent（上游），hub 无共享记忆面。
- 目标三档：**个人私有**（=现状 agent 记忆）｜**部门共享**｜**租户公共**。
- 设计（hub 面新增 KnowledgeStore，供 runtime 挂载）：
  - 表：`knowledge_docs(id, scope_tenant|group, group_id, title, content, updated_at)`——租户档 scope=tenant 全员可读；部门档 scope=group 沿 C6 组树继承读权限
  - runtime 注入：`QWENPAW_KNOWLEDGE_*` 环境变量（hub 已有环境注入链路 `runtime_environment`）
  - 写权限：admin/组管理员写，成员只读（复用 ACL）
- **与 J 线问数的关系**：J3 语义层（表列业务描述）同构——建议合并为一个 hub 知识面实现，两票共享存储。
- **堵点**：非外部依赖，是**工作量票**（存储+API+runtime 挂载三段）；建议与 J3 同批排期。

## 7 · 汇总

| 票 | 状态 | 堵点性质 |
|---|---|---|
| A6 | 🟡 设计冻结 | SQLite 多副本（真代码票，后置） |
| A12 | 🟡 模板冻结 | GPU 节点环境 |
| G5 | 🟡 架构定型 | 跨机网络+agent token 票 |
| H4 | 🟡 零代码结论 | 多地域入口路由 |
| I4 | 🟡 阶段门冻结 | 内网 CI runner |
| H1 | 🟡 设计冻结 | 工作量票，建议并 J3 同批 |
