# 12 · 测试体系与开发工作流

> 如何在这个 25 万行的仓库里安全地改代码：测试分层、CI 门禁、pre-commit、贡献流程。

---

## 1. tests/ 三层 + 标记体系

pytest markers（`pyproject.toml:180-196`）：`slow / p0 / p1 / p2 / unit / contract / integration / e2e / manual_real`——**p0 = PR smoke gate，p1 = nightly，p2 = 错误路径**。

| 层 | 规模 | 特点 |
|---|---|---|
| **tests/unit/** | 577 个文件、44 个子域目录（hub/governance/agents/app/channels…） | PR 必跑（`pytest tests/unit -v`） |
| **tests/contract/** | 24 个文件 | `BaseContractTest` 抽象基类框架——防"修一个 channel 子类坏其他子类"（见 `tests/contract/README.md`）；`make check-contracts` 检查渠道契约覆盖 |
| **tests/integration/** | 194 个文件 | HTTP 冒烟：每文件起**真实 QwenPaw 子进程**（随机端口，无需真 API key）；按 `-m p0/p1/p2` 分层；xdist `--dist=loadscope` 并行 |
| **tests/e2e/** | 1 个文件（hub local runtime） | CI 装 wheel 后跑 |

Makefile 速记：`make test`（全量）/ `test-unit` / `test-contract` / `test-integration` / `quick`（临时 WORKING_DIR 跑单测，最快反馈）/ `coverage-full` / `install-dev`。

## 2. e2e/（Playwright UI 测试）

pytest + playwright；`pages/` 26 个 page object（base_page + 每模块一页：chat/agents/channels/coding/mcp…）；`tests/` 45 个文件（含 `test_cov_*_deep` 深覆盖系列）；自带 pytest.ini 的模块级 markers。CI 中 e2e-smoke.yml 用 mocked API 起 dev server 跑 UI smoke。

## 3. CI 门禁（.github/workflows/，33 个）

| workflow | 职责 |
|---|---|
| **tests.yml** | **PR gate**（#7697 后 Ubuntu-only 后端矩阵：unit+contract+integration 分 shard + hub e2e；跨平台归 nightly） |
| **full-tests-nightly.yml** | 每日全 OS×Python 矩阵 + release gate 清单 |
| **e2e-smoke.yml** | Playwright UI smoke |
| **enterprise-ci.yml** | **fork 专属门禁**：hub-backend（py3.12）+ console（node24）两 job，path 过滤 hub/console，push/PR 到 feature/enterprise 触发 |
| **release.yml** + release-verify / release-window-sentinel | 统一发布流（freeze-main→resolve→build） |
| desktop-build / desktop-publish / desktop-promote | Tauri 桌面链 |
| docker-release / publish-pypi / deploy-website / plugins-release / codeql / creator-app-tests | 各产物线 |

## 4. pre-commit 钩子（.pre-commit-config.yaml）

8 类：pre-commit-hooks（check-ast/yaml/toml/json、detect-private-key 等）→ add-trailing-comma → **mypy** → **black（line-length=79）** → **flake8**（ignore E203）→ **pylint**（~30 条 disable）→ prettier（仅 .tsx，排除 console/——console 有自己的 prettier）→ actionlint。`skills/`、`scripts/pack`、`e2e/` 普遍豁免；helm 模板排除 check-yaml。

> 本地注意（fork 实测经验）：跑 `pre-commit run --all-files` 需与远程同版本钩子；python 环境用仓库 `.venv-test`。

## 5. 贡献流程（CONTRIBUTING.md）

1. Fork → 分支开发 → 本地过 `make quick` + 相关层测试；
2. pre-commit 全绿（black 79 列是最常见的格式冲突源）；
3. PR 描述 + 测试证据；PR gate（tests.yml）绿；
4. squash 工作流（上游无 merge commit）。

**本 fork 附加纪律**（ENTERPRISE.md + docs/enterprise/09）：

- 改上游文件仅限 09 白名单；其余一律新增文件；
- 需求变更先入 02 需求台账再排期；
- 每 phase 前对表官方 roadmap（撞车复评）；
- 例程票：月度 cherry-pick、季度 rebase、每周撞车扫描。

## 6. 调试与性能工具

- `qwenpaw doctor`：诊断修复（连通性/配置）。
- `scripts/startup_profile/`：`-X importtime` 采集启动导入耗时 + 函数级 trace，产出 JSON 供 viewer.html。
- `make coverage-full`：全模块覆盖率（html 报告）。
- 日志：`WORKING_DIR/qwenpaw.log`（`QWENPAW_LOG_LEVEL` 可调）。
- `scripts/verify/`、`scripts/review-bot/`：校验与评审辅助。

---

相关：[05-runtime-lifecycle](05-runtime-lifecycle.md)（启动链）、[10-hub-enterprise](10-hub-enterprise.md)（enterprise CI）、[11-console-frontend](11-console-frontend.md)（console vitest 3520 用例）
