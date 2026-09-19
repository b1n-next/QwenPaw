# 18 · E10 对话级计费改造清单（评估稿）

> 2026-09-19 · 状态：**评估**（未实施）· 决策待拍板
> 前置：E10 Agent 级已落（`bb0770d9`：costs `by_agent` + CSV 导出）。

## 1 · 为什么对话级现在不能直接做

上游 runtime 的记账粒度是 **(date, agent_id, provider_id, model) 聚合**：

| 层 | 现状 | 证据 |
|---|---|---|
| 采集 | `TokenRecordingModelWrapper` **已按 session 分桶暂存**（`_usage_by_session[session_id]`，model_wrapper.py:179-218） | session_id 在手，但落库时被丢弃 |
| 落库 | `TokenUsageRecord` 无 session/chat 字段（manager.py:51） | 行=日×Agent×provider×model 聚合 |
| 汇报 | hub `usage_counters` PK=(tenant, date, provider, model, agent)（upsert 时聚合） | 无对话维度 |

即：**采集点已有 session_id，只是存储/上报两段把它聚合掉了**。改造是"沿既有管道把已有字段带到底"，不是新埋点。

## 2 · 改造清单（按依赖顺序）

| # | 项 | 文件/层 | 内容 | 风险 |
|---|---|---|---|---|
| C1 | 记录模型加字段 | `token_usage/manager.py` | `TokenUsageRecord` + `session_id: Optional[str]`（默认 None 兼容旧行） | 低 |
| C2 | 存储加会话维度 | runtime 本地 usage 存储 | 表加列（或新表 `usage_by_session`），PK 加 session_id；**旧行 session_id=NULL** | 中：存量迁移 + 查询路径 |
| C3 | 落库带 session | `token_usage/` 上报点 | `pop_usage_for_session` 携带 session_id 写入 C2 | 低 |
| C4 | 汇报协议带维度 | runtime→hub 上报 payload | 计量 schema 加 `session_id`（可选字段，老 hub 忽略） | 中：跨进程协议版本 |
| C5 | hub 落库加维度 | `hub/usage/store.py` | `usage_counters` PK 追加 session 维度或旁挂 `usage_sessions` 明细表（推荐旁挂：主表聚合语义不变） | 中 |
| C6 | 导出/账单 | `control_app.py` | costs export 增加 `session_id` 列 + 按 session 聚合选项 | 低 |
| C7 | 归属用户 | 汇报链 | session→user 归属随 C4 带上（hub 侧已有 session 归属表） | 低 |

## 3 · 建议路线

- **旁挂明细表**（C5 推荐）：`usage_counters` 保持日聚合（G3 配额读它，改动=零），新增 `usage_session_details`（tenant, date, session_id, user_id, provider, model, agent, tokens…）只追加。E10 导出按明细表出，对话级账单不碰配额路径。
- **分两批**：C1-C3 纯 runtime 侧（本仓可做，不碰 hub 协议）；C4-C6 跨平面（需要上游 schema 版本协商，建议走上游 issue 而非 fork 私改）。
- **体量预估**：C1-C3 ≈ 1 票工作量（含测试）；C4-C6 视上游节奏。

## 4 · 不做对话级的替代（已满足当前口径）

Agent 级 + 时间窗 + 组归属的 CSV 已可支撑部门分摊；对话级只在"按次向终端用户收费"场景必需——当前需求矩阵未见该场景。
