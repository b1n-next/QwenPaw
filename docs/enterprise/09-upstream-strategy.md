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

**修改白名单**（仅允许改动的上游文件，当前合计 ≤6 处、每处 ≤50 行）：

| 文件 | 改动 | 所属 |
|---|---|---|
| `src/qwenpaw/hub/control_app.py` | ACL 两处接入 + /api/version permissions | Ph0 |
| `src/qwenpaw/app/_app.py`（或 providers loader） | 模型 bootstrap env 分支 | Ph1 |
| runtime usage 上报 hook（1 文件） | usage flush | Ph1 |
| `console/src/App.tsx` + `builtinMenu.ts` 相邻新增文件 | permissions 过滤（组合进 capabilities 管线） | Ph0 |
| `tests/unit/hub/test_control_app.py` | 2 处 member 探针 `/api/probe`→`/api/agents`（ACL 后未知路径对 user 拒绝） | Ph0 |
| `console/.prettierignore` | +1 行：忽略 `pnpm-lock.yaml`（pnpm 每次重生成，格式不受 prettier 管） | Ph0 |

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
