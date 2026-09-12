# QwenPaw 企业级增强层 · 文档库

> 分支 `feature/enterprise` ｜ 基线 `983b3ceb`
> 定位：内部可信网络环境的企业增强层。路线与边界见 01。

## 阅读顺序

| 文档 | 内容 | 何时读 |
|---|---|---|
| [01-master-plan.md](01-master-plan.md) | 定位/现状盘点/纠错记录/分层架构/阶段路线/风险 | **先读**，全貌 |
| [02-requirements-matrix.md](02-requirements-matrix.md) | 需求全覆盖台账（A-K 区，含优先级/现状/撞车） | 任何排期/设计前查 |
| [03-design-console-permission.md](03-design-console-permission.md) | Phase 0：代理 ACL + 菜单权限 | 立即（首个开发切片） |
| [04-design-model-governance.md](04-design-model-governance.md) | 模型统一治理 | Phase 1 启动时 |
| [05-design-identity-rbac.md](05-design-identity-rbac.md) | 用户组/RBAC/OIDC | Phase 2 启动时 |
| [06-design-k8s.md](06-design-k8s.md) | K8s Provisioner + Helm | Phase 1 启动时 |
| [07-design-observability.md](07-design-observability.md) | 用量计量/配额/指标 | Phase 1-2 |
| [08-design-data-app.md](08-design-data-app.md) | 通用问数应用（PawApp） | Phase 2（M1 可提前） |
| [09-upstream-strategy.md](09-upstream-strategy.md) | fork 维护/对齐例程/patch 白名单 | 每次动上游文件前 |
| [10-task-plan.md](10-task-plan.md) | WBS 票据与 DoD | 排期与周会 |

## 快速事实（防遗忘）

- 权限唯一强制点：hub `personal_runtime_proxy` + WS 入口；菜单过滤只是 UX；
- 每租户一 runtime（Pod）模型保持上游对齐；K8s 下 PVC 用 RWO 即可；
- 上游日均 ~9 commits：**新文件优先，改上游文件须在 09 白名单内**；
- 撞车高危区（模型治理/RBAC/Helm）：每 phase 前对表官方 roadmap（#7318）。
