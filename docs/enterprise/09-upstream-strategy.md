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

- Phase 0 的"控制台能力权限 + 代理 ACL"上游为空白点（已核查）→ 完成后整理成 PR 提交
  （干净实现，无 enterprise 耦合）；被合并即从白名单移除该 patch；
- 需求对齐：在 #7318 按官方模板回帖（内网可信、控制台权限、集中模型目录三点），
  争取官方方向覆盖 → 自研退役。

### 5.1 撞车 PR 跟踪（月检，最近一次 2026-09-16）

| PR | 状态 | 我方撞面 | 处置 |
|---|---|---|---|
| #7696 local admin bootstrap | **merged 09-15**（`hub/bootstrap.py` 新文件 + `auth.py` +229 + `cli/hub_cmd.py`） | `hub/bootstrap_admin.py`（我们自研，k8s initContainer 流程在用） | **平行实现共存**：上游走 CLI 命令、我方走 initContainer。文件零冲突（上游树无 bootstrap_admin.py）。替换为官方实现需切 runbook + helm（独立票 EP-3-x，未排期）；替换前两条路径并存，文档标注优先官方 CLI |
| #7683 hub 审计（login attempts + denied runtime creation） | **merged 09-15**（`control_app.py` +121 + 测试） | `control_app.py` 我方 +472 主接线（白名单最重行） | **rebase 融合点**：语义互补（上游补 login attempts/denied creation 两类事件，我方有 `acl.denied`/template/prompt/key 事件族）。下次 rebase `control_app.py` 必冲突，按行人工融合两套发射器，勿丢任一侧 |
| 2026-09-15/16 批量扫描（7763/7741/7636/7759/7758/7756/7787/7750/7704/7682/7681/7782） | 全部 merged | 无 | console/agents/memory/skill/mcp-**客户端**修复，不触我方首创模块（graph/a2a/mcp_server/knowledge/toolhooks/hub 子包），无新撞面 |
| #7318 需求对齐帖 | open（09-11 更新后无新决策） | — | 继续观望；官方覆盖任一自研点即提替换票 |

- 上游 09-16 HEAD：`7f945a46`；本地下次 fetch 后把 `enterprise/baseline` 对表基线推进纳入 EP-1-10 例程。

## 6. 实现状态与白名单维护（2026-09-14 追溯审计建立）

- **基线**：`enterprise/baseline-983b3ceb`（tag 常驻）。当前 diff：53 文件、
  +5386/-51（上游 merge 83325387 自带的 website/docs 3 文件除外，fork 实改
  上游文件 18 个 + 新增文件若干；v2 白名单表即以 tag diff 为准逐文件登记）。
- **维护规则**：① 动上游文件前先查本表，不在表内则先加表再动手；② 每次对表
  例程（EP-1-10/后续月度）重跑 `git diff --name-only enterprise/baseline-<x>..HEAD`
  与本表核对，漂移即修；③ rebase 前置检查：白名单文件的冲突逐行人工复核，
  优先评估"上游是否已官方实现"（官方实现则替换 patch 并回馈）。
- **收口节点**：Phase 2 平台线若落 11 §5 的 EP-2-11/12/13（trace/审批回流/
  策略下发），`control_app.py` 预计继续膨胀——届时评估把企业面拆成独立模块
  （`hub/enterprise/`），把上游文件 patch 面压回纯接线 ≤50 行。
