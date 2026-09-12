# 03 · 设计：控制台能力权限（Phase 0）

> 对应需求：B1-B5 ｜ 难度 ★★ ｜ 预估 1-2 周
> 原则：**服务端强制（hub 代理层）为主，菜单过滤只是 UX**。菜单可见性不承担安全职责。

## 1. 目标与非目标

**目标**
1. `user` 角色的租户在 console 看不到 工作区 / 设置 / 控制 菜单组（含路由直达拦截）；
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
  │  GET /api/version ──────────────►  control_app /api/version
  │      ◄── { mode:"hub", permissions:{ denied_groups:[...] } }
  │
  │  builtinMenu/Sidebar 按 denied_groups 过滤 visible
  │  （Route 层同样过滤：直接输 /settings → 重定向 /chat）
  │
  │  任意 /api/xxx ─────────────────►  personal_runtime_proxy
  │                                     ├─ ACL 策略引擎（新增 hub/acl/）
  │                                     │   user + group=admin面 → 403
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

```text
hub/acl/
  __init__.py       # build_acl_engine(config) → AclEngine
  engine.py         # AclEngine.decide(role, method, path) → Decision(allow|deny, reason)
  groups.py         # 能力分组常量 + 默认分组表（对话面/管理面）
  config.py         # acl.json 加载（覆盖默认分组）、版本、热加载
```

`acl.json`（落 hub 数据目录，admin 可编辑）：

```json
{
  "version": 1,
  "groups": {
    "chat":     {"default": "allow",  "routes": ["messages", "agent_scoped", ...]},
    "admin":    {"default": "deny",   "routes": ["config", "providers", "plugins", ...]}
  },
  "roles": {
    "admin": {"groups": {"chat": "allow", "admin": "allow"}},
    "user":  {"groups": {"chat": "allow", "admin": "deny"}}
  },
  "overrides": [
    {"role": "user", "method": "POST", "pattern": "^/api/agents/", "effect": "deny"}
  ]
}
```

语义：默认分组表 + overrides 逐条覆盖（含方法级）；未匹配 → **fail-closed（deny）**。

### 4.2 代理层接入（对上游文件的**最小修改**，两处）

1. `hub/control_app.py` `personal_runtime_proxy`：进入函数后、构造上游请求前插入
   `decision = acl.decide(user.role, request.method, path)`；deny → `HTTPException(403, detail=...)`
   并写审计事件（reason=acl_denied, group, pattern）。
2. `hub/control_app.py` websocket 入口（`@app.websocket("/api/{path:path}")`，L1421 附近）：
   同一 `AclEngine.decide`，deny → close(code=1008)。

### 4.3 `/api/version` 下发权限（对 hub 已有重写点扩展）

`control_app.py` 的 `/api/version` 响应中，hub 模式追加：

```json
"permissions": {"denied_groups": ["workspace", "settings", "control"],
                 "denied_routes": ["core.files", "core.channels", ...]}
```

生成逻辑：`user.role → groups → 映射到 console 菜单 route id 列表`（映射表与
`console/src/layouts/registry/builtinRoutes.tsx` 的 `core.*` id 对齐，随上游菜单演进在 EP 例程中同步）。

### 4.4 console 侧改动（v1.1 修订：复用上游 capabilities 过滤管线）

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

- 非 hub 部署（直连 runtime）：`/api/version` 无 `permissions` → console 全量菜单，行为与上游一致；
- `acl.json` 缺失/损坏 → 使用内置默认分组表（代码内常量），**deny 管理面**（fail-closed）；
- hub 升级替换 `control_app.py`：ACL 接入点以独立装饰器/依赖注入形式写成
  `hub/acl/dependencies.py`，rebase 时只需重挂一行。

## 5. 测试计划

| 层 | 用例 |
|---|---|
| 单测（hub/acl） | 分组匹配、方法级 override、fail-closed、acl.json 热加载、恶意路径（`/api/config/../messages`）归一化后判定 |
| 集成（control_app） | user→admin 面 API 403 且有审计事件；admin→全放行；WS deny 关闭码 1008；`/api/version` 携带正确 permissions |
| console（vitest） | denied_groups 下菜单不渲染；直接访问 `/settings` 重定向 `/chat`；无 permissions 字段时全量渲染 |
| 回归 | 非 hub 模式 console 全功能；hub admin 全功能 |

## 6. 交付物清单

- [ ] `hub/acl/` 新模块 + 单测
- [ ] `control_app.py` 两处接入（≤30 行改动）
- [ ] `/api/version` permissions 扩展
- [ ] console 三文件改动 + vitest
- [ ] `acl.json.example` + 运维说明（本文档 §3 定稿表同步进 groups.py）
- [ ] EP-0-1 盘点报告（40 router 逐条归组及理由，附在本文件附录）
