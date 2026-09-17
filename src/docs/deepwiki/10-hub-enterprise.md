# 10 · Hub 多租户与企业增强层（hub/ + deploy/ + docs/enterprise/）

> 本 fork（`feature/enterprise`）的增量主线。先讲上游自带的 Hub 控制面，再讲企业层在其上加的 ACL、模型治理、K8s、平台线治理贯通。

---

## 1. Hub 是什么（上游 v2.2.0 起）

**自托管多用户控制平面**：`qwenpaw hub` 起一个 FastAPI（`hub/control_app.py`，2181 行），管理员账户+普通用户（role∈{admin,user}，sqlite 用户表，`hub/auth.py:31-49`），**每用户一个 personal runtime**，hub 对 runtime 做纯网络代理（不 import runtime 内部代码）。

| 组件 | 职责 | 代码 |
|---|---|---|
| personal_runtime_proxy | `/api/{path}` 全量转发 + 注入 `X-QwenPaw-Runtime-Token` + 体积/空闲/首字节超时限制 | `hub/control_app.py:1702-1733` |
| WS 代理 | 语音等 WS 流量中继 | `hub/websocket_proxy.py` |
| TenantCredentialVault | 每租户凭据保险库、runtime 边界令牌发放 | `hub/credentials.py` |
| Provisioner 抽象 | `configure/validate_config/preflight/start/stop/status/close` 六方法 | `hub/provisioner.py` |
| Runtime 编排 | 创建/启停/禁用/重建、操作互斥 | `hub/service.py`、`hub/registry.py` |
| 操作审计 | `hub_audit_events`（sqlite）+ 管理端点 | `hub/operations.py` |
| 静态 console | 同一 bundle，`mode: hub` 区分 | `hub/static_files.py` |

## 2. 企业增强层定位（docs/enterprise/01）

**一句话**：在 QwenPaw / Hub 之上构建一层企业增强——身份权限、模型治理、部署运维、用量可观测、问数应用，服务**内部可信网络**（≤200 人，非公网多租户）。

三条铁律（`01-master-plan.md` §4 关键架构决策）：

1. **权限唯一强制点在 hub 个人代理**（personal_runtime_proxy + WS 代理）。runtime 保持单用户语义、对上游零侵入；console 菜单过滤只是 UX。
2. **每租户一个 runtime**（Pod）：与上游模型对齐，PVC 用 RWO 即可；cron/心跳天然单副本。
3. **附加层优先于侵入修改**：企业代码全落新文件（`hub/acl/`、`hub/provisioners/k8s/`、`deploy/helm/`），改上游文件须在 09 白名单内（上游日均 ~9 commits，fork 靠纪律存活）。

## 3. Phase 0 · 控制台权限（✅ 全部完成，EP-0-1..0-7）

- **ACL 引擎**（`hub/acl/engine.py`）：stdlib-only、admin 全通、其余按有序规则 first-match、**无匹配即 deny（fail-closed）**；支持 `acl.json` overlay 热载。
- DEFAULT_RULES 给 user 角色：先 deny agent 级管理子树（workspace/config/plugins…），再 allow chat 平面（`hub/acl/rules.py:53-70`）。
- **强制点**：`personal_runtime_proxy` 内 `acl.decide(role, method, path)`，不通过→403 `ACL_DENIED` + `acl.denied` 审计（`control_app.py:1702-1733`）；WS 代理同步覆盖（关闭码 1008）。
- **permissions 下发**：`GET /api/hub/me/permissions` → console `hubPermissionsStore` + 菜单过滤（复用上游 capabilities 管线，EP-0-5）；route 级裁剪 user 保留 7 个用户面页面、隐藏 18 个管理 route（v1.3 @c9aa6890 member 403 清扫）。
- DoD 实测：user 直连 `/api/config` 得 403；admin 无感；hub 套件 214 测试通过。

## 4. Phase 1 · 治理版（✅ 完成，EP-1-1..1-10，EP-1-5 并票）

| 能力 | 机制 |
|---|---|
| **模型统一治理**（EP-1-1/2/3） | hub 侧 model_catalog 表 + admin CRUD/test-connection；API key **Fernet 加密**，明文仅经 `QWENPAW_MODEL_BOOTSTRAP_JSON` env 注入租户（`hub/model_catalog/store.py:1-11`）；runtime bootstrap 钩子（唯一 runtime patch）；member "目录只读 + 目录内可切换"（不在 catalog→`MODEL_NOT_IN_CATALOG` 403） |
| **用量计量**（EP-1-4） | `UsageCollector` 每 60s 轮询运行中 runtime 的 `/api/token-usage/details`（internal token），SQLite `usage_counters` 按 (tenant,date,provider,model,agent_id) upsert（`hub/usage/collector.py:20-27`）；拉取式 = 零 runtime patch；admin 用量页 |
| **K8s Provisioner**（EP-1-6..1-9） | `hub/provisioners/k8s/`：每租户 1 Pod + 1 PVC + 1 ClusterIP Service、独立 namespace；runtime host suffix 白名单 **fail-closed**；helm chart（`deploy/helm/qwenpaw-hub/`）——hub **单副本 BY DESIGN**（sqlite 唯一写者，禁止扩副本）+ RWO PVC + RBAC 最小权限 + bootstrap_admin initContainer；kind 真集群验收（升级/重建演练 ✅） |

## 5. Phase 2 · 企业版（进行中）

**治理线**（EP-2-1..2-10，待办）：groups/policies 表 + 策略并入 AclEngine、OIDC SSO（JIT/组映射）、配额软硬阈值、Prometheus /metrics、Velero 备份手册、问数应用 M1-M4、runtime 受限 profile、金丝雀升级。

**平台线 P0 · 治理贯通**（✅ EP-2-11..2-14 已提交）+ **P1 · 编排原语**（EP-2-15 起，human_gate 已落）+ **P2 · 生态补齐**（MCP server 化/A2A 服务端/知识库等，见 docs/enterprise/11）：

| EP | 内容 | 机制要点 |
|---|---|---|
| **EP-2-11** | 跨平面 trace id | hub/trace.py 铸 32-hex id（仅 Hub 可铸，入站畸形值替换）；代理注入 `X-QwenPaw-Trace-Id` 并回显；hub_audit_events 加 trace_id 列（v2 迁移+索引）；runtime 侧纯 ASGI middleware（`app/trace_context.py`）镜像到 ContextVar，governance audit extra 携带——**一个 id 贯穿 hub 审计→请求→runtime 审计→响应**；console hub.ts listAuditEvents 支持 traceId 过滤 |
| **EP-2-12** | 审批持久化 + hub 台账 | approvals/store.py SQLite(WAL, 14d 清理) 镜像审批生命周期；启动恢复扫描使 pending 审批在 kill -9 后仍可应答；hub 缓冲 approve/deny 请求体并写 `approval.resolved` 审计（同 trace id） |
| **EP-2-13** | 组织策略基线下发 | hub policy_catalog 维护基线（单行+revision+sha256）；provisioner 经 `QWENPAW_POLICY_BASELINE_JSON` 注入；runtime `apply_hub_baseline_from_env` 先 canonical 化验证摘要，不匹配/畸形 **fail-closed 保留旧 hub 层**；hub_rules 高于 builtin/user 且**永不持久化到 YAML**（本地篡改无法降级组织策略）；STRICT 例外：即使 hub ALLOW 也升 ASK（`governance/policy.py:797-803,1395+`） |
| **EP-2-14** | 子 Agent principal 降权 | 三条 spawn 路径铸造 `<parent>:sub:<suffix>` principal 存 request_context（新 ContextVar）；contextvars hook spawn 轮设置、父/顶层轮清除防泄漏；**治理/审批仍走父身份**，token_usage 与 audit 归子 principal；hub usage 增 by_agent 分组（每子 Agent 成本可见） |
| **EP-2-15** | loop human_gate | 第 8 个内置 stop gate：轮次到点经共享 ApprovalService 挂起循环等人工批准（见 [03-agent-core §3](03-agent-core.md)） |

## 6. EP 提交线全列表（main..feature/enterprise，EP 线 33 个）

```text
Phase 0  814ecce9 EP-0-1/0-2 ACL 引擎 · 6a1116ea EP-0-3/0-4 代理 ACL+permissions
         55e7d3d3 EP-0-5 console 过滤 · c62787e5 EP-0-6 集成回归
         548aee63 EP-0-7 enterprise CI · 769dc4dc 验收 runbook（+style 3 个）
Phase 1  d2e6830d EP-1-1/2/3 模型目录+bootstrap · 4e2c1964 EP-1-4 用量
         2b240cb0 EP-1-6..1-8 K8s+helm · ec09a9a0 EP-1-9 kind 验收
         治理边界细化 7 个（2da1d468…c0f5174a）· 文档 3 个 · EP-1-10 对表
Phase 2  7433d5b3 EP-2-11 trace · f16c1c67 EP-2-12 审批持久化
         885257ca EP-2-13 策略基线 · 46dbb0d5 EP-2-14 子 principal
         357c11b7 EP-2-15 human_gate（HEAD）
```

tag：`enterprise/v0.1`（Phase 0 收口 @717c85ff）、`enterprise/v0.2`（Phase 1 收口）。

## 7. .hub-accept/：本地验收环境

**git-ignored 的验收脚手架**：`harness.py`（Phase 0 脚本化验收：对活 hub 验 403/permissions payload/WS 关闭码 1008/审计行）、`e2e_ep1.py`（EP-1 真机 E2E：admin 配 provider→member runtime 重启→目录可见）、`preflight/`（docker/local）、`runtimes/`（per-tenant 工作目录）、`control.db`（hub sqlite）。

## 8. 部署（deploy/）

- `docker-compose.yml`：单容器个人版（`agentscope/qwenpaw:latest`，127.0.0.1:8088，data/secrets/backups 三卷）。
- `deploy/helm/qwenpaw-hub/`：企业内网 K8s——hub Deployment 单副本（sqlite 单写者）+ RWO PVC(5Gi) + ClusterIP + 最小 RBAC + bootstrap_admin initContainer；per-tenant runtime Pod 由 provisioner 动态创建（qwenpaw-runtimes 命名空间）；安装手册 `docs/enterprise/runbook-k8s-install.md`（内网 Harbor/pre-pull、K8s 1.24+）。

## 9. 风险与上游对齐（fork 存活纪律）

- 上游 roadmap（#7318）与自研撞车高危区：模型治理/RBAC/Helm——每 phase 前"对表"，官方做了的评估复用/退役。
- 例程：月度 cherry-pick（RT-1）、季度 rebase（RT-2）、每周撞车复评（RT-3）。
- 已跟踪重叠 PR：#7696 local admin bootstrap（撞 hub/bootstrap_admin.py，合并即切官方）、#7683 hub 审计。

---

相关：[08-security-governance](08-security-governance.md)（治理策略三层）、[11-console-frontend](11-console-frontend.md)（hub.ts/Hub 管理页）、[12-testing-workflow](12-testing-workflow.md)（enterprise CI）
