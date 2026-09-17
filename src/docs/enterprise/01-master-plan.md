# 01 · 企业级增强层总纲（Master Plan）

> 分支 `feature/enterprise` ｜ 基线 commit `983b3ceb`（上游 v2.2.1 发布后、2.2.2b1 线，未打 tag）
> fork：`origin = b1n-next/QwenPaw`，`upstream = agentscope-ai/QwenPaw`
> 文档版本：v1.1（2026-09-10 修订：对齐 feature/enterprise 与上游 v2.2.1 后演进）

---

## 1. 项目定位与使用边界

**一句话定位**：在 QwenPaw / QwenPaw Hub 之上构建一层**企业增强层**（Enterprise Layer），
覆盖身份权限、模型治理、部署运维、用量可观测与问数应用，服务内部可信网络环境。

**使用边界（必须写进交付文档与运维手册）**：

| 维度 | 边界 |
|---|---|
| 信任模型 | **内部可信成员**。对齐官方 #7318 声明：2.2.0 的 Local/Docker 隔离共享宿主内核，**不面向互不信任的公网多租户** |
| 网络 | 内网部署；hub 端口不暴露公网；如需远程访问走 VPN/零信任网关 |
| 规模 | 起步 ≤200 人（参照 #7318 社区反馈样本：10~200 人内部团队） |
| 合规 | 等保内网自用口径；SOC2/GDPR 级合规**不在短期范围**（见需求台账 H 区） |

**短期目标（Phase 0-1，约 2 个月）**：内部可部署的受限版——普通用户只见对话与应用，
管理员集中管模型与账号；每租户一容器的隔离粒度。

---

## 2. 现状资产盘点（经代码核查，非评估推测）

### 2.1 Hub 控制面已存在（~7500 行，`src/qwenpaw/hub/`）

| 资产 | 文件 | 状态 |
|---|---|---|
| 多用户认证（sqlite 用户表、admin/user 角色、注册开关、token 版本、账号禁用） | `hub/auth.py` | ✅ 可用，需扩展 |
| 每租户凭据保险库（`TenantCredentialVault`，runtime 边界令牌发放） | `hub/credentials.py` | ✅ 可用 |
| Runtime 编排（创建/启停/禁用/重建、操作互斥、生命周期线程池） | `hub/service.py`、`hub/registry.py` | ✅ 可用 |
| Provisioner 抽象（`configure/validate_config/preflight/start/stop/status/close` 六方法）+ Local/Docker 双实现 | `hub/provisioner.py`、`local_provisioner.py`、`docker_provisioner.py` | ✅ 接口稳定，可加 K8s 第三实现 |
| 个人代理（`/api/{path}` 全量转发、注入 `X-QwenPaw-Runtime-Token`、体积/空闲/首字节超时限制） | `hub/control_app.py` | ✅ **ACL 的实施点** |
| WS 代理 | `hub/websocket_proxy.py` | ✅ ACL 需同步覆盖 |
| 操作审计 store、管理端审计端点 | `hub/operations.py`、`/api/hub/admin/audit` | ✅ 雏形，可观测性在此之上扩展 |
| OAuth 回调路由（runtime 侧 OAuth 中转） | `hub/oauth_routes.py` | ✅ OIDC/SSO 可借力 |
| 静态 console 分发（同一 bundle，`mode: hub` 区分） | `hub/static_files.py` | ✅ |

### 2.2 本体 runtime 现状

| 事实 | 位置 | 对企业化的含义 |
|---|---|---|
| 单用户认证（一个账号，无角色） | `app/auth.py`（模块 docstring 明示 Single-user） | 权限必须做在 **hub 代理层**，不能指望 runtime |
| 边界令牌放行（`QWENPAW_RUNTIME_INTERNAL_TOKEN`） | `app/auth.py:782` | runtime 信任 hub；hub 是唯一策略点 |
| 会话历史 = 本地 sqlite | `agents/context/scroll/history.py` | 状态外置（远期 ★★★★★） |
| 记忆 = markdown + agent.md；**外置后端已插件化**（ADBPG/PowerContext 迁至 `plugins/memory/`，v2.2.1 commit 5b1fa7ce） | `agents/memory/`、`plugins/memory/` | 状态外置出现官方扩展点，Phase 3 路径改善 |
| 沙箱 per-tool-call 生命周期 | `sandbox/local_sandbox.py:100` | K8s 化采用"常驻 Agent Pod + 按需沙箱 Job"两级架构 |
| SandboxCapability 类已存在 | `sandbox/config.py:190` | 能力协商 schema 有落点 |
| Console 菜单静态注册，`MenuItem.visible` 钩子现成 | `console/src/layouts/registry/` | 菜单权限只需"服务端下发名单 + visible 过滤" |
| PawApp SDK（页面型应用、app-scoped chat、managed service、依赖健康） | `src/qwenpaw/pawapp/` | 问数应用的载体 |

### 2.3 上游速度（决定 fork 策略的硬数据）

- 近 6 周 **383 commits（日均 ~9）**，PR 编号 7600+，squash 工作流（无 merge commit）；
- **v2.2.1 已正式发布**，当前 2.2.2b1 线（本 fork 基线 983b3ceb 即此线）；
- 官方 #7318 承诺"数周内"公布 Hub roadmap（模型治理 / RBAC / K8s 均在其列）；
- **对 Phase 0 利好**：v2.2.1 后 console 新增 `layouts/registry/capabilities.ts`
  （`filterMenuForAgentCapabilities` 按 agent 能力过滤内置菜单；调用点 `Sidebar.tsx:143` 与
  `SettingsCenter/useSidebarEntryGroups.ts:28`）。菜单过滤管线已存在，角色权限过滤可在
  同一调用点组合复用（详见 03 §4.4 修订）。

---

## 3. 对外部评估（GLM5.3）的采纳与纠错记录

| 结论 | 处置 |
|---|---|
| 依赖排序：身份 → 状态外置 → RBAC/模型网关 → Provisioner → 可观测 → CI/CD | ✅ 采纳（身份部分修正为"扩展现有 hub 用户表"） |
| 状态外置是 K8s 化最大工作量 | ✅ 采纳；评级 ★★★★★，Phase 3 再决策 |
| 沙箱 per-tool-call 与常驻 Pod 的张力；两级架构 | ✅ 采纳（代码原文背书） |
| capability schema（isolation_level/network 等） | ✅ 采纳，落点 `SandboxCapability` |
| 硬拒绝/软降级区分 | ✅ 采纳，写入 06 设计 |
| "单管理员账号、没有用户体系，身份层从零建" | ❌ **纠错**：hub 已有多用户层；做扩展不做重建 |
| "未提 Hub 控制面资产" | ❌ **纠错**：切入点是扩展 Hub（proxied enforcement），不是重新设计 |
| "每用户一 Pod 浪费，应共享控制面+状态外置" | ⚠️ **降级为远期**：per-tenant Pod 与上游架构对齐、改动最小；共享化是工作量爆炸点 |
| 组织四级层级 / SCIM / SIEM / SOC2 / 数据驻留 | ⚠️ **降级为 backlog**（除非公司硬合规要求） |

---

## 4. 分层架构（目标态）

```text
┌────────────────────────────────────────────────────────────┐
│ 交付层          CI/CD · 环境分层(dev/staging/prod) · 发布审批│  ← Phase 2+(轻量)
├────────────────────────────────────────────────────────────┤
│ 治理层          模型统一治理 · 控制台/资源 RBAC · 配额熔断    │  ← Phase 0-2
│                 · 用量计量 · 审计(扩展现有 operations store)  │
├────────────────────────────────────────────────────────────┤
│ 身份层          hub 用户表扩展: 用户组 · OIDC SSO · PAT 作用域│  ← Phase 1-2
│                 （SCIM/组织层级 → backlog）                   │
├────────────────────────────────────────────────────────────┤
│ 控制面（Hub，现成扩展）                                       │
│   个人代理 + ACL 策略引擎（唯一强制点）                        │
│   runtime 编排 · TenantCredentialVault · 审计 · /api/version  │
│   → 下发 permissions/denied_menus 给 console                  │
├────────────────────────────────────────────────────────────┤
│ 执行面          每租户一个 runtime（Phase 1: Docker/K8s Pod） │
│                 Local · Docker · K8s Provisioner(新)          │
│                 常驻 Agent Pod + per-tool-call 沙箱           │
│                 能力协商：requirement ⊆ capability 才调度      │
├────────────────────────────────────────────────────────────┤
│ 基础设施层       Helm · PVC(per-tenant RWO) · Secret(Vault 可选)│  ← Phase 1-2
│                 · 备份(Velero) · 升级 · 审计留存               │
└────────────────────────────────────────────────────────────┘
```

**关键架构决策**：

1. **唯一强制点在 hub 个人代理**（`personal_runtime_proxy` + WS 代理）。runtime 不引入角色概念，
   保持对上游零侵入。菜单权限 = 服务端下发名单 + console 过滤，属 UX 层，不承担安全职责。
2. **每租户一 runtime** 保持上游模型。K8s 化第一版 = Hub Deployment + per-tenant Pod（RWO PVC 即可，
   不需要 RWX——这是 per-tenant 模式对共享化的决定性优势）。cron/心跳天然单副本，无跨副本去重问题
   （该问题只在共享化后出现）。
3. **附加层优先于侵入修改**：所有自研代码落在新文件/新目录（`hub/acl/`、`hub/provisioners/k8s/`、
   `deploy/helm/`、`plugins/apps/qa-data/`），对上游文件的直接修改压到最少（白名单见 09）。

---

## 5. 阶段路线与验收标准

| 阶段 | 周期 | 交付 | 验收标准（DoD） |
|---|---|---|---|
| **Phase 0 受限版** | 1-2 周 | hub ACL + 菜单权限下发 + fork 工程化（CI/文档） | ① user 角色直连 `/api/config` 经 hub 得 403；② user 的 console 不出现 工作区/设置/控制 菜单组；③ admin 全功能不受影响；④ 单测+e2e 覆盖 ACL 表 |
| **Phase 1 治理版** | 4-8 周 | 模型统一治理（集中 Provider+别名+隐藏凭据）· 用量计量（token/成本落审计 store）· K8s provisioner 骨架 + Helm chart | ① 管理员配一次 Provider，租户首启拿到可用模型且看不到 Key；② hub 审计页可查 per-user token 用量；③ `qwenpaw hub --provisioner k8s` 可起 per-tenant Pod 并通过健康检查 |
| **Phase 2 企业版** | 2-3 月 | 用户组 RBAC · OIDC SSO · 配额软硬阈值 · Velero 备份手册 · 问数应用 M1-M2 | ① 组级策略在代理层生效；② 企业 IdP 登录全自动建号；③ 超配额熔断+告警；④ 问数应用在受限租户可用 |
| **Phase 3 远期** | 视上游 | 状态外置 · 共享执行面 · gVisor/Kata · SCIM/合规栈 | **先对表官方 roadmap 再立项**（撞车风险最高区） |

每阶段结束打 tag `enterprise/vX`，并执行一次上游对齐例程（见 09）。

---

## 6. 风险登记（Top 5）

| 风险 | 概率 | 缓解 |
|---|---|---|
| 上游 roadmap 与自研撞车（模型治理/RBAC/Helm 均官方在列） | 高 | 每 phase 前"对表"：优先做官方未细化的（控制台权限、per-tenant 模型目录）；官方做了的评估复用/退役 |
| 上游高速演进导致 rebase 冲突 | 高 | 附加层纪律 + 修改白名单 + 月度 cherry-pick + 季度 rebase（09） |
| 单人带宽不足 | 中 | 严格按 DoD 切片交付；Phase 3 不承诺 |
| 内网 K8s 环境受限（无镜像仓库/受控 ingress） | 中 | Phase 1 K8s 后端做成可选，Docker 后端始终保留 |
| 外置记忆等实验性 API 变动 | 低 | 记忆后端已插件化（`plugins/memory/`），只经插件扩展点交互，不 import 内部实现 |

---

## 7. 文档地图

`02` 需求全覆盖台账（**查阅入口**）→ `03`~`08` 模块设计 → `09` 上游对齐纪律 → `10` 任务分解（WBS）。
