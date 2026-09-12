# 02 · 需求全覆盖台账（Requirements Matrix）

> 用途：单一事实源。任何新需求先入台账再排期；任何设计/实现决策回链到需求 ID。
> 状态：✅ 已有 ｜ 🟡 部分 ｜ ❌ 无 ｜ 优先级：P0 必须 / P1 重要 / P2 有价值 / P3 backlog
> 来源：USR=用户提出 ｜ HUB=官方 #7318 方向 ｜ GLM=外部评估补全 ｜ AUD=代码核查结论
> 撞车：上游是否可能自己实现（高/中/低）——决定自研的沉没风险

## A. 部署与网络

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| A1 | 内网可信环境部署（不出公网） | USR | ✅（现状即支持） | P0 | — | — |
| A2 | Helm Chart：Hub Deployment + per-tenant Pod | HUB | ❌ | P1 | Ph1 | **高**（官方在考虑 K8s） |
| A3 | per-tenant PVC（RWO 即可，per-tenant 模型下无需 RWX） | AUD | ❌ | P1 | Ph1 | 高 |
| A4 | Secret 集成（K8s Secret 起步，Vault/KMS 可选） | HUB/GLM | 🟡（hub 已有凭据库，K8s 投递层缺） | P1 | Ph1 | 中 |
| A5 | 多机调度（K8s 原生调度即可满足） | HUB | ❌ | P2 | Ph1 | 高 |
| A6 | 弹性扩缩容（Hub 层 HPA；runtime per-tenant 不扩副本） | HUB | ❌ | P2 | Ph2 | 高 |
| A7 | 升级策略（hub 滚动升级 + runtime 重建；金丝雀/蓝绿） | HUB/GLM | ❌ | P2 | Ph2 | 中 |
| A8 | 备份容灾（Velero/PVC 快照 + sqlite 备份手册化） | HUB/GLM | ❌ | P1 | Ph2 | 低 |
| A9 | 定时任务幂等/去重 | GLM | 🟡（per-tenant 单副本天然无重复；共享化后才需要分布式锁） | P3 | Ph3 | 低 |
| A10 | 会话粘性 | GLM | 🟡（per-tenant 模型下 hub 代理天然路由到唯一 runtime；共享化后才需要） | P3 | Ph3 | 低 |
| A11 | 供应链安全（镜像签名验证、SBOM） | GLM | ❌ | P3 | backlog | 中 |
| A12 | GPU 资源配额与亲和调度 | GLM | ❌ | P3 | backlog | 中 |

## B. 控制台与菜单权限（本仓库切入点）

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| B1 | user 角色隐藏 工作区/设置/控制 菜单组 | USR | 🟡（v2.2.1 后上游已有 `capabilities.ts` 菜单过滤管线——按 agent 能力过滤；**角色维度仍缺**，可在同管线组合复用，见 03 §4.4） | **P0** | Ph0 | **低**（官方 #7318 未提控制台角色粒度；capabilities 管线是互补非撞车） |
| B2 | user 角色的对应 API 在 hub 代理层 403（真安全边界） | USR/AUD | ❌（`personal_runtime_proxy` 全量转发） | **P0** | Ph0 | 低 |
| B3 | WS 代理同步 ACL | AUD | ❌ | P0 | Ph0 | 低 |
| B4 | `/api/version` 下发 permissions/denied_menus | AUD | 🟡（hub 已重写该响应注入 `mode: hub`） | P0 | Ph0 | 低 |
| B5 | 按租户/组定制菜单白名单（而非全局两档） | USR 扩展 | ❌ | P2 | Ph2 | 低 |
| B6 | 直连 runtime 场景的受限 profile（`QWENPAW_CONSOLE_PROFILE`） | AUD | ❌ | P2 | Ph2 | 中 |
| B7 | 移动端/瘦客户端仅对话视图 | #7318 社区 | ❌ | P3 | backlog | 中 |

## C. 身份与组织

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| C1 | 多用户账号（注册/禁用/改密/登录限速） | USR/HUB | ✅（`hub/auth.py`：sqlite 用户表、角色、token 版本、锁定） | — | — | — |
| C2 | 用户组（group + member 表，组级策略挂载点） | HUB/GLM | ❌ | P1 | Ph2 | **高**（官方"用户组、RBAC"在列） |
| C3 | OIDC SSO（企业 IdP：Keycloak/AD/Authing；JIT 建号；组映射） | GLM | 🟡（`oauth_routes.py` 有 OAuth 中转骨架，非 OIDC RP） | P1 | Ph2 | 高 |
| C4 | LDAP 直连 | GLM | ❌ | P2 | backlog | 中 |
| C5 | SCIM 自动回收（离职联动） | GLM | ❌ | P3 | backlog | 中 |
| C6 | 组织层级（租户→部门→团队四级） | GLM | ❌（扁平 group 起步） | P3 | backlog | 中 |
| C7 | PAT 细粒度作用域（scoped token 只能调某 Agent/某 API 组） | GLM | 🟡（runtime 曾有 owner/collaborator/viewer 三级 token，#180 已实现基础层） | P2 | Ph2 | 中 |
| C8 | 委托/临时授权 | GLM | ❌ | P3 | backlog | 低 |

## D. RBAC 资源粒度

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| D1 | 角色→控制台能力（=B1/B2，先行切片） | USR | ❌ | P0 | Ph0 | 低 |
| D2 | 按用户/组控制 Agent 访问 | HUB | ❌ | P1 | Ph2 | **高** |
| D3 | 按用户/组控制 Skill / MCP / Channel 访问 | HUB | ❌ | P1 | Ph2 | 高 |
| D4 | 策略引擎最小实现（subject→resource→effect，静态配置起步，不引入 OPA） | AUD | ❌ | P1 | Ph2 | 中 |
| D5 | Agent/Skill 上架审批流 | GLM | ❌（市场有安装，无审批） | P2 | Ph2 | 中 |
| D6 | 多租户共享 Agent/Skill 商店（组织级发布/分享） | #7318 社区(rerbin) | ❌ | P3 | backlog | 中 |

## E. 模型统一治理

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| E1 | 管理员集中配置 Provider/Endpoint/Key | HUB | 🟡（hub 有 vault；模型配置仍在各 runtime） | **P0** | Ph1 | **高**（官方"Central model governance"在列） |
| E2 | 用户只见批准的模型目录/别名，不见凭据与 Endpoint | HUB/USR | ❌ | P0 | Ph1 | 高 |
| E3 | 首启 bootstrap：新租户 runtime 自动拿到可用默认模型 | HUB | 🟡（qwenpaw-data app 已示范从宿主读 active model 的路径，可复用） | P0 | Ph1 | 高 |
| E4 | 默认模型与按用途路由（编码→强模型，闲聊→轻模型） | HUB/GLM | ❌ | P2 | Ph2 | 高 |
| E5 | 故障切换/fallback 链 | HUB | ❌ | P2 | Ph2 | 高 |
| E6 | 限流与并发控制 | HUB | ❌ | P2 | Ph2 | 高 |
| E7 | 额度与成本控制（预算/熔断，与 F 区配额联动） | HUB | ❌ | P2 | Ph2 | 高 |
| E8 | Key 轮换机制 | GLM | 🟡（vault 有 secret 管理，轮换流程缺） | P2 | Ph2 | 中 |
| E9 | 模型→RBAC 交叉（不同组可见不同模型子集） | GLM | ❌ | P2 | Ph2 | 高 |
| E10 | 计费精度到对话/Agent 级 | GLM | ❌ | P3 | backlog | 中 |

## F. 用量与可观测

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| F1 | per-user token/成本统计 | HUB | 🟡（`HubOperationsStore` + `/api/hub/admin/audit` 有雏形；token 计量未接） | P1 | Ph1 | 高 |
| F2 | 计量采集点（runtime usage 事件上报 hub，代理层不解析 SSE） | AUD | ❌ | P1 | Ph1 | 中 |
| F3 | 配额软硬双阈值（80% 告警 / 100% 熔断，hub 代理前置检查） | HUB/GLM | ❌ | P1 | Ph2 | 高 |
| F4 | Prometheus 指标导出（hub `/metrics`） | GLM | ❌ | P2 | Ph2 | 中 |
| F5 | OpenTelemetry trace | GLM | ❌ | P3 | backlog | 中 |
| F6 | 审计事件结构化（who/what/when/allow-deny/reason，落 operations store 扩展表） | GLM/AUD | 🟡 | P1 | Ph1 | 中 |
| F7 | 运行时日志按租户留存与检索 | HUB | 🟡（runtime 日志本地；hub 不汇聚） | P2 | Ph2 | 中 |
| F8 | 健康状态面板（runtime 起停/资源，admin 页已有骨架） | HUB | 🟡（`/api/hub/admin/overview` 已有） | P1 | Ph1 | 低 |
| F9 | SIEM 对接/日志外送 | GLM | ❌ | P3 | backlog | 低 |

## G. 运行时与隔离

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| G1 | Runtime Provisioner 第三实现：K8s（per-tenant Pod） | HUB | ❌（接口六方法已稳定） | P1 | Ph1 | **高** |
| G2 | 能力协商协议（requirement ⊆ capability 才调度；schema 借 `SandboxCapability`） | HUB/GLM/AUD | 🟡（类存在，协商未实现） | P2 | Ph2 | 中 |
| G3 | 拒绝启动而非降级（fail-closed）+ 硬拒绝/软降级区分 | HUB/GLM | 🟡（provisioner preflight 已 fail-closed；能力级细分缺） | P1 | Ph1-2 | 中 |
| G4 | gVisor/Kata/MicroVM 后端 | HUB | ❌ | P3 | Ph3 | 中 |
| G5 | 远程 runtime 后端（跨机） | HUB | ❌ | P3 | backlog | 中 |
| G6 | per-tenant 运行时池与资源上限（Docker 已有 limits，K8s 用 quotas/limits） | HUB/GLM | 🟡 | P1 | Ph1 | 高 |
| G7 | 常驻 Agent Pod + 按需沙箱 Job 两级执行（K8s 场景沙箱不逐调用启 Pod） | AUD/GLM | ❌ | P2 | Ph2 | 中 |

## H. 数据治理与合规

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| H1 | 记忆/知识三档共享（个人私有 / 部门共享 / 租户公共） | #7318 社区(Marlin-Phone/ysf7762) | ❌（每 agent 记忆独立） | P2 | Ph2-3 | 中 |
| H2 | 审计日志 append-only/防篡改 | GLM | 🟡（sqlite 表，无链式校验） | P2 | Ph2 | 低 |
| H3 | 审计留存周期与导出接口 | GLM | ❌ | P2 | Ph2 | 低 |
| H4 | 数据驻留（多地域不跨区） | GLM | ❌ | P3 | backlog | 低 |
| H5 | 用户数据导出/删除（GDPR 式） | GLM | ❌ | P3 | backlog | 中 |

## I. 交付与环境管理

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| I1 | fork 工程化：分支/基线 tag/CI 跑通上游测试 | USR | ❌→本批交付 | P0 | Ph0 | — |
| I2 | 环境分层 dev/staging/prod（Helm values 分档） | GLM | ❌ | P2 | Ph2 | 低 |
| I3 | Agent/Skill/人格版本化与回滚 | GLM | 🟡（checkpoint/backup 已有基础） | P2 | Ph2 | 中 |
| I4 | 发布流水线（开发→审核→灰度→全量） | GLM | ❌ | P3 | backlog | 低 |

## J. 问数应用（数据智能问答）

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| J1 | 通用问数：数据源维护（CRUD+连通测试+凭据入 vault） | USR | ❌（QwenPaw-Data 有参考实现） | P1 | Ph2 | 中（上游 app 演进中） |
| J2 | schema 内省与缓存（库/表/列/注释） | USR | ❌ | P1 | Ph2 | 中 |
| J3 | 语义层（表列业务描述/指标口径/维度/同义词/示例问答） | USR | ❌ | P1 | Ph2 | 中 |
| J4 | 对话选源/选表提问 + SQL 透明展示 + 结果表格 | USR | 🟡（qwenpaw-data ChatWorkspace 可参照） | P1 | Ph2 | 中 |
| J5 | 只读 SQL 护栏（sqlglot 白名单、LIMIT/超时/行数） | USR/AUD | ❌ | P0（随 J1） | Ph2 | 低 |
| J6 | schema 向量检索挑表（大库不全量入 prompt） | USR | ❌ | P2 | Ph2-3 | 中 |
| J7 | NL2SQL 金集评测与回归 | AUD | ❌ | P1 | Ph2 | 低 |
| J8 | 图表可视化/导出 | USR | ❌ | P3 | backlog | 低 |
| J9 | 与企业层联动：问数 app 对 user 开放、数据源管理页仅 admin（复用 B 区机制） | AUD | ❌ | P1 | Ph2 | 低 |

## K. 其他社区诉求（记录备查，不承诺）

| ID | 需求 | 来源 |
|---|---|---|
| K1 | 桌面端稳定性/崩溃局部化（#7318 多条） | 社区 |
| K2 | 团队公共知识库与写入审批（rerbin/Marlin-Phone 讨论） | 社区 |
| K3 | 审批规则智能生成（funnygeeker） | 社区 |
| K4 | Hub 功能不拖累个人版体积（xiaohushi512） | 社区 |

---

## 台账维护规则

1. 新需求先登记（编号顺延），经"现状核查 + 撞车评估"后再入阶段；
2. 每阶段启动时复核一次撞车列（对表官方 roadmap / 近期 PR）；
3. 完成的需求在状态列标 ✅ 并链接落地 PR/commit；
4. 降级/否决的需求保留行（含理由），避免重复评估。
