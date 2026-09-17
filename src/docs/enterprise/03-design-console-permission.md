# 03 · 设计：控制台能力权限（Phase 0）

> 对应需求：B1-B5 ｜ 难度 ★★ ｜ 预估 1-2 周
> 原则：**服务端强制（hub 代理层）为主，菜单过滤只是 UX**。菜单可见性不承担安全职责。

## 1. 目标与非目标

**目标**
1. `user` 角色的租户在 console 看不到管理页面（含路由直达拦截）。
   **v1.3 修订（2026-09-14，@c9aa6890）**：裁剪粒度从"菜单组整组隐藏"细化为
   **精确 route 级 + 组自动折叠**——`user` 保留文件/检查点/定时任务/技能/技能池/
   工具/Token 用量七个用户面页面（其 API 已按用户面放行），仅隐藏 18 个管理 route
   （渠道/会话/心跳/ACP/Agent 配置/统计/MCP/设置中心/模型/环境/…，见 console_map.py）；
2. `user` 角色对管理面 API 的直连请求（绕过 UI 直接 curl）在 hub 代理层得到 403；
3. `admin` 角色行为不变；
4. 白名单/黑名单以**配置文件**维护，不改代码即可调整分组。

**非目标**
- runtime（本体）侧不加角色（保持对上游零侵入；直连 runtime 场景见 B6，Phase 2）；
- 资源粒度 RBAC（Agent/Skill 级，D 区，Phase 2）；
- 菜单白名单的按租户定制（B5，Phase 2）。

## 2. 架构

```text
console(浏览器)                          Hub 控制面                         runtime
  │  GET /api/hub/me/permissions ───►  control_app（独立鉴权端点）
  │      ◄── { role, denied_groups:[], denied_routes:[...18 项],
  │             model_readonly: true }
  │      （v1.2 修订：由公开的 /api/version 迁至独立鉴权端点——按角色下发
  │       必须鉴权；OS 桌面 dock 与 Sidebar 同规则过滤 @61663a62/c9aa6890）
  │
  │  Sidebar/Route/dock 按 denied_routes 过滤 visible
  │  （直接输 /settings → 重定向 /chat）
  │
  │  任意 /api/xxx ─────────────────►  personal_runtime_proxy
  │                                     ├─ ACL 策略引擎（新增 hub/acl/）
  │                                     │   user + 管理面 → 403（含 WS 1008）
  │                                     └─ 放行 → 注入 Runtime-Token ──► runtime
```

## 3. API 能力分组（初稿，EP-0-1 定稿）

基于 `src/qwenpaw/app/routers/` 全量 40 个 router 的初稿分组：

**对话面（user 默认放行）**
`auth` `healthz` `version` `messages`（会话消息）`agent_scoped`（app-scoped chat）
`agents`（列表/状态——切换器与 PawApp 需要；**写操作单独归类**，见下）
`agent_status` `token_usage`（个人用量只读）`approval`（用户审批流）
`tool_calls`（对话内工具调用状态）`skills_stream`（对话内技能流）
`pawapps`（应用运行；安装/卸载归管理面）`market`（浏览；写操作归管理面）
`frontend_plugin`（静态资源，本就在 `_PUBLIC_PREFIXES`）

**管理面（user 默认拒绝）**
`config` `settings`（写）`envs` `providers` `provider_oauth` `local_models`
`plugins`（安装/卸载/更新）`backup` `checkpoints` `git` `fork`
`files` `workspace`（文件工作区）`project_directory`
`access_control` `mail_access_control`（渠道 ACL）
`mcp` `mcp_oauth`（MCP 服务治理）`coding_mode` `loops` `harnesses`
`schemas_config` `tools`（工具治理）`skills`（技能管理）
`agents`（写操作：创建/删除/重排/启停配置）`market`/`pawapps`（写操作）
`console` `agent_stats`（管理统计）

**待 EP-0-1 盘点定稿的灰区**
- `agents` 路由含读（列表，放行）与写（管理）→ 需要方法级规则，不只路径前缀；
- `settings` 中 `language`/`upload-limit` 已是 `_PUBLIC_PATHS`，跟随放行；
- cron-jobs（定时任务）归管理面还是对话面 → 建议：查看放行、创建/修改拒绝（初版整体拒绝亦可）；
- `core.import`（pawport 整仓导入，v2.2.1 新增菜单+路由）→ **建议归管理面**
  （导入内容含 settings/plugins/projects，普通用户不应自助迁移工作区）。

## 4. 实施设计

### 4.1 策略引擎（新增 `src/qwenpaw/hub/acl/`，零侵入）

> **v1.3 修订（2026-09-14）**：本节原稿的 `groups.py`/`config.py` + `groups/roles`
> JSON 结构为设计初稿，**实际实现走了更简单的形态**（与附录 B 的 overlay 语义
> 一致，原稿与附录 B 存在自相矛盾，已修正）：

```text
hub/acl/
  __init__.py       # 公共 API
  engine.py         # AclEngine.decide(role, method, path) → Decision(allow|deny, reason)
  rules.py          # 有序规则表（RuleSpec: effect/pattern/methods/name），默认表内置
  console_map.py    # 角色 → denied_groups/denied_routes/model_readonly 下发载荷
```

`acl.json`（落 hub 数据目录，admin 可编辑，**平铺 overlay**，语义见附录 B）：

```json
{
  "version": 1,
  "rules": [
    {"role": "user", "method": "GET", "pattern": "^/api/example", "effect": "deny"}
  ]
}
```

语义：内置有序规则表（先 deny 管理子树，再 allow 用户面，方法级精确）+ overlay
逐条插入表头；未匹配 → **fail-closed（deny）**。默认表内规则带稳定 `name`
（审计 reason 引用）；组级 groups/roles 结构留待 Phase 2（EP-2-1 policies 求值）。

### 4.2 代理层接入（对上游文件的**最小修改**，两处）✅ 已实现

1. `hub/control_app.py` `personal_runtime_proxy`：函数入口、`ensure_personal_runtime`
   **之前**插入 `app.state.acl.decide(user.role, request.method, request.url.path)`
   （denied 请求不触发 runtime 供给）；deny → `HTTPException(403,
   detail={code:"ACL_DENIED", message, reason})` 并写审计事件 `acl.denied`。
2. `hub/control_app.py` websocket 入口（`@app.websocket("/api/{path:path}")`）：
   token 校验后同一 `decide(role, "WS", path)`，deny → close(code=1008) + 审计。

引擎初始化：`create_hub_app` 内 `app.state.acl = AclEngine.from_env(
config_dir=<hub root>)`——默认读 `<hub root>/acl.json` 覆盖层（mtime 热加载），
`QWENPAW_HUB_ACL_CONFIG` 可显式指定路径。

### 4.3 `/api/hub/me/permissions` 下发权限 ✅ 已实现（v1.2 修订）

> 修订：原方案挂 `/api/version`，实现时改为 **`GET /api/hub/me/permissions`**
> （`require_user` 保护）。理由：console 的 hub 模式探测来自 `/api/auth/status`
> （`console/src/auth/gate.ts`），`/api/version` 是公开安全端点且无认证上下文，
> 不适合承载角色信息；紧邻现有 `/api/hub/me` 更自然。

响应由 `hub/acl/console_map.py` 的 `permissions_payload(role)` 生成，四键：
`{"role","denied_groups","denied_routes","model_readonly"}`（`model_readonly`
为 EP-1-3 追加：目录只读标记，console 据此隐藏模型配置入口）。`denied_routes` 与
`console/src/layouts/registry/builtinRoutes.tsx` 的 `core.*` id 对齐；上游新增
菜单时在 EP 例程中同步本映射（纯数据文件，零逻辑改动）。

### 4.4 console 侧改动 ✅ 已实现（v1.1 方案：复用上游 capabilities 过滤管线）

> 修订背景：v2.2.1 后上游新增 `console/src/layouts/registry/capabilities.ts`
> （`filterMenuForAgentCapabilities`，按 agent 后端能力过滤内置菜单），已在
> `Sidebar.tsx:143` 与 `SettingsCenter/useSidebarEntryGroups.ts:28` 两个调用点生效。
> **菜单按"能力"过滤的管线已存在**——我们的角色权限过滤改为在同一管线组合，
> 与上游演进同构，rebase 成本显著低于逐条改 `visible` 钩子。

1. `console/src/App.tsx`：`backendInfo` 解析新增 `permissions` 字段（默认空 = 不过滤，
   兼容非 hub 部署与旧后端）；
2. 新增 `console/src/layouts/registry/permissions.ts`（与 `capabilities.ts` 同目录同风格）：
   `filterMenuForPermissions(items, permissions)`，按 `denied_routes`/`denied_groups`
   过滤菜单树（含整组折叠：组内全部 denied 则隐藏组头）；
3. 组合点改为**两处调用点各加一层**：
   `Sidebar.tsx` 的 `filterMenuForAgentCapabilities(rawAgentMenu, caps)` 外层再包
   `filterMenuForPermissions(..., permissions)`；`useSidebarEntryGroups.ts` 同理——
   **设置中心有独立导航，必须两处都挂**（上游能力过滤正是这么做的）；
4. 路由直达拦截：`MainLayout` 路由解析处对 denied path 做 `<Navigate to="/chat" replace/>`
   （`builtinRoutes` 的 `core.*` id 映射由 4.3 下发）。

> console 是同一 bundle 服务所有角色——**前端过滤仅 UX**，安全由 4.2 保证。bundle 不分角色构建。
> `NATIVE_WORKSPACE_MENU_IDS`（capabilities.ts 内的工作区菜单 id 集）可作为 denied_routes
> 映射的参照基线：workspace/import/acp/agent-config/agent-stats 是上游自己圈定的"工作区面"。

### 4.5 兼容与降级

- 非 hub 部署（直连 runtime）：无 `/api/hub/me/permissions` → console 全量菜单，行为与上游一致；
- `acl.json` 缺失/损坏 → 使用内置默认规则表（`rules.py` 常量），**deny 管理面**（fail-closed，
  损坏文件记 warning 后忽略，绝不 fail-open）；
- hub 升级替换 `control_app.py`：ACL 接入为 3 个小块（import、app.state 初始化、proxy/WS 各一段），
  rebase 时重挂即可。

## 5. 测试计划

> 2026-09-14 刷新：单测 23 函数 + 51 参数化实例（原 67 例，随用户面放行扩展
> 增长）；集成 7 用例（原 5，新增 member 模型切换目录校验、models 列表
> catalog 过滤两例）。

| 层 | 用例 | 状态 |
|---|---|---|
| 单测（tests/unit/hub/test_acl.py） | 规则匹配、方法级、fail-closed、acl.json 覆盖层（含损坏回退/热加载）、路径归一化（`..`、%2e%2e、多斜杠、控制字符、根逃逸）、admin 旁路、菜单 payload | ✅ 67 用例全绿 |
| 集成（control_app 既有套件） | 上游 hub 套件 209 通过（含代理重建、启停恢复）；仅 2 处 member 探针 `/api/probe` 改为 `/api/agents`（见 09 白名单备注） | ✅（1 例 seatbelt 环境性失败与本次无关，干净树同败） |
| 403/1008 专项（EP-0-6） | user→admin 面 API 403 + `acl.denied` 审计；WS 1008；`/api/hub/me/permissions` 正确 | ✅ tests/unit/hub/test_acl_integration.py 5 用例（含 acl.json 覆盖层端到端） |
| console（vitest，EP-0-5） | denied_routes 下菜单不渲染；直达 `/settings` 重定向 `/chat`；无 permissions 全量渲染 | ✅ permissions.test.ts 9 用例（过滤器/路由守卫/降级），tsc+eslint 过 |

## 6. 交付物清单

- [x] `hub/acl/` 新模块（rules/engine/console_map）+ 67 单测
- [x] `control_app.py` 接入（HTTP 403 / WS 1008 / 审计事件）
- [x] `/api/hub/me/permissions` 端点 + 角色映射（console_map.py）
- [x] console 侧 permissions 过滤（EP-0-5）：registry/permissions.ts + hubPermissionsStore + 两处组合点 + MainLayout 路由守卫 + 9 vitest 用例
- [x] `acl.json.example`（docs/enterprise/examples/）+ 附录 B 运维说明
- [x] EP-0-1 盘点报告 → 本文件附录 A

## 附录 A · EP-0-1 分组定稿（983b3ceb 实测 39 个路由文件；2026-09-14 v1.3 修订）

> 数据来源：`src/qwenpaw/app/routers/` 逐文件提取（现 40 个含路由文件）；
> **补充口径**：规则 17/18d/18g/18h 的来源是 routers/ 目录之外的挂载
> （`app/chats/api.py`、`app/crons/api.py`、`agents/commands` 等），初版盘点
> 未覆盖，v1.3 已纳入。机器可读形态 = `hub/acl/rules.py`（规则的稳定锚点
> 是 `name` 字段——审计 reason 引用它；注释编号仅编排用，与规则增删不同步）。

**对话面（user 放行）**

| 路由 | 前缀 | 关键端点 | 归组理由 |
|---|---|---|---|
| console | `/console` | POST chat、chat/stop、upload；GET push-messages、inbox | **主对话入口**（`agent.ts:49`）；仅 debug 子树拒绝 |
| agents（读） | `/agents` | GET 列表/详情/memory-backends | 会话切换器与 PawApp 列表必需；写操作按方法拒绝 |
| agent_scoped（对话子树） | `/agents/{id}` | console/chats/chat/agent-status 子路由 | **PawApp 对话面**（`agent_scoped.py` 复合路由）；其余子树全拒 |
| agent_status | `/agent-status` | GET | 会话状态展示 |
| approval | `/approval` | GET list；POST approve/deny | 用户侧审批流 |
| token_usage | `/token-usage` | GET（含 /details） | 个人用量只读 |
| tool_calls | `/tool-calls` | GET 会话内工具状态；POST cancel/offload | 对话内工具卡片交互 |
| pawapps（读） | `/pawapps` | GET 列表/详情/settings/static | 用户打开已安装应用；DELETE/写拒 |
| market | `/market` | GET providers/categories；POST search | 浏览市场（安装走 `/plugins`，默认拒） |
| frontend_plugin | `/frontend_plugin` | GET 静态资源 | 本就是 `_PUBLIC_PREFIXES` |
| settings（个人项） | `/settings` | GET/PUT language；GET upload-limit | 与 runtime `_PUBLIC_PATHS` 对齐；offload-policy 拒 |
| config（个人项） | `/config` | GET/PUT user-timezone | cron 编辑器与 agent locale 保存目标（规则 15b） |
| models（目录内） | `/models` | GET 列表（代理按目录过滤）；GET/PUT active | 切换模型是对话必需的使用行为而非配置（规则 16/16b，@f245f00b/c0f5174a；custom-providers/openrouter 发现子树仍拒） |
| chats | `/chats` | GET 会话列表/详情 | 会话侧栏数据源（规则 17，@d01e31b4） |
| coding-mode | `/coding-mode` | GET | 对话面的 coding 模式状态（规则 18，@d01e31b4） |
| files/preview | `/files/preview` | GET | 会话内文件预览（规则 18b；其余 /api/files 子树仍拒） |
| workspace | `/workspace` | `/workspace/*` 整树（checkpoints/git/project-directory 子路由） | 会员文件工作区（规则 19，@ef2c947d） |
| cron | `/cron` | 用户自己的定时任务 | 规则 18d（@ef2c947d）；agent-scoped `cron` 管理子树仍拒 |
| tools | `/tools` | GET + PATCH/POST `/{id}/toggle` | chat 工具卡数据源（规则 18e；console 发 PATCH） |
| harnesses | `/harnesses` | GET | codex/qoder 状态展示（规则 18f） |
| commands | `/commands` | 命令面板 | 斜杠命令补全数据源（规则 18g） |
| agent（读） | `/agent` | GET | 常驻 agent 探测（规则 18h；`admin|shutdown` 子树显式先拒） |
| loops | `/loops` | GET | loop 模式列表（规则 20；写/管理拒） |
| skills | `/skills` | 整树（`/skills/ai` 子树显式拒） | 技能管理页（规则 21） |
| auth/healthz/version | `/auth` 等 | 登录态/探活 | hub 多数已自行拦截；直连场景兜底 |

**管理面（user 拒绝，fail-closed 默认）**——v1.3 修订：原表多行已部分迁移至
用户面（见对话面新行），下表为**当前仍拒**的净管理面：

| 路由 | 前缀 | 备注 |
|---|---|---|
| config（42 端点）、envs | `/config` `/envs` | 渠道/环境治理（user-timezone 已放行见对话面；coding_mode GET 已放行） |
| providers、provider_oauth、local_models | `/providers` `/local-models` | 模型供应商治理（`/models` 面已目录内放行，见对话面） |
| files（除 preview）、fork | `/files`（余） `/fork` | 文件管理写面（preview GET 与 `/workspace/*` 已放行，见对话面） |
| plugins、portability_imports | `/plugins` `/portability/imports` | 安装/整仓导入（`core.import`） |
| mcp、mcp_oauth、skills_stream、agent_scoped 管理子树 | 各自前缀 | MCP 治理/AI 技能/agent 级管理（skills、tools GET、harnesses GET、loops GET 已放行，见对话面） |
| access_control、mail_access_control、backup、agent_stats | 各自前缀 | 渠道 ACL/备份/统计 |
| messages、voice | `/messages` `/voice` | 主动外发消息、语音通道（WS 同拒） |
| agents 写 | `PUT/PATCH/POST/DELETE /agents*` | 方法级规则（order/pin/backend-settings/删除；agent GET 已放行） |
| console/debug | `GET /console/debug/*` | 后端日志泄露面 |

**灰区裁决记录**（原 4 项全部闭环）：

1. agents 读写分离 → 方法级 deny（`agents.write`）+ scope 子树白名单（`agents.scoped-chat`）；
2. settings 公共路径 → 仅 language/upload-limit 放行，PUT language 属用户偏好放行；
3. cron-jobs → 初版归管理面；**2026-09-13 裁决推翻**：top-level `/api/cron`
   是用户自己的定时任务（用户面，规则 18d 放行），`GET /api/loops` 同步放行
   （规则 20）；仅 agent-scoped `cron` 管理子树维持拒绝；
4. core.import/pawport → 归管理面（导入含 settings/plugins/projects 全量）。

## 附录 B · 运维说明（acl.json 覆盖层）

**放置与生效**

- 默认路径：`<hub root>/acl.json`（hub root 即 `QWENPAW_HUB_DIR`，缺省 `~/.qwenpaw/hub`）；
- 或环境变量 `QWENPAW_HUB_ACL_CONFIG` 指定任意绝对路径（优先级更高）；
- mtime 变化即热加载，无需重启；每写一个完整 JSON 再原子替换（写临时文件后 mv）。

**规则语义**

```json
{"rules": [{"name": "...", "effect": "allow|deny", "pattern": "^/api/...", "methods": ["GET"]}]}
```

- 覆盖层规则**先于**内置规则评估（可用来给 user 开一个管理面只读口，或反向收紧对话面）；
- 仅影响非 admin 角色；admin 恒放行；
- `pattern` 为 Python 正则（re.match 语义，锚定开头）；`methods` 缺省=全部方法（含 WS 伪方法）；
- 全部落空 → 回到内置规则 → 仍未匹配 → 拒绝。**任何配置错误都不会 fail-open**。

**审计与排障**

- 每次拒绝写一条 `acl.denied` 审计事件（action 可筛），detail 含 `reason`（规则名）与 `method`；
- 客户端收到的 403 body：`detail.code="ACL_DENIED"`、`detail.reason=规则名`——
  `default-deny` 表示未匹配任何规则（新上游接口默认拒绝，需评估后在覆盖层或内置规则中放行）；
- console 侧菜单过滤失败（如旧后端无 `/api/hub/me/permissions`）只影响 UX：后端 403 不受影响。

**常见处置**

| 症状 | 处置 |
|---|---|
| 升级后 user 某新功能 403（reason=default-deny） | 评估该 API 归属；确属对话面则在覆盖层放行并反馈到 03 附录 A 更新内置规则 |
| 想临时给某用户组开只读管理面 | 覆盖层 `allow` + `methods:["GET"]`（用户组维度属 Phase 2 RBAC） |
| acl.json 写错 | 日志出现 `Ignoring unreadable ACL config` warning；修复文件即可，期间内置规则持续生效 |
