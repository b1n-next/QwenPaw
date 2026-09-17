# 11 · Console 前端与 website

> Console 是 QwenPaw 的 Web 控制台（`console/`，约 20 万行 TS/TSX、1011 个文件），同一 bundle 同时服务**单机形态**与 **Hub 多租户形态**（`mode: hub`）。本页讲清它的架构、页面清单、与后端的通信方式。

---

## 1. 技术栈与工程形态

| 维度 | 选型 | 证据 |
|---|---|---|
| 框架 | React 18 + TypeScript 5.8 | `console/package.json` |
| 构建 | Vite 6.4；测试 vitest 4 | `console/package.json` |
| UI 库 | antd 5.29 + `@agentscope-ai/chat/design`（对话组件） | `console/package.json` |
| 状态 | zustand 5（`stores/` 下 20 个 store） | `console/src/stores/` |
| 路由 | react-router-dom 7 | `console/src/App.tsx:388-410` |
| 桌面 | @tauri-apps/api 2.10（Tauri 壳） | `console/src/tauri/` |

包管理 pnpm；console 有独立的 prettier 配置（根仓库 pre-commit 的 prettier 明确排除 `console/`，`.pre-commit-config.yaml:121-127`）。

## 2. 目录职责地图

```text
console/src/
├── pages/        # 页面组件（Chat、Hub、Settings…）
├── stores/       # 20 个 zustand store（会话/设置/插件状态…）
├── layouts/      # MainLayout / Sidebar / registry（菜单与路由注册表）
├── api/          # 请求层：request.ts 封装 + modules/ 40+ 域模块
├── hooks/        # 通用 hooks
├── features/     # files-workspace、project-directory 两大特性模块
├── os/           # 桌面 OS 风 shell：Dock / Launcher / 窗口管理
├── tauri/        # Tauri 桌面后端 bootstrap
├── plugins/      # 前端插件 registry + Slot 插件槽
├── components/   # 通用组件
└── locales/      # i18n
```

## 3. 请求层与通信

- **HTTP**：原生 fetch 封装（`console/src/api/request.ts:74`），默认 30s 超时+可选重试；`config.ts:11-16` 统一加 `/api` 前缀；token 取 localStorage 或构建期注入（`config.ts:23-27`）。API 按域拆分在 `api/modules/`（agent/chat/hub/mcp/skill 等 40+ 模块）。
- **对话流 = SSE**：fetch ReadableStream 消费后端 SSE（`pages/Chat/replayFastForward.ts:2` 注释"replayed SSE streams"）。
- **WebSocket**：console 不直连 WS；语音等 WS 流量在 Hub 形态下经 `hub/websocket_proxy.py` 中继（语音端点 `/api/voice/ws`，`app/_app.py:942`）。
- **轮询**：ConsolePollService + AgentStatusPollingController（`layouts/MainLayout/index.tsx:77-78`）。

## 4. 路由与页面清单

顶层路由（`App.tsx:388-410`）只有三条：`/login`、`/hub/admin`、`/*`（其余经插件 registry `useRoutes()` 在 MainLayout 内渲染，`layouts/MainLayout/index.tsx:27,96-104`）。

核心路由 25+ 条注册于 `layouts/registry/builtinRoutes.tsx:69-122`：

| 类别 | 页面 |
|---|---|
| 对话 | chat |
| 资产 | files、project-directory、sessions、inbox、checkpoints、backups |
| 能力 | skills、skill-pool、tools、mcp、acp、market、imports、apps/:appId |
| 自动化 | cron-jobs、heartbeat |
| 管理 | agents、models、environments、channels、security、token-usage、agent-stats、debug、settings-center |

**Hub 管理台**是独立巨型页 `pages/Hub/index.tsx`（2705 行，挂 `/hub/admin`）：管理用户、runtime、用量、审计。其 API 客户端 `api/modules/hub.ts`（390 行）定义 HubUser/HubRuntime/HubAuditEvent/HubUsageSummary 类型 + `hubApi`（healthz/me/runtimes CRUD/用户管理/用量/审计…，`hub.ts:289` 起）。企业线 EP-2-11 为 `listAuditEvents` 加了 `traceId` 过滤参数（`hub.ts:382-387`）。

## 5. 菜单权限管线（企业线 Phase 0 落点）

上游已有 `layouts/registry/capabilities.ts` 的 `filterMenuForAgentCapabilities`（按 agent 能力过滤菜单）；企业线 EP-0-5 在**同一调用点**（`Sidebar.tsx`、`SettingsCenter/useSidebarEntryGroups.ts`）组合了角色权限过滤（`registry/permissions.ts` + hubPermissionsStore）：服务端 `GET /api/hub/me/permissions` 下发 denied 集合 → 前端过滤菜单 + `/chat` 重定向 + 无 permissions 时全量渲染。**菜单过滤只是 UX，真正的强制点在 hub 代理层**（见 [10-hub-enterprise](10-hub-enterprise.md)）。

## 6. website/（官网文档站）

**不是 Docusaurus**——Vite 6 + React 18 + Tailwind 4 + radix-ui/shadcn 自建静态站：

- 内容 = `public/docs/*.md` 中英双语（intro/architecture/models/context/persona/desktop/roadmap 各 .zh/.en），`src/pages/Docs/`（markdown.ts + navigation.ts）用 react-markdown + rehype-highlight 渲染；
- 搜索 = fuse.js 客户端全文（构建期 `build-search-index.mjs` 生成索引）；
- 页面 = Home / Docs / Downloads / Blog / ReleaseNotes；supabase 仅用于 blog 统计。

---

相关：[02-architecture](02-architecture.md)（消息流）、[10-hub-enterprise](10-hub-enterprise.md)（Hub 形态）、[12-testing-workflow](12-testing-workflow.md)（console vitest 3520 用例）
