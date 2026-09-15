# 02 · 总体架构（Architecture）

> 本页回答三个问题：**系统分几层？一条消息怎么流过去？关键设计决策是什么？**
> 所有结论均给出代码落点，可按图索骥。

---

## 1. 分层总图

```mermaid
flowchart TB
    subgraph Client["客户端层"]
        CONSOLE["Console（React Web）"]
        TUI["TUI（textual 终端）"]
        DESKTOP["桌面端（Tauri）"]
        CHANNELS["聊天渠道（DingTalk/飞书/微信/Discord/TG/iMessage/QQ/Slack/Matrix…）"]
        PAWAPP["PawApp 小应用"]
    end

    subgraph AppLayer["应用服务层 src/qwenpaw/app/（FastAPI）"]
        routers["44 个 Router（chats/messages/files/skills/approval/…）"]
        auth["AuthMiddleware + RuntimeBoundary"]
        MAM["MultiAgentManager（多 Agent 懒加载）"]
        CHM["ChannelManager（渠道适配）"]
        CRON["CronManager（APScheduler）"]
        MAIL["Mail 监控"]
        APPROVALS["ApprovalService"]
    end

    subgraph RuntimeLayer["运行时层 src/qwenpaw/runtime/"]
        RT["Runtime（8 阶段请求编排）"]
        ENV["Envelope（SSE 状态机）"]
        AB["AgentBuilder（每请求组装）"]
        AEX["AgentExecutor（心跳包装）"]
        HOOKS["HookRegistry（生命周期钩子）"]
    end

    subgraph AgentLayer["Agent 内核 src/qwenpaw/agents/ + loop/ + modes/"]
        AG["QwenPawAgent（基于 agentscope Agent）"]
        CTX["Scroll Context（durable history + 驱逐索引 + recall）"]
        TOOLKIT["Toolkit + 内置工具 + Skill 池"]
        GATES["Stop Gates ×8（iteration/budget/timeout/doom/rubric/human…）"]
        SUBAG["子 Agent spawn"]
    end

    subgraph Capability["能力与资源层"]
        PROVIDERS["ProviderManager（14+ 模型厂商 + 本地模型 + 路由/fallback）"]
        DRIVERS["Drivers（MCP/A2A/ACP 连接层 + 凭据保管）"]
        BROWSER["Browser 自动化（Playwright）"]
        SANDBOX["Sandbox（Seatbelt/bubblewrap/Win）"]
        GOV["Governance（allow/deny/ask/sandbox 策略）"]
        GUARD["Tool Guard / Skill Scanner"]
        MEMORY["Memory（三层记忆 + ReMe）"]
    end

    subgraph Persistence["持久化（工作区 ~/.qwenpaw/）"]
        SQLITE[(SQLite：会话历史/审计/usage)]
        MD[(Markdown：记忆/技能/配置)]
        FILES[(项目文件 + checkpoints + backups)]
    end

    Client -->|HTTP/SSE/WS| AppLayer
    routers --> MAM --> RT
    CHM --> RT
    CRON --> RT
    RT --> AB --> AG
    RT --> ENV
    AG --> CTX & TOOLKIT & GATES & SUBAG
    AG --> PROVIDERS
    TOOLKIT --> DRIVERS & BROWSER & SANDBOX
    TOOLKIT --> GUARD --> GOV
    AG --> MEMORY
    CTX --> SQLITE
    MEMORY --> MD
    AG --> FILES
```

## 2. 核心抽象：Workspace = 一个完整的 Agent 运行单元

整个系统的中心抽象是 **Workspace**（`src/qwenpaw/app/workspace/workspace.py:137`）：每个 Agent（默认 Agent、QA Agent、用户自建 Agent）各有一个 Workspace，封装：

| 组件 | 职责 |
|---|---|
| `ChannelManager` | 所有渠道连接与消息进出 |
| `BaseMemoryManager` | 会话记忆 |
| `DriverManager` | 外部能力运行时（MCP 等） |
| `CronManager` | 定时任务 |
| `WorkspacePlugins` | 工具/钩子/命令/prompt 四类注册表 |

请求处理统一交给 **`Runtime`**（每个 Workspace 一个实例）。`MultiAgentManager`（`src/qwenpaw/app/multi_agent_manager.py:36`）负责多 Workspace 的**懒加载、并发启动（信号量限流）、热重载、生命周期**。

## 3. 一条消息的完整生命周期（单机形态）

```mermaid
sequenceDiagram
    participant U as 用户（Console/渠道/TUI）
    participant API as FastAPI Router
    participant WS as Workspace
    participant RT as Runtime（8 阶段）
    participant AG as QwenPawAgent
    participant M as 模型 Provider

    U->>API: POST /api/chats/{id}/send（或渠道 webhook/WS）
    API->>WS: 定位 Workspace（MultiAgentManager 懒加载）
    WS->>RT: runtime.run(AgentRequest)
    RT->>RT: ①PRE_DISPATCH → slash 命令分发（命中则短路）
    RT->>RT: ②POST_DISPATCH → ③PRE_AGENT_BUILD
    RT->>AG: ④AgentBuilder.build()（组装模型/工具/技能/记忆/mode）
    RT->>RT: ⑤POST_AGENT_BUILD → ⑥PRE_EXECUTE
    loop ReAct 循环（每轮后 Stop Gates 评估）
        AG->>M: 模型调用（流式事件）
        AG->>AG: 工具调用（Tool Guard→Sandbox→执行）
        AG->>RT: TextBlock/tool_call 事件 → Envelope（SSE）
    end
    RT->>RT: ⑦POST_RESPONSE（session.save 等）→ finalize
    RT-->>API: SSE 事件流（response.created → deltas → completed）
    API-->>U: 流式渲染；渠道侧由 ChannelManager 回发
```

**8 个 Hook 阶段**（`src/qwenpaw/runtime/phases.py:28`，固定不变；可插入点全在这）：

```
PRE_DISPATCH（请求归一化，slash 分发前）
→ [固定步：slash 命令注册表分发]
→ POST_DISPATCH（未命中 slash 后）
→ PRE_AGENT_BUILD（session.load 等预构建）
→ [固定步：AgentBuilder.build + 启动 mode]
→ POST_AGENT_BUILD（注入 mode 上下文）
→ PRE_EXECUTE（bootstrap/prompt 刷新/env 栈压入）
→ [固定步：AgentExecutor 执行 agent，产出 SSE]
→ POST_RESPONSE（session.save / cron 触发回写）
→ ON_ERROR / FINALLY（错误归一化、幂等清理）
```

> **两层钩子是正交的**：runtime hooks 包住**一次请求**的生命周期；`agentscope.middleware`（`agents/middlewares.py`）包住**单次 agent reply 循环**。插件可以同时挂两层（`src/qwenpaw/runtime/phases.py:18` 原文注明）。

## 4. 部署形态架构

### 4.1 单机（默认）

一个 `qwenpaw app` 进程 = FastAPI（uvicorn）+ 所有 Workspace + Console 静态资源（`app/_app.py` 挂载）。数据在 `~/.qwenpaw/`（working / working.secret / working.backups）。

### 4.2 Hub 多租户（企业形态）

```mermaid
flowchart TB
    subgraph HubPlane["Hub 控制面（qwenpaw hub，src/qwenpaw/hub/）"]
        AUTH["多用户认证（sqlite 用户表/admin/user）"]
        ACL["ACL 策略引擎（fail-closed）★唯一强制点"]
        PROXY["个人代理 /api/{path} + WS 代理（注入 Runtime Token）"]
        PROV["Provisioners：Local / Docker / K8s（六方法接口）"]
        CATALOG["模型目录/策略目录/用量计量/审计"]
    end
    subgraph TenantA["租户 A runtime Pod"]
        RA["qwenpaw app（完整单机形态）"]
    end
    subgraph TenantB["租户 B runtime Pod"]
        RB["qwenpaw app"]
    end
    Console["Console（同一 bundle，mode: hub）"] --> PROXY
    PROXY --> ACL
    ACL -->|放行| RA
    ACL -->|放行| RB
    PROV --> RA & RB
    CATALOG -->|bootstrap env 注入模型目录| RA & RB
```

关键决策（摘自 `docs/enterprise/01-master-plan.md` §4，均有代码背书）：

1. **权限唯一强制点在 hub 个人代理**（`hub/control_app.py` 的 `personal_runtime_proxy` + WS 代理）：runtime 保持单用户语义、对上游零侵入；console 菜单过滤只是 UX。
2. **每租户一个 runtime**（Local/Docker/K8s Pod）：与上游模型对齐，PVC 用 RWO 即可，cron/心跳天然单副本。
3. **附加层优先于侵入修改**：企业代码全部落新文件（`hub/acl/`、`hub/provisioners/k8s/`、`deploy/helm/`），改上游文件须在 09 白名单内。

## 5. 值得先记住的 6 个设计决策

| # | 决策 | 代码证据 |
|---|---|---|
| 1 | **Agent 依赖全注入**：QwenPawAgent 不自建任何依赖，构造全部委托 AgentBuilder | `agents/react_agent.py:8`（模块 docstring）、`runtime/builder.py` |
| 2 | **Scroll Context 而非摘要压缩**：每轮全量持久化，被驱逐的轮次建立索引、按需 recall——"nothing summarized away" | `agents/context/scroll/`（manager 2182 行 + memoryspace 2015 行 + eviction_index） |
| 3 | **Loop Engineering 声明式门**：循环停止条件是可编译的 gate 目录（8 种内置），三态决策 BYPASS / INTERRUPT_AND_CONTINUE / TERMINATE | `loop/gates/base.py:15`、`loop/compiler.py:11` |
| 4 | **治理先于执行**：工具调用先过 Tool Guard 规则 → 治理策略（allow/deny/ask/sandbox）→ 沙箱内执行 | `security/tool_guard/`、`governance/`、`sandbox/` |
| 5 | **协议中立驱动层**：MCP/A2A/ACP 统一为 Driver 抽象，凭据集中加密保管，每次调用过策略门 | `drivers/adapters/`、`drivers/credentials/` |
| 6 | **全插件化扩展**：工具/钩子/命令/prompt 四注册表挂在 Workspace 上，Oh-My-Paw 插件可整体打包分发 | `app/workspace/workspace_plugins.py:33`、`src/qwenpaw/plugins/` |

## 6. 模块依赖方向（谁 import 谁）

```text
console（前端） ──HTTP/SSE/WS──▶ app
渠道 SDK ──webhook──▶ app/channels
app ──▶ runtime ──▶ agents ──▶ providers / drivers / memory / context
agents/loop/modes ◀── runtime（mode 启动注入）
app/workspace ──▶ runtime + channels + crons + plugins（组装）
hub ──代理──▶ app（黑盒转发，不 import 内部）
plugins(仓库) ──插件 API──▶ agents/app 的注册表
```

> 注意 `hub` 对 `app` 是**网络转发关系**而非 import 关系——这是"企业层不侵入上游"的结构保证。

---

下一页：[03-agent-core — Agent 内核深读](03-agent-core.md)
