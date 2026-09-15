# 11 · 平台能力评估与 Phase 2 平台线 WBS

> 基线：`c0f5174a`（Phase 1 治理版收口，CI 三绿）。审计方法：四路并行只读子审计
> （执行运行时与上下文 / 能力面与治理 / 协议适配与 harness / console 产品模块），
> 证据落文件级；本文为裁决与立项依据，不构成已完成承诺。
> 关联：02（需求矩阵 G 组）、10（既有 WBS，治理线 EP-2-1..2-10）。

## §0 定位判定

**作为"开发者底座"：成立。作为"可视化平台"：不成立——缺编排层与治理贯通层。**

代码规模：src/qwenpaw 994 个 Python 文件（约 33.7 万行）+ console 1005 个 TS/TSX。
运行时内核、上下文系统（Scroll 压缩为全库最成熟子系统）、能力面（31 内置工具 +
MCP client + Skills 8.3k 行 + 子 agent）、治理原语（策略/审批/沙箱/审计）与 hub
多租户平面（本仓 Phase 0/1 成果）五块骨架齐备。结构性缺口集中在三处：

1. **编排层无图原语**——loop 是停止门管线而非 DAG；无画布编辑器；无 Human Gate。
2. **治理层不贯通**——hub 平面（ACL/模型目录/用量/审计）与节点平面
   （policy.yaml/ApprovalService/audit.db）各自为政，无 trace_id 贯穿。
3. **注册面封闭**——harness registry 硬编码；子 agent 共享身份；MCP server 缺位。

## §1 基础能力矩阵（五维）

### 1.1 执行运行时 —— ✅ 有

| 能力 | 证据 | 差距 |
|---|---|---|
| 事件循环 | `runtime/runtime.py` `Runtime.run()` 8-phase（`phases.py` PRE_DISPATCH→FINALLY）+ `HookRegistry` 拓扑排序；`executor.py` 驱动 `agent.reply_stream`；`heartbeat.py` 25s 保活；`envelope.py` SSE 信封翻译 | 内层 ReAct 继承自 agentscope，深层行为受上游版本约束 |
| 会话 | `app/chats/session.py` `SafeJSONSession`（原子写+每路径锁）；`ChatManager` 多会话/分组/归档；取消路径 `asyncio.shield` 保存中断回合（闭合悬空 tool-call） | **本地 JSON 单机**：无 DB 后端/跨实例存储，多副本无 HA 故事 |
| Checkpoint | `checkpoints/service.py` git 快照四类（auto/snap/pre-restore/sha）+ timeline + GC；`restore.py` 事务式三档恢复（会话/+memory/+全工作区，含 dry-run 与恢复前快照） | **不含执行态**：在飞 tool-call、gate 计数、子代理树不入快照，长循环崩溃只能回合/会话级恢复 |
| 超时隔离 | `tool_calls/_coordinator.py` 双 deadline（offload@50% + 宽限期强杀）；`react_agent` 工具级默认（shell 60s 等）+ agent.json 覆盖；`loop/gates/limits.py` `TimeoutGate` | `TimeoutGate` 仅循环边界检查（代码自述），挂起的模型调用无循环层硬取消 |

### 1.2 上下文系统 —— ✅ 有（RAG 部分）

| 能力 | 证据 | 差距 |
|---|---|---|
| System Prompt | `runtime/prompt_manager.py` priority 分层组装；`prompt_contributors.py` 11 个贡献者（protected 契约→AGENTS.md→SOUL→…→预载技能）；`protected_prompt.py` 防注入执行契约 | 无 prompt 版本化/A-B/按角色模板治理 |
| Compaction | `agents/context/scroll/manager.py` **九级渐降压管线**（持久化→预折叠→LLM 摘要→驱逐索引→live/think/active-fold）；`eviction_index.py` 分层里程表；`memoryspace.py` SQLite FTS5/BM25 + CJK 回退；`recall_tool.py` 结构化召回 | 企业标准下几乎无差距；摘要质量依赖模型 |
| RAG | `agents/memory/` 插件注册表 + ReMe 后端（embedding 热应用/reranker）；`plugins/memory/adbpg` 向量库插件 | **检索栈外包**：核心包无内置向量索引；文档级 RAG 无一等公民入口；企业落地须自备并验证外部检索服务 |
| 渐进披露 | 技能 preload（全文进 system prompt）/viewer（按需读 SKILL.md）二分（`runtime/builder.py`）；Scroll 驱逐索引+recall 工具本质是上下文渐进披露 | 工具描述全量注入 schema，无按查询动态子集/描述压缩 |

### 1.3 能力面 —— ✅ 有

| 能力 | 证据 | 差距 |
|---|---|---|
| 内置工具 | `runtime/tool_registry.py` `tool_descriptor`（带 `requires_modes/skills/features/sandbox` + `default_policy` 治理元数据）；31 个内置工具 | 无工具级签名/来源校验（供应链）；migration_compat 系属过渡 |
| MCP | client 完整：`drivers/handlers/mcp.py` + 有状态客户端（StdIO/Http）+ 工具白名单 + per-principal 策略 + OAuth 发现（`mcp_oauth.py`） | **无 MCP server**：自身不能作为 tool 挂进企业 MCP 网关 |
| Skills | `agents/skill_system/` 8.3k 行：workspace/pool 服务、9 种安装来源、`skill_scanner` 安装前扫描 | 市场以第三方公网为主，无企业私仓/上架审批流；scanner 仅正则基线 |
| 子 Agent | `agent_management.py` `spawn_subagent`（git worktree 隔离、batch≤10 并发≤3、tools/skills 白名单）、`chat_with_agent`/`submit_to_agent`；`delegate_external_agent` 外部 harness 委派 | **与父共享身份**：无独立 principal/凭证降权；hub 侧无子 agent 资源预算归集 |

### 1.4 治理层 —— ✅ 有（贯通 ✗，企业成色的主缺口）

| 能力 | 证据 | 差距 |
|---|---|---|
| 审批 HITL | `app/approvals/service.py`（Future 挂起+超时 GC+渠道通知）；`ApprovalScope` EXACT/SIMILAR→`generalize.py` 审批即泛化成规则；`driver_gate.py` 把 MCP driver 的 ask 桥进同一审批流 | **pending 全内存，重启即丢**；无多级/会签/委托/豁免窗；审批记录不落 hub 无统一台账 |
| Hooks | `runtime/hooks.py` 按相位注册 + SHORT_CIRCUIT 语义；8 个生命周期挂点；9 个内置 hook + 模式门控 + 插件可注册 | **无 per-tool-call 挂点**：企业 DLP/风控 webhook 接不进工具执行链 |
| 策略 | `governance/policy.py` 1885 行：builtin/user 两层首匹配、glob 语义、YAML 持久化、四档 execution_level、shell 逃逸检测；`drivers/policy.py` 带 principal/时间窗 ABAC | **workspace 本地 YAML**：无 hub 集中下发/版本化/签名/组织 baseline；策略变更无中央审计 |
| 沙箱 | 四平台：macOS Seatbelt / Linux Bubblewrap+Landlock / Windows 三档；**逐工具调用创建销毁**（`ResourceGovernor.compile_sandbox_config`→`PolicyGuardedTool`→`shell.py:1012`），MountSpec/PortRule/env 白名单 | 无容器/microVM 后端（gVisor/Firecracker/K8s runtimeClass）；file_io 实际只走进程内 guard；browser 仅子进程隔离 |
| 血缘审计 | 节点：`governance/audit.py` SQLite 5W（agent/session/tool/target/decision）；hub：`hub/operations.py` `hub_audit_events`（actor/action/correlation_id），control_app 30+ 处 record_audit；`observability/langfuse.py` LLM trace | **两库割裂无 trace_id 贯穿**（工具审计无 user_id）；本地库 100k 自动 purge 最旧 10k，无防篡改（hash chain/WORM），不满足取证留存 |

### 1.5 协议适配 —— 🟡 部分

| 协议 | 评级 | 证据 | 差距 |
|---|---|---|---|
| CLI | ✅ | `cli/main.py` ~30 子命令；`task_cmd.py` headless 单任务（result.json+用量）；TUI/daemon/doctor | headless 仅单轮；channel 固定 console |
| Web | ✅ | console 双形态（经典控制台 + `/os` 桌面 OS：Dock/MissionControl/WindowFrame）；FastAPI ~40 路由；hub 控制平面（本仓） | hub 定位内网可信网络（ENTERPRISE.md 明示） |
| IDE | 🟡 | `agents/acp/server.py` 1590 行 ACP stdio JSON-RPC（Zed/OpenCode 直连，复用完整 Workspace/会话/权限/UsageUpdate）；LSP 作为 agent 工具 | 无 VSCode/JetBrains 第一方扩展；编辑器覆盖间接 |
| MCP server | ❌ | 唯一服务端是附属包 `packages/qwenpawmail-mcp` | 不能被其他 MCP 宿主编排 |
| A2A | 🟡 | 华为 xiaoyi 变体（双 WebSocket）；标准 A2A 1.0 客户端在 cloudpaw **插件**（agent-card 发现+SSE）；agent 间委托走私有 HTTP `/console/chat` 回环 + ACP 子进程 | 核心无 `.well-known/agent-card.json` 服务端；互操作碎片化 |

## §2 harness / loop 专项

### 2.1 harness（外部 agent 桥接）

**形态**：把 Codex CLI / Qoder 等第三方 agent 作为 QwenPaw 会话后端引擎，console 做
统一前端（模型选择、审批预设、QwenPaw Skills/MCP 双向投影）。

- `base.py` `HarnessAdapter` ABC：认证生命周期（抽象）+ 可选能力（默认实现）+
  核心 `run_turn()` 返回 `AsyncIterator[HarnessEvent]`。
- `events.py` 8 种归一事件（text/reasoning delta、tool 三态、completed/cancelled/
  error）+ 20 项能力矩阵 + 审批预设；`streaming.py` 有序文本段/工具信封。
- `capabilities/resolver.py` 双向投影（SecretStr 密钥 + fingerprint 隔离）。
- 两实现：codex（app-server JSON-RPC 子进程）/ qoder（进程内 SDK）；测试 15+ 文件。

**裁决：抽象面 provider-neutral 足以接任意第三方 agent，但注册体系封闭**——
`create_adapter()` 硬编码 if/elif、`PROVIDER_CATALOG` 冻结常量、plugins/ 无
HarnessAdapter 注册钩子、配置面仅 `binary` 一键。企业自定义 harness 必须 fork。

### 2.2 loop（停止门组合系统）

**形态：不是 DSL、不是图编排，是挂在 ReAct 停止决策上的 StopGate 管线。**

- `gates/base.py` 三态 `BYPASS/TERMINATE/INTERRUPT_AND_CONTINUE`（续跑经
  `build_continuation()` 注入消息）；`handler.py` 优先级串行 + 故障隔离。
- `catalog.py` 白名单 7 种内建门：iteration / doom_loop / token_budget / timeout /
  tool_call_budget / qualitative_rubric / completion_rubric（pydantic 严格参数 +
  互斥组，暴露 JSON Schema 给前端）；`compiler.py` 把 `CustomLoopModeConfig`
  原子化编译成 StopHandler；`DeclarativeLoopMode` 注册为 slash 命令。
- 会话级隔离（ContextVar）；模式级门（goal/mission）在核心内。

**裁决：无 Human Gate**——7 种门全自动判定，HITL 仅在工具权限层（tool_guard
ASK）；无"第 N 步暂停等签发"的一等原语，不支持分支/并行/DAG。企业流程性循环
（发布审批、变更单）需自行写 Python 门插件。

## §3 产品模块矩阵（六模块）

| 模块 | 评级 | 现状证据 | 缺口 |
|---|---|---|---|
| 助手对话面 | ✅ | SSE 流式+SDK 重连；CoT 折叠透视+thinking 等级；27 种工具卡；Chat+IDE 双面板（Monaco per-hunk diff keep/undo） | CoT 非独立过程面板 |
| HITL 审批台 | ✅ | 三层（chat 卡 442 行/Inbox/IM 渠道）+ 四档等级（STRICT/SMART/AUTO/OFF）+ approve-exact/pattern + 超时倒计时 | 无"修改后批准"分支；无移动 H5 专页 |
| 工具&知识库市场 | 🟡 | apps/plugins/skills 三 tab；MCP per-agent 管理（stdio/sse/streamable_http+OAuth+访问策略）；技能池；ClawHub/LobeHub 导入 | **知识库 UI 全缺**（上传/切块/向量化/检索测试零覆盖） |
| 智能体中枢大厅 | 🟡 | Agent CRUD（拖拽排序/Copy）；AgentStats 按日多维+渠道 Pie；侧栏实时状态指示器 | 无聚合大厅落地页；Agent 无详情页；市场不卖 Agent 模板；资产与指标未联动 |
| 治理后台 | 🟡 | hub 2636 行（overview/runtimes/users/usage/credentials/audit/settings）；本地 Security/Backups | 无 Prompt 资产库与版本 Diff；无 API Key 池（轮换/配额/共享）；本地控制台无操作审计 |
| DAG 可视化编排器 | ❌ | **零图编辑库**（无 reactflow/xyflow/dagre/elkjs）；loop 配置=1772 行表单+dnd-kit 拖拽；checkpoint/记忆图为只读自绘 | 整层缺失——平台定位的最大断点 |

## §4 差距登记簿

> 编号 G-P1..G-P14（Platform 线，区别于 02 的 G 组运行时需求）。WBS 票据引用此编号。

| # | 差距 | 归类 | 严重度 |
|---|---|---|---|
| G-P1 | 无 DAG 画布编排器（节点/连线/属性面板/Human Gate 卡片） | 编排 | 高 | ✅ EP-2-17 `129c8b66` + EP-2-18 `343e51e1` |
| G-P2 | loop 无 human_gate 原语（审批只在工具层） | 编排 | 高 | ✅ EP-2-15 `357c11b7` |
| G-P3 | harness registry 封闭（硬编码 factory，无插件钩子） | 编排 | 高 | ✅ EP-2-16 `3063b8be` |
| G-P4 | 策略无 hub 集中下发/版本化/签名（每节点本地 YAML） | 治理贯通 | 高 | ✅ EP-2-13 `885257ca` |
| G-P5 | 审批不持久化（内存态）、不回流 hub、无多级/会签 | 治理贯通 | 高 | ✅ EP-2-12 `f16c1c67` |
| G-P6 | 双审计库无 trace_id 贯穿；本地可 purge、无防篡改 | 治理贯通 | 高 | ✅ EP-2-11 `7433d5b3` |
| G-P7 | 子 agent 共享父身份，无 principal 降权/预算归集 | 治理贯通 | 中 | ✅ EP-2-14 `46dbb0d5` |
| G-P8 | 无 MCP server 化（不能进企业 MCP 网关） | 互操作 | 中 | ✅ EP-2-20 `50ac4387` |
| G-P9 | A2A 碎片化（标准客户端困在插件、无 agent-card 服务端） | 互操作 | 中 | ✅ EP-2-21 `6e4f9b6d` |
| G-P10 | RAG 检索栈外包（无内置向量索引，文档级 RAG 无入口） | 能力 | 高 | ✅ EP-2-22 `785b7557` |
| G-P11 | hook 无工具级挂点（DLP/风控接不进工具链） | 治理贯通 | 中 | ✅ EP-2-23 `6c953fb2` |
| G-P12 | 无 Prompt 资产版本管理/Diff；无 API Key 池 | 产品 | 中 | ✅ EP-2-24 `231dcf4b` |
| G-P13 | 无 Agent 模板市场/中枢大厅聚合页 | 产品 | 中 | ✅ EP-2-19 `50894b98` |
| G-P14 | 会话单机 JSON、沙箱无容器后端、无移动 H5 审批 | 规模化 | 低（Phase 3） | ⬜ Phase 3 候选（未启动） |

## §5 Phase 2 平台线 WBS

> 票号 EP-2-11 起（EP-2-1..2-10 为既有治理线，见 10 号文档，不重复）。
> 预估口径同 10 号：1 人 + AI 辅助。优先级：P0（及格线）> P1（平台成立）> P2（生态）。

### P0 · 治理贯通（企业底座及格线，4 周）— ✅ 全部完成

| 票 | 内容 | 交付物 | DoD | 预估 | 依赖 | 关联 |
|---|---|---|---|---|---|---|
| EP-2-11 | trace_id 贯穿：hub 生成 X-QwenPaw-Trace-Id → 代理透传 → runtime 审计/SSE 回传 → hub 审计落库关联 | `hub/trace.py` + 代理头注入 + `governance/audit.py` 列扩展 + `hub_audit_events.trace_id` 索引 | 一条 user→agent→tool→MCP 链路在 hub 审计页可完整回放 | 3d | — | G-P6 |
| EP-2-12 | 审批持久化 + hub 台账：pending 落 SQLite（重启恢复）；resolve 事件经代理回流 hub 审计 | `approvals/store.py` + 恢复扫描 + `approval.resolved` 审计事件 | kill -9 重启后 pending 可恢复；hub 审计页见完整审批流 | 3d | — | G-P5 |
| EP-2-13 | 策略 hub 下发：hub 存组织 baseline policy（版本化+hash 签名）→ provisioner 注入 env → runtime 启动校验合并（hub baseline 压过本地） | `hub/policy_catalog/` + bootstrap env 扩展 + `governance/policy.py` 合并层 | 改 hub 策略→runtime 重启生效；本地无法降级 hub 规则 | 4d | — | G-P4，衔接 EP-2-1 |
| EP-2-14 | 子 agent 身份降权：spawn 时分配 sub-principal（`user:xxx:agent:N`），工具审计与预算按 principal 归集 | `agent_management.py` principal 注入 + 审计列 + usage 归集 | hub 用量页按子 agent 拆分可见 | 3d | EP-2-11 | G-P7 |

### P1 · 编排原语（平台成立，5-6 周）— ✅ 全部完成

| 票 | 内容 | 交付物 | DoD | 预估 | 依赖 | 关联 |
|---|---|---|---|---|---|---|
| EP-2-15 | loop `human_gate`：第 N 轮/命中条件时挂起 → 复用 ApprovalService 等待 → 批准则续跑/驳回则终止 | `loop/gates/human.py` + catalog 注册（第 8 种门）+ console 审批卡联动 | 一个带 human_gate 的自定义 loop mode E2E：暂停→审批→续跑 | 3d | EP-2-12 | G-P2 |
| EP-2-16 | harness registry 插件化：`HarnessAdapter` 注册钩子（plugins/api.py 扩展点）+ adapter 配置面泛化（binary/env/args）+ 第三方注册示例 | registry 动态化 + `examples/custom_harness/` | 不 fork 源码接入一个示例 harness（echo demo）并出现在 provider 列表 | 3d | — | G-P3 |
| EP-2-17 | 图执行引擎（后端）：把 gate/loop/subagent/harness 节点化为可编排 DAG（pydantic 图 schema + 拓扑执行器 + 节点级 checkpoint 续跑） | `src/qwenpaw/graph/`（schema/executor/state_store） | 线性+分支两图 E2E；中断后从已完成节点续跑 | 5d | EP-2-15 | G-P1 |
| EP-2-18 | DAG 画布编排器（前端）：reactflow 画布 + 节点面板（模型/Prompt/Tools/Human Gate）+ 右侧属性面板 + 发布为 Agent 模板 | `console/src/pages/Composer/` + graph API 对接 | 拖拽连线发布一个含 Human Gate 的图智能体并可从大厅启动 | 6d | EP-2-17 | G-P1 |
| EP-2-19 | Agent 模板市场 + 中枢大厅：模板打包（graph+prompt+skills 清单）/上架审批/一键实例化；大厅聚合页（资产卡+健康度+模板入口） | `hub/templates/` + `console/src/pages/Hub/Agents` + Market 模板 tab | member 从大厅选模板→实例化→对话跑通；admin 上下架 | 5d | EP-2-18 | G-P13 |

### P2 · 生态与产品补齐（按需排队）— ✅ 全部完成

| 票 | 内容 | 交付物 | DoD | 预估 | 依赖 | 关联 |
|---|---|---|---|---|---|---|
| EP-2-20 | MCP server 化：把 agent/图暴露为 MCP server（stdio+streamable http），tool schema 自动生成 | `src/qwenpaw/mcp_server/` | 外部 MCP 宿主（如 Claude Desktop）调用 QwenPaw agent 成功 | 4d | — | G-P8 |
| EP-2-21 | A2A 服务端：`.well-known/agent-card.json` + 标准 A2A 1.0 端点（message/stream SSE），把 cloudpaw 插件客户端提为核心 | `app/routers/a2a.py` 核心化 | 标准 A2A 客户端发现并调用成功 | 4d | — | G-P9 |
| EP-2-22 | 知识库一等公民：文档上传/切块/向量化管线（复用 ReMe 后端）+ 检索测试 UI + 工作区文档 RAG 入口 | `app/routers/knowledge.py` + console 知识库页 | 上传 PDF→切块→检索命中片段注入对话 | 6d | — | G-P10 |
| EP-2-23 | 工具级 hook 挂点：PRE_TOOL/POST_TOOL 相位 + 外部 webhook hook 类型（DLP/风控接入示例） | `runtime/phases.py` 扩展 + `hooks/webhook.py` | 第三方 webhook 拦截一次工具调用并改写参数 | 3d | — | G-P11 |
| EP-2-24 | Prompt 资产库：版本化存储 + Diff 视图 + 发布回滚；API Key 池（轮换/配额/共享） | `hub/prompts/` + hub key pool + console 治理页 | prompt 改动可 Diff 可回滚；key 轮换不断流 | 5d | EP-2-13 | G-P12 |

### Phase 3 候选（不承诺，对表 02 Phase 3 与官方 roadmap）

会话 DB/Redis 后端（G-P14）｜gVisor/Kata 沙箱（02-G4）｜移动 H5 审批｜
VSCode/JetBrains 扩展｜审计 WORM/外部 SIEM 归集｜skill 企业私仓。

## §6 与既有规划的关系

- **02-G2/G4/G7**（能力协商/gVisor/常驻 Pod）与本登记簿正交，维持原规划。
- **EP-2-1**（groups/policies 表）是 EP-2-13 的存储前置，建议合并实施。
- **EP-2-5**（Prometheus）与 EP-2-11 的 trace 体系互补，指标侧不变。
- 治理线（EP-2-1..2-10）与平台线（EP-2-11..2-24）可并行排期；资源紧张时
  **P0 治理贯通优先于一切平台票**——没有 trace_id 与审批台账，后续所有编排
  产物的治理都是补丁式的。

## §7 台账维护规则

- 票据状态维护在 10 号文档（本文只登记平台线票面与 DoD，10 号加同名行互链）。
- 每完成一票，§4 登记簿对应差距行标注 ✅ 并附 commit；评审发现新差距追加 G-P15+。
- 上游合入相关能力（如官方 loop/编排演进）时，先对表官方 roadmap 再决定撤票或改票
  （09 号上游零领先纪律）。
