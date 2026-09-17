# QwenPaw · Enterprise Layer（企业级增强层）

本 fork 在上游 [agentscope-ai/QwenPaw](https://github.com/agentscope-ai/QwenPaw) 之上维护一层
面向**内部可信网络**的企业增强：控制台/菜单权限、模型统一治理、用户组 RBAC 与 SSO、
K8s 部署、用量可观测、通用问数应用。

- 📚 文档库入口：[`docs/enterprise/README.md`](docs/enterprise/README.md)（总纲 → 需求台账 → 各模块设计 → WBS）
- 🌿 开发分支：`feature/enterprise`（fork：`b1n-next/QwenPaw` = `origin`；上游 = `upstream`）
- 📌 基线：commit `983b3ceb`（上游 v2.2.1 发布后、2.2.2b1 线；未打 tag，以 commit 为准）
- 🔧 维护纪律：修改上游文件仅限 [09 白名单](docs/enterprise/09-upstream-strategy.md)；其余一律新增文件
- ⚠️ 使用边界：内部可信成员；不面向互不信任的公网多租户（与上游定位一致）

上游同步、cherry-pick 与 rebase 例程见 09；需求变更先入
[02 台账](docs/enterprise/02-requirements-matrix.md) 再排期。
