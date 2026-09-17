# 09 · Fork 维护与上游对齐纪律

> 背景：上游日均 ~9 commits、squash 工作流、官方 Hub roadmap 待发布（#7318）。
> 目标：以最小 patch 面长期跟随上游，撞车可控、rebase 可执行。

## 1. 远端与分支布局（已按实际配置修正）

```text
upstream → https://github.com/agentscope-ai/QwenPaw   （官方仓库，只读拉取）
origin   → git@github.com:b1n-next/QwenPaw.git        （你的 fork，推送目标）

本地分支：
  main               跟随 fork/main（与上游 main 同步，不放自研提交）
  feature/enterprise 企业开发长线（本批文档所在）
  u/<date>-sync      上游对齐工作分支（月度例程用，用完即删）

基线：未打 tag，以 commit 记录 —— 当前基线 983b3ceb（上游 v2.2.1 发布后、2.2.2b1 线）。
阶段标记：Phase 完成时打 tag enterprise/v0.x（首个 Phase 0 完成打 enterprise/v0.1）。
基线 tag 已建：`enterprise/baseline-983b3ceb`（983b3ceb，annotated）。rebase 时可直接用作 `--onto` 锚点。
```

推送（remote 已配好，日常只需）：

```bash
git push -u origin feature/enterprise
```

## 2. Patch 面纪律（rebase 成本的决定因素）

**修改白名单**（v2，2026-09-14 追溯审计全量重登记；原"≤6 处、每处 ≤50 行"
定量口径已失真——`control_app.py` +472 行成为企业面主接线文件——改为
**登记制**：凡改上游文件必须在本表登记且注明 commit，rebase 冲突时逐行复核）：

| 文件 | 改动 | 所属 | 引入 |
|---|---|---|---|
| `src/qwenpaw/hub/control_app.py` | 企业面主接线：ACL 代理接入（HTTP+WS 1008）、permissions 端点、模型激活目录校验 + 列表过滤、usage 采集端点、k8s provisioner 挂载（+472/-13） | Ph0/1 | 多 commit |
| `src/qwenpaw/app/_app.py` | 模型 bootstrap 调用（lifespan 内 lazy import，+5） | Ph1 | 717c85ff |
| `src/qwenpaw/hub/local_provisioner.py` | `QWENPAW_MODEL_BOOTSTRAP_JSON` 过滤后显式放行（+6，仿 internal token 模式） | Ph1 | 717c85ff |
| `src/qwenpaw/hub/docker_provisioner.py` | 同上（docker env update 字典，+6） | Ph1 | 717c85ff |
| `src/qwenpaw/hub/config.py` | provisioner 配置扩 `k8s` 字面量 + 运行时后缀白名单 | Ph1 | 2b240cb0 |
| `src/qwenpaw/hub/service.py` | loopback 守卫统一（runtime 回环地址校验） | Ph1 | 2da1d468 |
| `src/qwenpaw/utils/http.py` | `runtime_host_allowed` 辅助（回环判定） | Ph1 | 2da1d468 |
| `.pre-commit-config.yaml` | check-yaml exclude helm 产物 | Ph1 | 157bda0c |
| `console/.prettierignore` | 忽略 `pnpm-lock.yaml`（+3） | Ph0 | — |
| `console/src/layouts/MainLayout/index.tsx` + `Sidebar.tsx` + `layouts/i18n.ts` | permissions 过滤挂载（Sidebar/路由守卫） | Ph0 | — |
| `console/src/pages/SettingsCenter/useSidebarEntryGroups.ts` | denied_routes 组折叠联动 | Ph0 | — |
| `console/src/os/AppStore.tsx` | OS dock 按 denied_routes 过滤 | Ph1 | c9aa6890 |
| `console/src/pages/Chat/ModelSelector/index.tsx` | model_readonly 门控（隐藏添加/OAuth/API-key 入口；目录内切换放行） | Ph1 | 61663a62/c0f5174a |
| `console/src/api/modules/hub.ts` | hub usage API 客户端 | Ph1 | 4e2c1964 |
| `console/src/pages/Hub/index.tsx` + `pageUtils.ts` + `index.module.less` | admin 用量统计区 | Ph1 | 4e2c1964 |
| `console/src/locales/*.json`（7 语言） + `i18n.ts` | 权限/用量/菜单词条 | Ph0/1 | 多 commit |
| `tests/unit/hub/test_control_app.py` | member 探针改道 + 代理行为演进同步 | Ph0/1 | 多 commit |
| `tests/unit/hub/test_config.py` | k8s provisioner 配置用例 | Ph1 | 2b240cb0 |
| `src/qwenpaw/hub/operations.py` | 审计 store：trace_id 记录/过滤/序列化 | Ph2 | EP-2-11 |
| `src/qwenpaw/hub/database.py` | schema v2：hub_audit_events.trace_id 列 + 索引 + v1→v2 迁移 | Ph2 | EP-2-11 |
| `src/qwenpaw/governance/audit.py` | runtime 工具审计 extra 记 trace_id（零表迁移，`_trace_extra` 惰性 import） | Ph2 | EP-2-11 |
| `src/qwenpaw/app/approvals/service.py` | 审批生命周期镜像到 ApprovalStore（create/resolve/cancel/GC 六触点 + 恢复扫描） | Ph2 | EP-2-12 |
| `src/qwenpaw/governance/policy.py` | hub_rules 第三层（evaluate 最优先）+ `apply_hub_baseline_from_env`（sha256 fail-closed；load 忽略 YAML hub_rules 键） | Ph2 | EP-2-13 |
| `src/qwenpaw/governance/resource_governor.py` | start() load 后应用 env 基线（1 行 import + 2 行调用） | Ph2 | EP-2-13 |
| `src/qwenpaw/hub/local_provisioner.py`（追加）/ `docker_provisioner.py`（追加） | `QWENPAW_POLICY_BASELINE_JSON` 过滤后显式放行 | Ph2 | EP-2-13 |
| `src/qwenpaw/agents/tools/agent_management.py` | spawn 三路径铸造 `subagent_principal`（`<parent>:sub:<suffix>`）注入 request_context | Ph2 | EP-2-14 |
| `src/qwenpaw/hooks/request_setup/contextvars_hook.py` | 子会话回合设/清 sub-principal ContextVar（审批路由仍走父身份） | Ph2 | EP-2-14 |
| `src/qwenpaw/governance/audit.py`（追加）/ `token_usage/manager.py`（追加） | 审计 agent 列与 usage agent 维度优先取 sub-principal | Ph2 | EP-2-14 |
| `src/qwenpaw/loop/catalog.py` | 第 8 种门 `human_gate` 注册（HumanGateParams + catalog 条目） | Ph2 | EP-2-15 |
| `src/qwenpaw/harnesses/registry.py` | 插件 harness 注册表（builtin id 保留、config_fields 泛化重建键、动态 catalog） | Ph2 | EP-2-16 |
| `src/qwenpaw/harnesses/runtime.py`（追加） | providers() 列表改走 `list_provider_items()`（1 处 for 源替换） | Ph2 | EP-2-16 |
| `src/qwenpaw/plugins/api.py`（追加） | `PluginApi.register_harness_provider` 扩展点 | Ph2 | EP-2-16 |
| `src/qwenpaw/graph/`（新目录：schema/executor/state_store） | EP-2-17 图执行引擎（零上游文件触碰） | Ph2 | EP-2-17 |
| `src/qwenpaw/app/_app.py`（追加） | 挂载 `/api/graph` 路由（1 行 import + include） | Ph2 | EP-2-18 |
| `src/qwenpaw/hub/control_app.py`（追加） | EP-2-19 模板市场路由（member 列表/实例化 + admin CRUD/上下架，graph 经内部 token 推送 runtime） | Ph2 | EP-2-19 |
| `src/qwenpaw/app/agent_context.py`（新文件，20 行） | EP-2-14 sub-principal ContextVar 模块（set/clear helper；新文件不算改上游，登记备查） | Ph2 | EP-2-14 |
| `src/qwenpaw/hub/usage/store.py`（追加） | EP-2-14 usage 汇总 `by_agent` 维度（+24） | Ph2 | EP-2-14 |
| `src/qwenpaw/loop/gates/__init__.py`（追加） | EP-2-15 导出 HumanGate（1 行 import/export） | Ph2 | EP-2-15 |
| `console/src/api/types/agent.ts`（追加）/ `console/src/pages/Agent/Config/components/AgentLoopCard.tsx`（追加） | EP-2-15 human_gate 类型与卡片（+1/+43） | Ph2 | EP-2-15 |
| `console/src/App.tsx`（追加） | EP-2-18 `/composer` lazy 路由（AuthGuard + Suspense，1 个 Route 元素） | Ph2 | EP-2-18 |
| `src/qwenpaw/hub/trace.py` + `src/qwenpaw/hub/policy_catalog/` + `src/qwenpaw/hub/templates/` + `src/qwenpaw/hub/prompt_library/` + `src/qwenpaw/hub/key_pool/`（新文件） | EP-2-11/13/19/24 hub 侧新模块（登记备查；零上游文件触碰） | Ph2 | 各票 |
| `src/qwenpaw/mcp_server/`（新目录） | EP-2-20 MCP server（protocol/tools/stdio/http，零上游文件触碰） | Ph2 | EP-2-20 |
| `src/qwenpaw/a2a/`（新目录） | EP-2-21 A2A server + core client（server.py 挂载经 `_app.py` 行，零上游文件触碰） | Ph2 | EP-2-21 |
| `src/qwenpaw/knowledge/` + `src/qwenpaw/app/routers/knowledge.py` + `src/qwenpaw/agents/tools/knowledge_search.py`（新文件） | EP-2-22 知识库层 + API + 工具（零上游文件触碰；挂载/导入经白名单既有行） | Ph2 | EP-2-22 |
| `src/qwenpaw/toolhooks/`（新目录） | EP-2-23 工具级 hook 层（零上游文件触碰；漏斗接线见 react_agent.py 行） | Ph2 | EP-2-23 |
| `src/qwenpaw/app/chats/session_store.py`（新文件） | Ph3 G-P14 SQLite 会话后端 + 工厂（零上游文件触碰） | Ph3 | G-P14 |
| `src/qwenpaw/pawapp/deps.py`（追加） | Ph3 会话存储改走 build_session_store() 工厂（默认 file 行为不变，try 块内 2 行替换） | Ph3 | G-P14 |
| `src/qwenpaw/sandbox/container_sandbox.py`（新文件） | Ph3 G-P14 docker 容器沙箱后端（零上游文件触碰） | Ph3 | G-P14 |
| `src/qwenpaw/sandbox/config.py`（追加） | Ph3 `SandboxMode.CONTAINER` 枚举值 + create_sandbox 分支（+6 行） | Ph3 | G-P14 |
| `src/qwenpaw/app/routers/mobile.py`（新文件） | Ph3 G-P14 移动 H5 审批页（内联 HTML 单页，零依赖零构建，复用 /api/approval） | Ph3 | G-P14 |
| `src/qwenpaw/app/routers/graph.py`（追加） | Ph3 悬项收口：resume miss 时 `_rebuild_executor` 从 graph_runs.db 重建（status=suspended + 模板在盘 → 重建续跑；模板缺失/终态 → 409） | Ph3 | EP-2-18 悬项 |
| `console/src/pages/Knowledge/`（新目录） | EP-2-22 console 收口：知识库管理页（列表/粘贴入库/上传/删除/检索试跑，antd，零新依赖） | Ph2+ | EP-2-22 |
| `console/src/api/modules/knowledge.ts`（新文件） | EP-2-22 知识库 API 客户端（Form 编码变体，沿用 graph.ts 约定） | Ph2+ | EP-2-22 |
| `src/qwenpaw/app/chats/session_store_redis.py`（新文件） | Ph3 G-P14 增强：Redis 会话后端（可选依赖 `qwenpaw[sessions]`，`QWENPAW_SESSION_STORE=redis` + `QWENPAW_SESSION_REDIS_URL`；lazy import，未启用零依赖） | Ph3 | G-P14 |
| `src/qwenpaw/app/chats/session_store.py`（追加） | Ph3 工厂增 redis 分支（+7 行）；`pyproject.toml` 追加 `sessions` extra（redis>=5,<7） | Ph3 | G-P14 |
| `tests/unit/sandbox/test_container_live.py`（新文件） | G-P14 容器沙箱 live 验收套（真 docker daemon：cgroup 限额/网络隔离/只读挂载/清理；无 daemon 自动 skip，5 例） | Ph3 | G-P14 |
| `src/qwenpaw/hub/quota/`（新目录） | EP-2-3 配额引擎（热重载 overlay + 30s 用量缓存 + 软/硬阈值；零上游文件触碰） | Ph2 | EP-2-3 |
| `src/qwenpaw/hub/control_app.py`（追加） | EP-2-3 代理配额门（ACL 后/转发前）+ `quota.exceeded` 审计 + `X-QwenPaw-Quota-Warning` 头 + `GET /api/hub/admin/quota` | Ph2 | EP-2-3 |
| `src/qwenpaw/hub/metrics.py`（新文件） | EP-2-4 无依赖 Prometheus collector（计数器/gauge + 文本格式渲染） | Ph2 | EP-2-4 |
| `deploy/prometheus/qwenpaw-alerts.yaml`（新文件） | EP-2-4 PrometheusRule 三条基线告警（runtime 缺失/采集滞后/ACL 拒绝速率）+ scrape 配置样例 | Ph2 | EP-2-4 |
| `src/qwenpaw/hub/usage/collector.py`（追加） | EP-2-4 `last_pass_epoch` 时间戳（采集新鲜度 gauge 数据源） | Ph2 | EP-2-4 |
| `deploy/scripts/backup-hub-sqlite.sh`（新文件） | EP-2-5 SQLite 在线备份脚本（`.backup` 一致快照 + secrets 复制 + SHA256SUMS + 轮转） | Ph2 | EP-2-5 |
| `docs/enterprise/runbook-backup-restore.md`（新文件） | EP-2-5 备份恢复双层手册（SQLite 层实测演练记录 + Velero 层步骤） | Ph2 | EP-2-5 |
| `src/qwenpaw/hub/acl/groups.py`（新文件） | EP-2-1 `GroupPolicyStore`（groups/group_members/policies CRUD + 主体序策略拉取） | Ph2 | EP-2-1 |
| `src/qwenpaw/hub/database.py`（追加） | EP-2-1 三表迁移（groups/group_members/policies + subject 索引） | Ph2 | EP-2-1 |
| `src/qwenpaw/hub/acl/engine.py`（追加） | EP-2-1 `decide(policies=)` 前置求值（user>group>role · 同路径 deny 优先 · apigroup 资源映射） | Ph2 | EP-2-1 |
| `src/qwenpaw/hub/control_app.py`（追加） | EP-2-1 admin groups/policies CRUD 端点 + 代理 decide 升级（组+策略实时求值） | Ph2 | EP-2-1 |
| `src/qwenpaw/app/_app.py`（追加） | 挂载 `/mobile/approvals`（1 行 import + include，根路径非 /api） | Ph3 | G-P14 |
| `src/qwenpaw/app/_app.py`（追加） | 挂载 `/api/mcp-server`（1 行 import + include；`/api/mcp/*` 既有客户端面不动） | Ph2 | EP-2-20 |
| `src/qwenpaw/app/_app.py`（追加） | 挂载 A2A 面（root `/.well-known/agent-card.json` + `/api/a2a`，2 行 include） | Ph2 | EP-2-21 |
| `src/qwenpaw/app/_app.py`（追加） | 挂载 `/api/knowledge`（1 行 import + include） | Ph2 | EP-2-22 |
| `src/qwenpaw/agents/tools/__init__.py`（追加） | 导入 knowledge_search 工具模块（1 行 import，装饰器自注册） | Ph2 | EP-2-22 |
| `src/qwenpaw/agents/react_agent.py`（追加） | EP-2-23 `_execute_tool_call` 漏斗内插 toolhooks 三挂点分发（pre/post/failure + trace_id，约 35 行，函数局部 import 无顶层依赖） | Ph2 | EP-2-23 |
| `src/qwenpaw/hub/control_app.py`（追加） | EP-2-24 prompt 资产库路由（member 只读已批准版 + admin 提案/审批）与 key 池路由（admin 增列/启停 + member lease 轮询） | Ph2 | EP-2-24 |

**已废弃条目**（v1 表内、实际未走该路线，清理记录）：
- ~~runtime usage 上报 hook~~——EP-1-4 改拉取式（hub 侧 UsageCollector），
  runtime 零 patch（07 §7 偏差说明）；
- ~~`App.tsx`/`builtinMenu.ts`~~——实际走 `registry/permissions.ts` 新文件
  组合进 capabilities 管线，两文件零改动。

**附加层**（全部新文件/目录，rebase 零冲突）：
`hub/acl/`、`hub/provisioners/k8s/`、`hub/models_catalog/`、`deploy/helm/`、
`plugins/apps/qa-data/`、`docs/enterprise/`、`tests/hub_acl/`、`tests/hub_k8s/`。

**红线**：不改 `agents/`、`sandbox/`、`channels/`、pawapp SDK 内部；不删上游测试；
发现上游 bug → 给上游提 PR（以 fork 的干净分支），不吸收进 feature/enterprise 的功能提交。

> 白名单漂移提示：上游对 `builtinMenu.ts`（v2.2.1 +9 行，新增 `core.import`）与
> `_app.py`（+46/-10，记忆插件化）仍在活跃修改——凡触碰白名单文件，动手前先
> `git log upstream/main -- <file>` 看最近一周变更；console 权限过滤优先走
> `capabilities.ts` 管线组合（03 §4.4），少改 `builtinMenu.ts` 本体。

## 3. 对齐例程

**月度（~1 小时）**
1. `git fetch upstream && git log --oneline feature/enterprise..upstream/main -- <白名单文件>` 评估冲突；
2. cherry-pick 安全修复（fix/security、CVE 相关 commit）到 `u/<date>-sync` → 回并 `feature/enterprise`；
3. 跑 CI（上游测试套件 + 自研 tests/）。

**季度（半天）**
1. 评估整体 rebase：`git rebase --onto upstream/<新tag> <旧基线commit或tag> feature/enterprise`；
   冲突仅预期出现在白名单 ≤6 处；
3. 对表官方 roadmap（#7318 及 release notes）：撞车项列退役/替换清单（02 台账"撞车"列同步）。

**触发式**
- 上游发布含 Hub 功能的新 minor → 立即执行季度例程（不等周期）；
- 官方 roadmap 公布 → 48h 内完成 02 台账全量撞车复评。

## 4. 上游监视点

| 信号 | 位置 | 频率 |
|---|---|---|
| Hub roadmap / RBAC / 模型治理动向 | issue #7318 及其引用 | 每周 |
| 官方 Helm/K8s/provisioner PR | repo PR 搜索 `provisioner|helm|k8s` | 每月 |
| `hub/`、`pawapp/`、`console layouts/registry` 重构 | `git log upstream/main -- src/qwenpaw/hub` | 每月（对齐例程内） |
| release notes | `website/public/release-notes/` | 每版本 |

## 5. 给上游回馈（降低长期维护成本）

- Phase 0 的"控制台能力权限 + 代理 ACL"上游为空白点（已核查）→ **干净 PR 已备（09-16）**：分支 `upstream/acl`（基于 main `7f945a46`，commit `c9964883`）：`hub/acl/` 纯增量 + `control_app.py` +37 接线（engine 挂载 / `GET /api/hub/me/permissions` / personal_runtime_proxy decide 门，拒绝事件走 #7683 发射器 `outcome=denied`）+ 72 测试；enterprise 耦合已剥（3 个依赖 model_catalog/websocket 扩展的集成测试移出、docstring 中性化、探针路径移到 member 允许面）。PR 正文：`pr-2026-09-16-acl.md`。开 PR 动作留待人工（跨仓权限）；被合并即从白名单移除该 patch；
- 第二票 **工具 hook 回馈 PR 已备（09-16）**：分支 `upstream/tool-hooks`（基于 main `7f945a46`，commit `588a5a59`）：`toolhooks/` 纯增量（491 行）+ `react_agent._execute_tool_call` 漏斗化（未注册 hook 纯透传）+ demo + 16 测试；解耦步骤：EP-2-23 字样中性化、`trace_context` 降级可选注入（pylint no-name-in-module 豁免）、上游无 `import time` 处补齐。PR 正文：`pr-2026-09-16-tool-hooks.md`。分支 agents 回归 3152 绿；
- 第三票 **知识库回馈 PR 已备（09-16）**：分支 `upstream/knowledge`（基于 main `7f945a46`，commit `b9ef4c66`）：`knowledge/` 纯增量（464 行，stdlib+SQLite 零新依赖，embedding hash 兜底）+ `/api/knowledge` 5 端点（`_app.py` 5 行接线）+ `knowledge_search` 工具（`tools/__init__.py` 1 行）+ 12 测试；EP-2-22 字样中性化。PR 正文：`pr-2026-09-16-knowledge.md`。分支回归 knowledge+app 2383、agents/tools 736 绿。**回馈池三票集齐，可一并人工提交**；
- 需求对齐：在 #7318 按官方模板回帖（内网可信、控制台权限、集中模型目录三点），
  争取官方方向覆盖 → 自研退役。

### 5.1 撞车 PR 跟踪（月检，最近一次 2026-09-17）

**例程步骤**（每月执行，约 0.5d）：
1. `git fetch origin main` + GitHub API 列 `commits?per_page=5` 与 `pulls?state=closed&merged` 增量（按上次月检日期过滤）；
2. 新合并逐条对白名单/自研模块扫撞面（ACP/console/agents/mcp 修复域 vs 我方首创目录 graph/a2a/mcp_server/knowledge/toolhooks/hub 子包）；
3. 双侧重叠文件 = `comm -12 <(git diff --name-only <旧基线>..origin/main) <(git diff --name-only <旧基线>..feature/enterprise)`，重叠且上游实质改动的白名单文件 → 登记为"rebase 人工复核点"；
4. 语义撞车 → 按 #7696/#7683 先例立处置预案（替换票或融合点）；
5. 推进 `enterprise/baseline-<新HEAD>` tag，§6 基线数字重算；
6. 决定本轮是否执行合并（撞面 ≥ 3 个白名单文件或含高危重构 → 立即合并消化；否则留下月）。

| PR | 状态 | 我方撞面 | 处置 |
|---|---|---|---|
| #7696 local admin bootstrap | **merged 09-15**（`hub/bootstrap.py` + `auth.py` + `cli/hub_cmd.py`） | `hub/bootstrap_admin.py` | **已替换（09-17）**：核心逻辑 100% 走官方 API（`ensure_admin_initialization_available` + `initialize_hub_admin`，root 解析 `QWENPAW_HUB_DIR`）；我方只剩非交互 + 幂等容器壳（~30 行，官方交互式 CLI 无法服务 initContainer 的两个约束）。helm initContainer 已切 env 注入形态。白名单条目从「平行自研」降级为「官方实现的容器壳」 |
| #7683 hub 审计（login attempts + denied runtime creation） | **merged 09-15**（`control_app.py` +121 + 测试） | `control_app.py` 我方 +472 主接线（白名单最重行） | **已执行融合（09-16 merge `ac6759c5`）**：`record_audit` 签名合并 `trace_id + outcome + remote_address` 双族参数，两套发射器并存；上游 `record_auth_event` 一并并入；`WORKING_DIR` import 因上游重构移除 |
| 2026-09-15/16 批量扫描（7763/7741/7636/7759/7758/7756/7787/7750/7704/7682/7681/7782） | 全部 merged | 无 | console/agents/memory/skill/mcp-**客户端**修复，不触我方首创模块（graph/a2a/mcp_server/knowledge/toolhooks/hub 子包），无新撞面 |
| 2026-09-17 例行扫描（7783 ACP 委托 / 6569 console EIO/EPIPE / 7805 字重 / 7732 ACP 权限选项 / 7789 多文件夹项目目录） | 全部 merged（09-16） | 无语义撞车（ACP/console/proj-dir 域不交我方自研模块）；但 3 个白名单文件上游再动：`config/config.py`（上+6/我-106）、`app/agent_context.py`（上±29）、`hooks/request_setup/contextvars_hook.py`（上+18/我-18，互删改） | **已消化（09-17 预演合并 `0ba13ef3`）**：三文件 git 零冲突自动融合且语义复核通过（上游新增 multi-folder 逻辑落位于我方删改区之外：contextvars_hook 保 `set_current_project_dirs` 新调用 + 我方删减不冲突；agent_context 上游 ±29 为 `get_project_dirs_for_request` 新增，与我方 EP-2-14 改动分属不同函数）；hooks+projdir 98 例、app+agents 5588 例、console 3641 例全绿 |
| #7318 需求对齐帖 | open（09-11 更新后无新决策） | — | 继续观望；官方覆盖任一自研点即提替换票 |

- 上游 09-17 HEAD：`d12bcd6c`——**已 merge 进 feature/enterprise（`0ba13ef3`，零冲突，三复核点语义复核通过）**。上轮：`7f945a46` → `ac6759c5`（control_app 融合）。基线 tag：`enterprise/baseline-7f945a46`；`d12bcd6c` 合并后基线 diff 重算留下次月检（§5.1 步骤 5）。

## 6. 实现状态与白名单维护（2026-09-14 追溯审计建立）

- **基线**：`enterprise/baseline-7f945a46`（2026-09-17 推进；上一基线
  `enterprise/baseline-983b3ceb` 仍保留供追溯）。当前 diff：204 文件、
  +34016/-119（= Phase 0-3 全部自研 + 白名单登记的上游文件修改；v2 白名单
  表以本 tag diff 为准逐文件登记，例程步骤 5 每月重算）。
- **维护规则**：① 动上游文件前先查本表，不在表内则先加表再动手；② 每次对表
  例程（EP-1-10/后续月度）重跑 `git diff --name-only enterprise/baseline-<x>..HEAD`
  与本表核对，漂移即修；③ rebase 前置检查：白名单文件的冲突逐行人工复核，
  优先评估"上游是否已官方实现"（官方实现则替换 patch 并回馈）。
- **收口节点**：Phase 2 平台线若落 11 §5 的 EP-2-11/12/13（trace/审批回流/
  策略下发），`control_app.py` 预计继续膨胀——届时评估把企业面拆成独立模块
  （`hub/enterprise/`），把上游文件 patch 面压回纯接线 ≤50 行。
