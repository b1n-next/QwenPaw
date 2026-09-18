# 02 · 需求全覆盖台账（Requirements Matrix）

> 用途：单一事实源。任何新需求先入台账再排期；任何设计/实现决策回链到需求 ID。
> 状态：✅ 已有 ｜ 🟡 部分 ｜ ❌ 无 ｜ 优先级：P0 必须 / P1 重要 / P2 有价值 / P3 backlog
> 来源：USR=用户提出 ｜ HUB=官方 #7318 方向 ｜ GLM=外部评估补全 ｜ AUD=代码核查结论
> 撞车：上游是否可能自己实现（高/中/低）——决定自研的沉没风险

## A. 部署与网络

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| A1 | 内网可信环境部署（不出公网） | USR | ✅（现状即支持） | P0 | — | — |
| A2 | Helm Chart：Hub Deployment + per-tenant Pod | HUB | ✅（`deploy/helm/qwenpaw-hub/`：deployment/pvc/service/rbac/configmap + bootstrap_admin initContainer + NOTES；kind 验收过） | P1 | Ph1 | **高**（官方在考虑 K8s） |
| A3 | per-tenant PVC（RWO 即可，per-tenant 模型下无需 RWX） | AUD | ✅（`provisioners/k8s/manifest.py` PVC builder + stop 保 PVC 会话延续，06 §7.1 kind 实测） | P1 | Ph1 | 高 |
| A4 | A4 | Secret 集成（K8s Secret 起步，Vault/KMS 可选） | ✅（闭环 EP-2-13 归置承诺：`hub-secret.yaml` 新模板（admin 凭据 + 可选 OIDC client_secret 入 Secret 资源）；**OIDC secret 已入 vault**：`QWENPAW_HUB_OIDC_CLIENT_SECRET` env 启动时一次性导入加密 vault 并即刻清 env，`_build_oidc_client` 三源解析（yaml 显式值 → vault → 空）；Deployment env/init args 全部 `secretKeyRef`/`$(VAR)` 引用（**spec 零明文**，grep 实证 0）；`hub.secretProvider.enabled` 开关（默认 off 兼容旧流，staging/prod 档默认 on）；顺手修真 bug：bootstrap init 缺 `--root` 参数（镜像 argparse 必需，CrashLoopBackOff 实证）；kind 实弹：helm upgrade → rollout → Secret 投递冒烟 SMOKE-PASS。Vault/KMS 升级位留待需要时） | ✅ | Ph2（已落） | Vault/KMS 可选升级 |
| A5 | 多机调度（K8s 原生调度即可满足） | HUB | ✅（随 G1 达成：k8s provisioner 起 per-tenant Pod 跨节点调度；多副本 hub 仍属 A6 状态外置前提） | P2 | Ph1 | 高 |
| A6 | 弹性扩缩容（Hub 层 HPA；runtime per-tenant 不扩副本） | HUB | ❌ | P2 | Ph2 | 高 |
| A7 | A7 | 升级策略（hub 滚动升级 + runtime 重建；金丝雀/蓝绿） | ✅（EP-2-10 金丝雀：sqlite 单写者约束下采用**隔离状态金丝雀**——emptyDir 草稿副本 + `hub-smoke.sh` 五关冒烟门 + JSON patch 选择器切流；kind 全链路实测含回退（merge patch 不删 selector key 的踩坑已固化为手册警示）；runtime 升级走 registry 期望态重建）；**停机窗口已明示**：runbook-canary §5 量化 Recreate 单副本窗口（~30-80s 典型）+ 五级缓解（金丝雀先行/镜像预热/低峰+备份兜底/PDB 显式预算/零停机=Phase3+ 状态外置议题）| ✅ | Ph2（已落） | — |
| A8 | A8 | 备份容灾（Velero/PVC 快照 + sqlite 备份手册化） | ✅（EP-2-5 `1a41…`：`runbook-backup-restore.md` 双层手册——SQLite 在线 `.backup` 脚本 `deploy/scripts/backup-hub-sqlite.sh`（WAL 一致快照+SHA256SUMS+轮转）+ Velero 卷级步骤；**L1 恢复演练实测闭环**（破坏→恢复→integrity ok→行数/vault 对账→轮转 8→3），L2 待生产首跑补记） | ✅ | Ph2（已落） | — |
| A9 | 定时任务幂等/去重 | GLM | 🟡（per-tenant 单副本天然无重复；共享化后才需要分布式锁） | P3 | Ph3 | 低 |
| A10 | 会话粘性 | GLM | 🟡（per-tenant 模型下 hub 代理天然路由到唯一 runtime；共享化后才需要） | P3 | Ph3 | 低 |
| A11 | 供应链安全（镜像签名验证、SBOM） | GLM | ❌ | P3 | backlog | 中 |
| A12 | GPU 资源配额与亲和调度 | GLM | ❌ | P3 | backlog | 中 |

## B. 控制台与菜单权限（本仓库切入点）

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| B1 | user 角色隐藏管理页面（v1.3 演进为精确 route 级，保留 7 个用户面页） | USR | ✅（`registry/permissions.ts` + Sidebar/Route/OS dock 过滤，03 §4.4；vitest 覆盖） | **P0** | Ph0 | **低**（官方 #7318 未提控制台角色粒度；capabilities 管线是互补非撞车） |
| B2 | user 角色的对应 API 在 hub 代理层 403（真安全边界） | USR/AUD | ✅（`hub/acl/` 引擎 + 代理 decide→403 + `acl.denied` 审计；fail-closed；67+ 单测/集成用例） | **P0** | Ph0 | 低 |
| B3 | WS 代理同步 ACL | AUD | ✅（websocket_proxy decide→close 1008） | P0 | Ph0 | 低 |
| B4 | permissions 下发（**端点实现定名 `/api/hub/me/permissions`**，公开 version 端点不承载角色数据；四键 payload 含 model_readonly） | AUD | ✅（control_app.py + console_map.py） | P0 | Ph0 | 低 |
| B5 | 按租户/组定制菜单白名单（而非全局两档） | USR 扩展 | ✅（`acl/console_map.py` 组/用户 `menu:<group>` 策略消费，菜单载荷按策略过滤；`test_menu_policies.py` 7 用例） | P2 | Ph2（已落） | 低 |
| B6 | B6 | 直连 runtime 场景的受限 profile（`QWENPAW_CONSOLE_PROFILE`） | ✅（EP-2-9：runtime 环境变量 `QWENPAW_CONSOLE_PROFILE=restricted` 启用 `/api/console/profile`（复用 hub user 档 payload，shape 同 `/hub/me/permissions`）；console 降级链升级为 permissions 404 → 探测 profile → 才全量；未设置/`full` 行为与上游完全一致） | ✅ | Ph2（已落） | — |
| B7 | 移动端/瘦客户端仅对话视图 | #7318 社区 | ❌ | P3 | backlog | 中 |
| B8 | Hub 控制台吸收上游重构（治理/邀请/用量面板 UI） | 上游 #7779 | ✅（吸收接面：fork Hub 页新增治理 section（托管模型 OrganizationModels + 组织预算 OrganizationBudget + 邀请 Invitations，上游 #7779 组件接入 fork 版导航/面板骨架）；locale 7 语言；测试 14 例（含 governance 冒烟）；fork 页既有用量表格保留） | P2 | Ph2（已落） | 中 |

## C. 身份与组织

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| C1 | 多用户账号（注册/禁用/改密/登录限速） | USR/HUB | ✅（`hub/auth.py`：sqlite 用户表、角色、token 版本、锁定） | — | — | — |
| C2 | C2 | 用户组（group + member 表，组级策略挂载点） | ✅（EP-2-1：`groups`+`group_members`+`policies` 三表迁移；`GroupPolicyStore` CRUD；admin API `/api/hub/admin/groups*` 与 `/policies*` 全套） | ✅ | Ph2（已落） | — |
| C3 | C3 | OIDC SSO（企业 IdP：Keycloak/AD/Authing；JIT 建号；组映射） | ✅（EP-2-2：授权码流 + userinfo 后信道（免 JWT 验签依赖）；JIT 建号 + `source='oidc'` 组全量同步（IdP 移除即生效）；本地 disabled 拒登录；admin settings 配 issuer/client/claims；本地账密登录保留降级） | ✅ | Ph2（已落） | — |
| C4 | LDAP 直连 | GLM | ❌ | P2 | backlog | 中 |
| C5 | SCIM 自动回收（离职联动） | GLM | ❌ | P3 | backlog | 中 |
| C6 | 组织层级（租户→部门→团队四级） | GLM | ❌（扁平 group 起步） | P3 | backlog | 中 |
| C7 | PAT 细粒度作用域（scoped token 只能调某 Agent/某 API 组） | GLM | 🟡（runtime 曾有 owner/collaborator/viewer 三级 token，#180 已实现基础层） | P2 | Ph2 | 中 |
| C8 | 委托/临时授权 | GLM | ❌ | P3 | backlog | 低 |

## D. RBAC 资源粒度

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| D1 | 角色→控制台能力（=B1/B2，先行切片） | USR | ✅（随 B1/B2 交付） | P0 | Ph0 | 低 |
| D2 | 按用户/组控制 Agent 访问 | HUB | ✅（Agent=Hub 模板：实例化端点接 `agent_template:<id>` 组策略门（deny 优先，403+审计 `template.instantiate_denied`）；`acl/resource_policies.py` + 测试 9 例） | P1 | Ph2 | **高** |
| D3 | 按用户/组控制 Skill / MCP / Channel 访问 | HUB | ✅（hub 侧：`skill:/mcp:/channel:` 组策略 → 每属主 `QWENPAW_RESOURCE_BASELINE_JSON` 白名单（凭据面注入，热更）；runtime 侧：`app/resource_baseline.py` 解析 + channels 注册表交集 + 技能预载过滤 + `resource_allowed()` 门） | P1 | Ph2 | 高 |
| D4 | D4 | 策略引擎最小实现（静态半边 = 有序规则表 + overlay 已随 Ph0 落地；组级扩展（groups/policies 表求值）仍 Ph2，见 05 §7） | ✅（动态+静态全落：`AclEngine.decide` 前置 policies 求值——user>group>role、同路径 deny 优先、fail-closed 默认表兜底；`menu:*/agent:*/model:*` 资源类型已建模、代理层不消费） | ✅ | Ph2（已落） | — |
| D5 | Agent/Skill 上架审批流 | GLM | ❌（市场有安装，无审批） | P2 | Ph2 | 中 |
| D6 | 多租户共享 Agent/Skill 商店（组织级发布/分享） | #7318 社区(rerbin) | ❌ | P3 | backlog | 中 |

## E. 模型统一治理

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| E1 | 管理员集中配置 Provider/Endpoint/Key | HUB | ✅（`hub/model_catalog/store.py` Fernet + admin CRUD + test-connection；04 §7） | **P0** | Ph1 | **高**（官方"Central model governance"在列） |
| E2 | 用户只见批准的模型目录/别名，不见凭据与 Endpoint | HUB/USR | ✅（服务端强制：代理 GET /api/models 目录过滤 + 密钥永不回显 `api_key_set`；@c0f5174a） | P0 | Ph1 | 高 |
| E3 | 首启 bootstrap：新租户 runtime 自动拿到可用默认模型 | HUB | ✅（`QWENPAW_MODEL_BOOTSTRAP_JSON` env + re-sync 钩子，04 §7；真机 E2E 过） | P0 | Ph1 | 高 |
| E4 | E4 | 默认模型与按用途路由（编码→强模型，闲聊→轻模型） | ✅（模型路由策略：`model:<id>`/`model:*` 策略在成员激活 `PUT /api/models/active` 时代理内生效——catalog 门之后二次收口；deny 优先（对齐引擎/B5）；无策略沿用 catalog 判定；403 `MODEL_FORBIDDEN_BY_POLICY` 带目标模型） | ✅ | Ph2（已落） | — |
| E5 | 故障切换/fallback 链 | HUB | ✅（消费面已接：`ModelGateway.call` 按 admin 定义链逐跳重试（每跳全量 reserve+open 记账），seen-set 防跨模型环，成功响应带 `X-QwenPaw-Fallback` 头 + `qwenpaw_hub_model_fallback_total` 指标；链配置热读（admin PUT 即生效）；测试 `test_e5_failover.py` 4 例） | P2 | Ph2 | 高 |
| E6 | 限流与并发控制 | HUB | ✅（`ratelimit.py` 令牌桶+并发槽；代理门链 429+Retry-After+`ratelimit.exceeded` 审计+`qwenpaw_hub_rate_limited_total` 指标，finally 释放槽位） | P2 | Ph2（已落） | 高 |
| E7 | E7 | 额度与成本控制（预算/熔断，与 F 区配额联动） | ✅（成本核算：单价表存模型扩展（`input/output_per_mtok`+currency，admin PUT 入审计）；`GET /admin/usage/costs` 按模型计价（MTok 单价 × usage 汇总）+ 按组汇总（tenant→组映射，无组落 `(ungrouped)`）+ 多币种合计 + `unpriced_models` 明示；读取入审计） | ✅ | Ph2（已落） | — |
| E8 | Key 轮换机制 | GLM | ✅（双通道：① provider key `POST .../providers/{id}/rotate-key`——新 key preflight 探测（GET /models）通过才落库，失败 409 保旧 key（fail-closed）+ 审计；② runtime internal token `POST .../runtimes/{id}/rotate-token`——vault 新值+PREVIOUS 双值，graph 推送 401 时宽限回退旧值，runtime 重启即全切 + 审计；流程手册 runbook-key-rotation） | P2 | Ph2（已落） | 中 |
| E9 | E9 | 模型→RBAC 交叉（不同组可见不同模型子集） | ✅（`GET /api/hub/models` 用户面目录：enabled 目录 × 调用者组/用户 `model:*` 策略过滤；**可见性≡可激活**（与 E4 同一 `_model_policies_allow` 判定，deny 优先），目录不显代理会拒的模型；admin 目录端点不受影响） | ✅ | Ph2（已落） | — |
| E10 | 计费精度到对话/Agent 级 | GLM | ❌ | P3 | backlog | 中 |

## F. 用量与可观测

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| F1 | per-user token/成本统计 | HUB | ✅ token 维度（`usage/collector+store` + admin 用量页 by_user/by_model/by_date + 真机对账；**成本估算 ☐ Ph2** 依赖单价表，见 07 §7） | P1 | Ph1 | 高 |
| F2 | 计量采集点（**架构偏差：拉取式**——hub 每 60s 拉 runtime `/api/token-usage/details`，零 runtime patch，代理层不解析 SSE 原则保持） | AUD | ✅（07 §7 偏差说明 + `usage_counters` 幂等快照表） | P1 | Ph1 | 中 |
| F3 | 配额软硬双阈值（80% 告警 / 100% 熔断，hub 代理前置检查） | HUB/GLM | ✅（QuotaEngine 软/硬双阈值，代理前置 403 QUOTA_EXCEEDED；软阈值 X-QwenPaw-Quota-Warning 响应头+审计+指标；`/admin/quota` 管理；14 测试） | P1 | Ph2（已落） | 高 |
| F4 | Prometheus 指标导出（hub `/metrics`） | GLM | ✅（`metrics.py` exposition + `GET /api/hub/metrics`（require_user）+ `deploy/prometheus/qwenpaw-alerts.yaml` 告警样例；9 测试） | P2 | Ph2（已落） | 中 |
| F5 | F5 | OpenTelemetry trace | ✅（W3C tracecontext：`trace.py` 增 `sanitize/traceparent` 解析（version-00 严格校验，畸形即弃）+ trace-id 回退映射；代理转发注入 `traceparent` 下游（runtime OTel SDK 可接）；`X-QwenPaw-Trace-Id` 贯穿保持。**OTLP 全家桶不引入**——零依赖原则下的显式取舍，hub 侧 trace 已全程贯穿） | ✅ | Ph2（已落） | — |
| F6 | 审计事件结构化（who/what/when/allow-deny/reason，落 operations store 扩展表） | GLM/AUD | 🟡（`hub_audit_events` 五要素已落（actor/action/resource/outcome/correlation_id）；acl_denied 已有、quota 预留字段 ☐——**EP-1-5 2026-09-14 降级并入 EP-2-3 配额票**实施） | P1 | Ph1（残留）/Ph2（quota 字段） | 中 |
| F7 | 运行时日志按租户留存与检索 | HUB | ✅（拉取式尾部留存：`hub/runtime_logs.py` RuntimeLogCollector 仿 EP-1-4（5min 拉 `/api/debug/backend-logs` 尾 500 行，internal token 通道），滚动保留 48 快照/runtime + sha256 去重；检索= `GET /api/hub/admin/runtimes/{id}/logs`（admin）；**边界诚实**：尾部窗口留存非日志管道（Loki/ELK 外置，07 §7）） | P2 | Ph2（已落） | 中 |
| F8 | 健康状态面板（runtime 起停/资源，admin 页已有骨架） | HUB | 🟡（overview/runtimes 列表 + 起停随 hub 交付；**资源粒度深化 2026-09-14 归置 Ph2**——与 EP-2-4 metrics 指标源共用管线，无独立 Ph1 票故显式改期） | P1 | **Ph2** | 低 |
| F9 | F9 | SIEM 对接/日志外送 | ✅（`hub/siem.py` SiemRelay：审计事件 JSONL 批量外送 webhook（batch_size/flush_interval/secret 头可配）；fire-and-forget 不阻塞请求路径，失败计数+last_error 可观测（`GET/PUT /admin/siem`）；H2 链不依赖 relay 存活） | ✅ | Ph2（已落） | — |

## G. 运行时与隔离

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| G1 | Runtime Provisioner 第三实现：K8s（per-tenant Pod） | HUB | ✅（六方法 + 14 单测 + kind 验收四项（Pod/PVC/Service/stop 保会话/fail-closed）；06 §7.1） | P1 | Ph1 | **高** |
| G2 | G2 | 能力协商协议（requirement ⊆ capability 才调度；schema 借 `SandboxCapability`） | ✅（`hub/capability.py`：`RuntimeCapability`（version/sandbox[借 SandboxCapability 形状]/tools，metadata 往返）+ `CapabilityRequirement` + `negotiate()`（requirement ⊆ capability：**数值序**版本比较、沙箱必选、工具子集）；注册无门记录能力集（可观测），**start 端点协商调度**：不满足 → 409 `CAPABILITY_MISMATCH` + missing 明细 + 审计 failure；admin `GET/PUT /runtime-requirements` 热设要求（入审计）；runtime payload 透出 capabilities） | ✅ | Ph2（已落） | — |
| G3 | 拒绝启动而非降级（fail-closed）+ 硬拒绝/软降级区分 | HUB/GLM | ✅（preflight fail-closed（Ph1）+ G2 协商门：requirement ⊄ capability → 409 CAPABILITY_MISMATCH + missing 明细 + 审计，硬拒绝语义全程无静默降级） | P1 | Ph1+Ph2（已落） | 中 |
| G4 | gVisor/Kata/MicroVM 后端 | HUB | ✅（`SandboxMode.CONTAINER`：docker run/exec/rm，`platform_hints[container_runtime]` 直通 `--runtime`（gVisor/Kata 零代码切换），内存/pids 为真实 cgroup 限额；live 验收套真 daemon 证明隔离属性（2026-09-17，`aee532b8`/`5255f348`）） | ✅ | Ph3（已提前落） | — |
| G5 | 远程 runtime 后端（跨机） | HUB | ❌ | P3 | backlog | 中 |
| G6 | per-tenant 运行时池与资源上限（Docker 已有 limits，K8s 用 quotas/limits） | HUB/GLM | ✅（三层齐备：① per-pod——chart `runtimes.resources` → `QWENPAW_HUB_K8S_{CPU,MEMORY}_{REQUEST,LIMIT}` env → provisioner configure → 容器 resources（既有）；② ns 级——`runtime-quota.yaml` 新模板：ResourceQuota（pods/cpu/memory/storage 聚合上限）+ LimitRange（兜底注入 default requests/limits，四键成对校验）；③ kind 实测：quota `pods: 0/8, requests.cpu: 0/8` 就位、LimitRange default 250m/512Mi~1/2Gi、hub healthz 200。per-tenant **差异化档位**留 Ph3（当前全局一档+ns 护栏，够企业起步）） | P1 | Ph1（已落） | 高 |
| G7 | G7 | 常驻 Agent Pod + 按需沙箱 Job 两级执行（K8s 场景沙箱不逐调用启 Pod） | ✅（两级执行：常驻 agent Pod（现状不动）+ `sandbox_job_manifest()` 按需沙箱 Job——batch/v1，默认加固（non-root/drop ALL/禁提权/只读 rootfs+tmp emptyDir）、TTL 自清、activeDeadline 上限、backoff 0；provisioner `launch_sandbox_job` 派发；端点 `POST /runtimes/{id}/sandbox-jobs`（校验+审计+501 优雅降级）） | ✅ | Ph2（已落） | — |

## H. 数据治理与合规

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| H1 | 记忆/知识三档共享（个人私有 / 部门共享 / 租户公共） | #7318 社区(Marlin-Phone/ysf7762) | ❌（每 agent 记忆独立） | P2 | Ph2-3 | 中 |
| H2 | H2 | 审计日志 append-only/防篡改 | ✅（无票据直落：`hub_audit_events` 加 `prev_hash/row_hash` 链式 SHA-256（全字段参与 canonical JSON）；`BEGIN IMMEDIATE` 内取头-算哈希-插入原子；存量行幂等补链；`verify_chain()` 全walk 报断链位置/原因；admin 端点 `/audit/verify` + `/audit/chain-head`（外部锚定用）。边界如实：链检测篡改/删行/重排，整库重算级攻击需配合 chain-head 外部锚定（备份手册已含离线副本建议）） | ✅ | Ph2（已落） | — |
| H3 | H3 | 审计留存周期与导出接口 | ✅（无票据直落：`GET /audit/export` JSONL 流式导出（含 prev/row_hash 可离线校验）；`POST /audit/prune` **先归档后删**（JSONL 落 hub root + 被裁段尾哈希入 `audit_chain_archives` 锚点表 + 剩余链 fresh-genesis 重哈希续链）；`GET /audit/archives` 锚点清单；prune 自身入审计；留存节奏由运维 cron 驱动（默认不自动删）） | ✅ | Ph2（已落） | — |
| H4 | 数据驻留（多地域不跨区） | GLM | ❌ | P3 | backlog | 低 |
| H5 | 用户数据导出/删除（GDPR 式） | GLM | ❌ | P3 | backlog | 中 |

## I. 交付与环境管理

| ID | 需求 | 来源 | 状态 | 优先级 | 阶段 | 撞车 |
|---|---|---|---|---|---|---|
| I1 | fork 工程化：分支/基线 tag/CI 跑通上游测试 | USR | ✅（enterprise-ci.yml hub+console 两 job、fork-verify、baseline tag、docs/enterprise 全套；CI 三绿常态） | P0 | Ph0 | — |
| I2 | I2 | 环境分层 dev/staging/prod（Helm values 分档） | ✅（EP-2-10：`values-{dev,staging,prod}.yaml` 三档——dev NodePort/低资源/注册开，staging 生产同形+日备，prod 高资源+18:00 日备+三冒烟门；渲染验证 ×14 资源） | ✅ | Ph2（已落） | — |
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

## M. 市场能力（应用/技能/插件；详见 24 号档）

> 调研结论：市场服务端闭源（platform.agentscope.io），无源码可
> 私有化；插件 CDN 目录链路与生成器开源、可自托管。

| ID | 需求 | 来源 | 状态（证据） |
|---|---|---|---|
| M1 | 插件清单格式与生成器（自产条目） | 24 §2② | 🟡 开源（`scripts/pack/*`、`plugins/*/plugin.json`） |
| M2 | 插件安装/下载（CDN 链路自托管） | 24 §2② | ✅（`PLUGIN_DOWNLOAD_CDN` 支持 `QWENPAW_PLUGIN_DOWNLOAD_CDN` env 覆盖——内网镜像指 env 即用；官方 CDN 保底默认） |
| M3 | 应用/插件市场服务端（搜索/账号/发布） | 24 §2① | ❌ 官方闭源无源码；内网可仿 EP-2-19 模板市场自建 |
| M4 | 技能市场 provider 接入 | 24 §2③ | 🟡 4 provider；platform/clawhub 闭源、modelscope 可自托管 |
| M5 | 市场供应链安全（sha256/签名/缓存） | 24 §5 | ❌ 安装路径无摘要校验，内网源启用前必须补 |

---

## 台账维护规则

1. 新需求先登记（编号顺延），经"现状核查 + 撞车评估"后再入阶段；
2. 每阶段启动时复核一次撞车列（对表官方 roadmap / 近期 PR）；
3. 完成的需求在状态列标 ✅ 并链接落地 PR/commit；
4. 降级/否决的需求保留行（含理由），避免重复评估。
