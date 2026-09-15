# 10 · 任务分解（WBS）

> 票号规则：EP-<phase>-<seq>。每票独立可交付、可验收；预估按"1 人 + AI 辅助"口径。
> 状态：☐ 未开始 ｜ ◐ 进行中 ｜ ☑ 完成。

## Phase 0 · 受限版（1-2 周）

| 票 | 内容 | 交付物 | 验收（DoD） | 预估 | 依赖 |
|---|---|---|---|---|---|
| EP-0-1 ☑ | router 能力分组盘点定稿（39 个路由文件逐条归组） | 03 附录 A 定稿表 + `hub/acl/rules.py` 常量 | 灰区 4 项全部裁决（见附录 A） | 0.5d | — |
| EP-0-2 ☑ | `hub/acl/` 策略引擎 + 单测 | rules/engine/console_map + tests/unit/hub/test_acl.py | 67 用例全绿（fail-closed/归一化/覆盖层） | 2d | EP-0-1 |
| EP-0-3 ☑ | 代理接入：HTTP + WS + 审计事件 | control_app.py（deny 先于供给，`acl.denied` 审计） | 上游 hub 套件 209 通过 | 1d | EP-0-2 |
| EP-0-4 ☑ | permissions 下发 + 映射表 | `GET /api/hub/me/permissions` + console_map.py | user/admin 返回正确 denied 集 | 0.5d | EP-0-1 |
| EP-0-5 ☑ | console 过滤（复用 capabilities 管线 + vitest） | registry/permissions.ts + hubPermissionsStore + Sidebar/useSidebarEntryGroups/MainLayout | 菜单隐藏 + `/chat` 重定向 + 无 permissions 全量渲染（9 用例过） | 2d | EP-0-4 |
| EP-0-6 ☑ | 集成回归 + acl.json 运维说明 | tests/unit/hub/test_acl_integration.py（5 用例）+ examples/acl.json.example + 03 附录 B | 集成/回归全过（hub 214 通过；console 3520 通过） | 1d | EP-0-3/5 |
| EP-0-7 ☑ | fork 工程化：远程布局/CI/文档库 | `.github/workflows/enterprise-ci.yml`（hub-backend + console 两 job） | push 到 fork 成功；CI 绿 | 0.5d | Enterprise CI 两轮全绿（py3.12 + node24）；NPM Format 修复后绿；Pre-commit 修复项（black/flake8）本地同版本钩子全过，远程结论待网关恢复后补看 |

**Phase 0 DoD**：内网两角色实测——user 干净的对话+应用视图且 API 不可越权；admin 无感；
打 tag `enterprise/v0.1`。

## Phase 1 · 治理版（4-8 周）

| 票 | 内容 | 预估 | 依赖 |
|---|---|---|---|
| EP-1-1 ☑ | 模型目录表 + admin CRUD 路由 + test-connection（04 §2-3） | 3d | 19 新测试全绿 + 真机 E2E |
| EP-1-2 ☑ | runtime bootstrap env 钩子（唯一 runtime patch） | 2d | provisioner 放行 + lifespan 钩子；E2E 见 04 §7 |
| EP-1-3 ☑ | user 模型只读联动（ACL 归组 + console 隐藏入口） | 1d | ACL 规则 16 `GET /api/models` 放行 + 写 fail-closed；settings-center 对 user 整页禁用（03 denied_routes）= 添加 Provider 入口天然不可达 |
| EP-1-4 ☑ | usage 采集 + hub usage_counters + admin 用量页 | 4d | 拉取式（零 runtime patch，07 §7 偏差说明）；真机数值对账一致 |
| EP-1-5 ⊘ | 审计事件扩展（acl_denied **已随 EP-0-3 落地**；quota 预留字段 2026-09-14 追溯审计**降级并入 EP-2-3** 配额票——字段与配额语义同票实施才不返工） | — | EP-0-3 |
| EP-1-6 ☑ | `provisioners/k8s/` 六方法实现 + 单测（mock client，14 例全绿） | 5d | — |
| EP-1-7 ☑ | k8s manifest 渲染 + PVC/Service/RBAC 清单（纯 dict 构建） | 2d | EP-1-6 |
| EP-1-8 ☑ | `deploy/helm/` chart + 内网安装手册（含 bootstrap_admin initContainer；runbook=`docs/enterprise/runbook-k8s-install.md`） | 3d | EP-1-7 |
| EP-1-9 ☑ | K8s 真集群验证（kind 替代）+ 升级/重建演练（ec09a9a0；四项验收记录见 [06 §7.1](06-design-k8s.md)；runbook：`docs/enterprise/runbook-k8s-install.md`） | 3d | EP-1-8 |
| EP-1-10 ☑ | 对表例程执行（roadmap/PR 撞车复评，02 台账更新）（2026-09-14 执行：四路追溯审计 → 02 矩阵 19 行状态刷新 + 03/04/05/07/09 文档回写 + 09 白名单 v2 全量重登记；对表发现 2 个 open PR 重叠：**#7696 local admin bootstrap**（撞 `hub/bootstrap_admin.py`，若合并则 initContainer 直切官方实现）、**#7683 hub 审计**（login attempts + denied runtime creation，与 `acl.denied` 审计互补）；#7318 更新至 09-11 无新决策——两项 PR 纳入 09 §5 每月跟踪，合并即替换自研） | 0.5d | — |

**Phase 1 DoD**：04/06/07 验收节全过（06 §7.1 kind 实测 ✅、04 §7 真机 E2E ✅、07 §7 对账 ✅）。
tag 补记：`enterprise/v0.1`（Phase 0 收口 @717c85ff）、`enterprise/v0.2`（Phase 1 收口 @本文档批次）——2026-09-14 追溯审计后补打，tag 落点即各阶段最后一个代码 commit，注释注明补打原因。

## Phase 2 · 企业版（2-3 月）

| 票 | 内容 | 预估 |
|---|---|---|
| EP-2-1 | groups/policies 表 + 策略求值并入 AclEngine（05） | 3d |
| EP-2-2 | OIDC SSO（授权码 + JIT + 组映射，借 oauth_routes） | 5d |
| EP-2-3 | 配额（软硬阈值 + 代理前置检查 + UI 文案） | 3d |
| EP-2-4 | Prometheus `/metrics` + 告警规则样例 | 2d |
| EP-2-5 | Velero 备份手册 + 恢复演练记录 | 2d |
| EP-2-6 | 问数应用 M1（封装版闭环） | 5d |
| EP-2-7 | 问数 M2（多源+语义标注+admin 联动） | 5d |
| EP-2-8 | 问数 M3（指标/向量检索 + 金集 30 题） | 5d |
| EP-2-8b | 问数 M4（08 号里程碑四，收尾切片——占位票，M3 后细化） | 3d |
| EP-2-9 | runtime 受限 profile（B6，直连场景） | 2d |
| EP-2-10 | 升级金丝雀流程 + 环境分层 values（I2） | 2d |
| EP-2-11..2-14 ☑ | **平台线 P0 · 治理贯通**：trace_id 贯穿 / 审批持久化回流 / 策略 hub 下发 / 子 agent 降权（票面与 DoD 见 [11 §5](11-platform-assessment.md)；`7433d5b3`/`f16c1c67`/`885257ca`/`46dbb0d5`） | 13d |
| EP-2-15..2-19 ☑ | **平台线 P1 · 编排原语**：loop human_gate / harness registry 插件化 / 图执行引擎 / DAG 画布 / 模板市场+大厅（见 11 §5；`357c11b7`/`3063b8be`/`129c8b66`/`343e51e1`/`50894b98`） | 22d |
| EP-2-20..2-24 ☑ | **平台线 P2 · 生态补齐**：MCP server 化 / A2A 服务端 / 知识库 / 工具级 hook / Prompt 资产+Key 池（见 11 §5；`50ac4387`/`6e4f9b6d`/`785b7557`/`6c953fb2`/`231dcf4b`） | 22d |

**Phase 2 DoD**：tag `enterprise/v0.3`；05/07/08 验收节全过。

## Phase 3 · 远期（先对表官方 roadmap 再立项）

状态外置（sqlite→PG、文件→对象存储、会话→Redis）｜共享执行面｜gVisor/Kata｜
SCIM/LDAP｜SIEM｜组织四级｜数据驻留——**全部未承诺**，立项前提：官方 roadmap 未覆盖
且业务出现硬需求。

## 例程票（长期）

| 票 | 内容 | 周期 |
|---|---|---|
| RT-1 | 月度 cherry-pick 安全修复 + CI | 每月 1h |
| RT-2 | 季度 rebase 到新 tag + 白名单复核 | 每季 0.5d |
| RT-3 | 撞车复评（#7318/release notes/PR 搜索） | 每周 10min |
| RT-4 | 02 台账状态同步（完成项链接 PR） | 每阶段末 |


## Phase 0 验收操作单（手工，内网）

前置：`qwenpaw hub` 起控制面（console bundle 已构建进包或 `QWENPAW_CONSOLE_STATIC_DIR` 指向 `console/dist`）。

1. **admin 侧**：注册首个账号（自动 admin）→ 控制台全菜单可见；`curl -H "Authorization: Bearer <admin>" http://<hub>/api/config` 返回 200。
2. **user 侧**：管理员在控制台创建 member → member 登录后侧边栏只剩 收件箱/市场/应用+对话；
   `curl -H "Authorization: Bearer <member>" http://<hub>/api/config` → **403** `detail.code=ACL_DENIED`；
   浏览器直输 `http://<hub>/settings/general` → 弹回 `/chat`。
3. **WS**：member 用 WS 客户端连 `/api/voice/ws` → 关闭码 **1008**。
4. **审计**：控制台审计页（或 sqlite 查 `hub_audit_events`）出现 `acl.denied` 行，detail 含 reason/method。
5. **回归**：admin 关闭再启动 member 的 runtime、member 正常对话一轮（chat 面未被误伤）。

全部通过 → `git tag -a enterprise/v0.1 && git push origin enterprise/v0.1`，并执行一次 09 月度对齐例程。
