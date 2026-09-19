# 25 · A11 供应链安全（SBOM + 制品校验）

> 2026-09-19 · 状态：**内网指南已落（文档面）**；签名验证链路依赖内网 cosign/Registry 部署，落地动作见 §4
> 范围：QwenPaw Hub 部署的第三方制品（Python 包、npm 包、容器镜像、runtime 插件包）可追溯、可校验。

## 1 · SBOM 生成

| 制品 | 命令 | 格式 |
|---|---|---|
| Python（后端） | `pip install cyclonedx-bom && cyclonedx-py requirements --output-format JSON -o sbom-python.json` | CycloneDX |
| npm（console） | `cd console && npm ci && npm run sbom`（`@cyclonedx/cyclonedx-npm -o --output-format JSON`） | CycloneDX |
| 容器镜像 | `syft <image-ref> -o cyclonedx-json > sbom-image.json` | CycloneDX |

产物入库位置：`releases/sbom/<version>/`（随 02 矩阵 A11 行引用）。SPDX 等价可用 `--spec-version` 切换；内网建议统一 CycloneDX（dep 工具链兼容好）。

## 2 · 依赖锁定与最小信任

- 后端：`pyproject.toml` + `uv.lock`（或 pip-tools `requirements.lock`）双入库；**禁止浮动版本部署**。
- console：`package-lock.json` 已入库，`npm ci`（非 `npm install`）构建。
- runtime 插件包（`plugins/`）：目录内自带 `requirements.txt`/锁文件；安装时走 J1 凭据 vault（不落明文）。
- 上游同步（09 纪律）：merge 后重跑 SBOM diff，新增依赖必须在 25 §5 表登记来源与用途。

## 3 · 镜像校验（内网 Registry）

1. **digest 固定**：部署清单/K8s manifest 引用 `@sha256:<digest>` 而非 tag（tag 可变，digest 不可）。
   `docker pull registry.local/qwenpaw@sha256:...`
2. **签名验证（cosign，内网部署后启用）**：
   ```bash
   cosign verify --key <内网公钥> registry.local/qwenpaw@sha256:<digest>
   ```
   公钥经 out-of-band 分发（内网配置库），不随镜像走。
3. **Policy 入口**：K8s admission（cosign webhook）或本地 `crane manifest --raw | sha256sum` 对账。

## 4 · 落地检查单（内网启用前）

- [ ] SBOM 生成纳入 release 流程（I4 灰度管线的前置门）
- [ ] 内网 cosign 私钥/公钥对初始化，CI 签名步骤接入
- [ ] 部署 manifest 全量改 digest 引用
- [ ] runtime 插件包安装路径补摘要校验（与 M5 同一缺口，见 24 §5）
- [ ] 每季 SBOM diff 复审（新增依赖登记）

## 5 · 已知缺口登记

| 依赖 | 状态 |
|---|---|
| cosign 内网实例 | 未部署（IT 侧） |
| 插件安装摘要校验 | M5 票（❌） |
| K8s admission 签名策略 | 随 K8s 面（06 设计）推进 |
