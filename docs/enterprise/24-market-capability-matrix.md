# 24 · 应用 / 技能 / 插件市场能力矩阵（调研）

> 状态：信息收集（2025-XX，feature/enterprise）。本档只陈述现状与
> 差距，不含实现承诺 —— 按指令"暂时不需要实现，先收集信息"。

## 1. 背景

Market 页（应用 / 技能 / 插件）的官方来源均为
`platform.agentscope.io`（AgentScope 官方平台）。本文回答两个问题：

1. 当前项目 / agentscope 开源仓库是否提供市场服务端源码？
2. 企业内网部署（离线 / 私有化）下，市场能力的现实边界在哪？

## 2. 三条链路 + 一条自有先例

### 链路 ① 插件 / 应用市场（Plugin & App Market）

- **客户端**：本地代理 `src/qwenpaw/agent_market/plugins.py:1026-1078`，
  `_PLUGIN_MARKET_BASE_URL` 硬编码
  `https://platform.agentscope.io/openapi/v1/plugins`。
- **服务端**：**闭源**。`platform.agentscope.io` 的市场服务不在本
  仓库，也不在 agentscope 上游开源仓库（上游 `main` 已核对，无
  `apps/server` / 市场后端目录）。
- **结论**：应用与插件市场的**服务端无法私有化**；内网只能走
  链路 ② 的 CDN 目录 + 自建分发（见 §4 M3）。

### 链路 ② 官方插件 CDN 目录（Plugin Catalog）

- **客户端**：`scripts/pack/download_catalog.py`，`PLUGIN_DOWNLOAD_CDN`
  硬编码官方 CDN。
- **生成器**：**开源在仓库内** —— `scripts/pack/generate_plugin_metadata.py`
  （扫描 `plugins/*/plugin.json` 生成元数据）、`merge_plugin_index.py`
  （增量合并索引）、`patch_main_index.py`（主索引打补丁）。
- **清单格式**：`plugins/*/plugin.json`（fork 与上游一致，均有
  `plugins/` 目录可自产条目）。
- **结论**：**整链路可自托管**。内网可以：内网 HTTP 服务挂目录
  JSON + 插件包 → 覆盖 `PLUGIN_DOWNLOAD_CDN` → 离线安装。

### 链路 ③ 技能市场（Skill Market）

- **客户端**：`src/qwenpaw/market/`，4 个 provider：
  - `platform`（platform.agentscope.io，闭源服务端）
  - `clawhub`（ClawHub 公共服务）
  - `modelscope`（ModelScope，可自托管实例）
  - `aliyun`（需 AK 凭证，走阿里云 API）
- 服务端均为第三方 / 闭源；`modelscope` 例外（开源版可内网部署）。

### 先例 ④ fork 自有 Hub 模板市场（EP-2-19）

- Hub 侧 `template_store`（上架 / 修订 / 发布 / 实例化 / 审计），
  console 侧 Agent 大厅。**这是企业内"自有市场"的既有范式**：
  应用 = 模板（manifest + graph + skills），治理（D2 资源策略已接
  实例化门）齐备。

## 3. 硬编码点清单（内网阻断点）

| 位置 | 常量 | 值 | 可覆盖性 |
|---|---|---|---|
| `agent_market/plugins.py` | `_PLUGIN_MARKET_BASE_URL` | platform.agentscope.io/openapi/v1/plugins | 代码级（无 env/config） |
| `scripts/pack/download_catalog.py` | `PLUGIN_DOWNLOAD_CDN` | 官方 CDN | 代码级 |
| `market/` providers | provider base urls | platform / clawhub / modelscope / aliyun | 部分可配置 |

## 4. 能力矩阵（M 区，并入 02 总矩阵）

| ID | 能力 | 现状 | 内网可达性 |
|---|---|---|---|
| M1 | 插件清单格式与生成器（自产条目） | 🟡 开源（本仓库 scripts/pack + plugins/） | 改 CDN 指向即用 |
| M2 | 插件安装 / 下载（CDN 链路） | 🟡 客户端开源，目录可自托管 | 需内网目录服务 |
| M3 | 应用 / 插件市场服务端（搜索、账号、发布） | ❌ 官方闭源，无源码 | 只能自建（可仿 EP-2-19 范式） |
| M4 | 技能市场 provider 接入 | 🟡 4 provider，modelscope 可自托管 | 平台依赖不一 |
| M5 | 市场供应链安全（sha256 校验、来源签名、缓存） | ❌ 安装无摘要校验、无缓存层 | 内网目录需自证完整性 |

## 5. 内网现实边界（结论）

- **能做**：插件目录自托管分发（M1+M2，工作量小：起一个静态
  HTTP + 改 CDN 常量为配置项）；技能经 ModelScope 私有实例（M4）；
  应用层用 EP-2-19 模板市场承接（治理 / 审计 / RBAC 已在）。
- **不能做（无源码）**：复刻官方 platform.agentscope.io 的市场
  服务端（账号 / 发布 / 推荐 / 计费）。
- **风险提示**：当前插件安装路径**无 sha256 校验**（M5）——内网
  自建目录时应在分发层补完整性校验，或先在下载器中补校验再
  切内网源。

## 6. 关联

- 02 矩阵 M1-M5 行（本档 §4 同步登记）
- 09 白名单：本档新增，无上游文件改动
