# 01 · 项目概览（Overview）

> 本页是 QwenPaw 代码库的"一页纸"总览：它是什么、用什么技术、仓库长什么样、有哪些运行形态。
> 总体架构图见 [02-architecture](02-architecture.md)，各子系统深读见后续章节。

---

## 1. QwenPaw 是什么

**QwenPaw** 是一个**可本地或云端部署的个人 AI 助手**（"Your personal AI assistant — deploy locally or in the cloud"），由 AgentScope 团队出品（Apache 2.0），核心基于 **AgentScope 2.0** 框架（`agentscope==2.0.7.post1`，见 `pyproject.toml`）。

一句话理解：**它把"一个会用工具、有记忆、能被治理的 Agent"做成了可自托管的产品** —— 通过 Console（Web）、TUI（终端）、桌面端（Tauri）、十几个聊天渠道（DingTalk/飞书/微信/Discord/Telegram/iMessage/QQ/Slack/Matrix…）与人交互，通过 Skills/Plugins/MCP 无限扩展能力。

### 1.1 产品级能力矩阵（来自 README.md 功能表）

| 能力 | 说明 | 代码落点（速查） |
|---|---|---|
| **三层记忆** | live working context + 逐字全量历史 + 自演化个人知识库（ReMe） | `src/qwenpaw/agents/context/`、`src/qwenpaw/agents/memory/`、`reme-ai` 依赖 |
| **本地/云模型** | QwenPaw-Flash（2B/4B/9B）本地运行时；也支持 Ollama/LM Studio/14+ 云厂商 | `src/qwenpaw/local_models/`、`src/qwenpaw/providers/` |
| **安全内建** | 内核级沙箱、Tool Guard、File Guard、Skill Scanner、访问策略 | `src/qwenpaw/sandbox/`、`src/qwenpaw/security/`、`src/qwenpaw/governance/` |
| **多 Agent 并行** | 运行时 spawn 独立子 Agent（各有记忆/技能）；ACP 跨系统编排 | `src/qwenpaw/app/multi_agent_manager.py`、`src/qwenpaw/agents/acp/` |
| **文件工作区** | 统一的项目/Agent 文件导航、预览、编辑、diff、上传下载 | `src/qwenpaw/app/routers/files.py`、`src/qwenpaw/app/workspace/` |
| **可扩展** | Skills + Plugins + 市场 marketplace + MCP 外部工具 | `src/qwenpaw/agents/skill_system/`、`src/qwenpaw/plugins/`、`src/qwenpaw/market/` |
| **全渠道触达** | 一个实例接所有渠道 | `src/qwenpaw/app/channels/`（30+ 渠道适配） |
| **数据自主** | 本地部署、数据不出机器 | 整个工作区默认在 `~/.qwenpaw/` |

### 1.2 版本与定位（本仓库的 fork 背景）

本仓库是上游 [`agentscope-ai/QwenPaw`](https://github.com/agentscope-ai/QwenPaw) 的企业内 fork（`origin = b1n-next/QwenPaw`），在 `feature/enterprise` 分支上维护一层**企业增强层**（控制台权限、模型统一治理、RBAC/SSO、K8s 部署、用量可观测）。基线 commit `983b3ceb`（上游 v2.2.1 之后）。详见 [10-hub-enterprise](10-hub-enterprise.md) 与 `ENTERPRISE.md`、`docs/enterprise/01-master-plan.md`。

| 快速事实 | 值 |
|---|---|
| 语言/框架 | Python 3.11–3.13，FastAPI + uvicorn，AgentScope 2.0 |
| 后端规模 | `src/qwenpaw/` 约 **23 万行** Python（app 92K、agents 85K 为两大核心） |
| 前端 | `console/`（React 18 + TS + Vite + AntD，约 20 万行）+ `website/`（文档站） |
| 桌面端 | Tauri 打包（`src/qwenpaw/tauri/`、`scripts/pack-tauri/`） |
| 测试 | `tests/`（unit/contract/integration）+ `e2e/`（Playwright） |
| 当前分支 | `feature/enterprise`（企业线推进至 EP-2-15） |

---

## 2. 技术栈速览

- **Agent 框架**：AgentScope 2.0（`agentscope.agent.Agent` / `ReActConfig` / `Toolkit` / 事件流模型）
- **Web 服务**：FastAPI（`src/qwenpaw/app/_app.py` 组装，999 行），SSE/WS 流式
- **调度**：APScheduler（crons）；**浏览器自动化**：Playwright；**桌面截图**：mss
- **记忆**：`reme-ai`（ReMe 个人知识库）+ SQLite 会话历史（`agents/context/scroll/history.py`）
- **协议**：MCP（Model Context Protocol）、A2A、ACP（agent-client-protocol，接 Codex/Qoder 等外部 harness）
- **沙箱**：macOS Seatbelt / Linux bubblewrap / Windows AppContainer（`src/qwenpaw/sandbox/`，9 文件 ~9.7K 行）
- **打包**：pip / 安装脚本（uv）/ Docker / Helm（`deploy/helm/qwenpaw-hub`）/ Tauri 桌面

---

## 3. 仓库地图（目录 → 职责）

```text
QwenPaw/
├── src/qwenpaw/            # Python 后端主体（~230K 行）
│   ├── app/                # ★ 应用服务层（92K）：FastAPI 组装、44 个 router、
│   │   │                   #   channels/（渠道）、chats/（会话流式）、crons/、mail/、
│   │   │                   #   approvals/（审批）、mcp/、workspace/（Workspace）、
│   │   │                   #   multi_agent_manager.py（多 Agent 懒加载管理）
│   ├── agents/             # ★ Agent 内核（85K）：react_agent.py（QwenPawAgent）、
│   │   │                   #   context/（Scroll Context 上下文工程）、tools/（内置工具）、
│   │   │                   #   skill_system/（技能池）、memory/、acp/、hooks/、md_files/
│   ├── loop/               # Loop Engineering：8 种内置 stop-gate + 声明式自定义循环
│   ├── modes/              # Agent 模式：default / coding / goal / mission / custom_loop
│   ├── runtime/            # 运行时核心（请求处理 stream_query、commands/）
│   ├── cli/                # click 命令树 + tui/（textual 终端 UI）
│   ├── config/             # 配置加载（config.py 主配置 + utils）
│   ├── providers/          # 模型提供商抽象（14+ 云厂商、oauth、fallback、路由）
│   ├── local_models/       # QwenPaw Local 本地推理（QwenPaw-Flash 系列）
│   ├── drivers/            # 协议中立连接层：MCP/A2A/ACP 适配器 + 凭据保管
│   ├── memory/             # 记忆门面（实现在 agents/memory + reme-ai）
│   ├── checkpoints/        # 工作区检查点（快照/恢复）
│   ├── backup/             # 备份管理
│   ├── sandbox/            # 三平台 OS 级沙箱
│   ├── security/           # tool_guard / skill_scanner
│   ├── governance/         # allow/deny/ask/sandbox 资源治理策略
│   ├── browser/            # 浏览器自动化子系统（execution/control_link/governance/sdk）
│   ├── hub/                # ★ 多租户控制平面（acl/model_catalog/policy_catalog/
│   │                       #   provisioners/usage）——企业层的宿主
│   ├── plugins/            # Oh-My-Paw 插件系统（加载/生命周期）
│   ├── market/             # 插件/技能市场
│   ├── portability/        # 从其他助手（Codex/Qoder…）迁移导入
│   ├── harnesses/          # 外部 Agent harness 集成（codex/qoder/capabilities）
│   ├── pawapp/             # PawApp 小应用平台（页面型应用 SDK）
│   ├── tunnel/ tauri/      # 内网穿透 / Tauri 桌面壳
│   └── hooks/ services/ observability/ token_usage/ …
├── console/                # ★ Web 控制台前端（React+TS+Vite，~20 万行，1011 个 TS 文件）
├── plugins/                # 仓库内置插件集（apps/ bundle/ channel/ memory/ tool/）
├── packages/               # 独立子包（qwenpawmail-mcp：邮件 MCP server）
├── e2e/                    # Playwright e2e（pages/ 页面对象 + tests/）
├── tests/                  # pytest 三层：unit / contract / integration
├── deploy/helm/            # qwenpaw-hub Helm chart（企业线 EP-1-8）
├── docs/
│   ├── design/             # 环境管理重设计等设计文档
│   ├── enterprise/         # ★ 企业增强层文档库（01~11 + runbook）
│   └── deepwiki/           # 本 DeepWiki 学习文档
├── website/                # 官网/文档站（React+Vite）
├── .hub-accept/            # Hub 企业线本地验收环境（kind 集群 harness + control.db）
└── scripts/                # 安装/打包/校验脚本（pack、pack-tauri、verify、github）
```

---

## 4. 运行与部署形态

| 形态 | 入口 | 适用 |
|---|---|---|
| **本地 pip** | `pip install qwenpaw && qwenpaw init --defaults && qwenpaw app` | 个人本机，Console 在 `http://127.0.0.1:8088/` |
| **安装脚本** | `curl …/install.sh \| bash`（自动装 uv + venv + 前端资产） | 免 Python 环境 |
| **Docker** | `docker run -p 127.0.0.1:8088:8088 -v qwenpaw-data:/app/working …` | 容器化单用户（data/secrets/backups 三个卷） |
| **桌面端** | Tauri 安装包（Beta） | 图形化用户 |
| **云一键** | AgentScope Platform / 阿里云 ECS / ModelScope Studio | 云端托管 |
| **Hub 多租户** | `qwenpaw hub`（控制面 + 每租户一个 runtime：Local/Docker/K8s provisioner） | 团队/企业内网（≤200 人可信网络） |

工作区数据约定（默认 `~/.qwenpaw/`，Docker 内 `/app/working`）：

- `working` — 配置、记忆、技能（Docker 卷 `qwenpaw-data`）
- `working.secret` — 模型厂商设置与 API Key（卷 `qwenpaw-secrets`）
- `working.backups` — 备份归档（卷 `qwenpaw-backups`）

---

## 5. 学习路径建议

- **产品上手线**：README Quick Start → Console 各页面 → 加一个 Skill → 接一个渠道
- **内核线（理解 Agent 如何工作）**：[03-agent-core](03-agent-core.md) → [02-architecture](02-architecture.md) 的消息时序 → [13-flows](13-flows.md)
- **平台/企业线（理解本 fork 的增量）**：[10-hub-enterprise](10-hub-enterprise.md) → `docs/enterprise/01-master-plan.md`
- **工程线**：[12-testing-workflow](12-testing-workflow.md) → `CONTRIBUTING.md`
