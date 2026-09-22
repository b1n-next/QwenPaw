# 28 · A6 Postgres 改造路线图（已拍板 2026-09-19）

> 决策：**放弃 SQLite 单副本长期化，直接外置 Postgres**（用户拍板），解锁控制面多副本 + HPA。
> 改造杠杆：全部 13 个 store 的 `_connect()` 收敛在 `connect_hub_database()` 单一函数——驱动切换一处生效。

## 1 · 现状盘点

| 项 | 数量 | 说明 |
|---|---|---|
| 使用 `connect_hub_database` 的文件 | 8 | database/auth/config/templates/registry/operations/model_catalog/acl + 子包 |
| store 类 `_connect()` | 13 | 全走同一入口 |
| SQLite 专有语法（database.py） | 24 处 | PRAGMA×2、json_valid CHECK、INSERT OR IGNORE、BEGIN IMMEDIATE、randomblob 等 |
| 散落 sqlite-only | auth.py 匿名化 `randomblob/substr(hex(...))`、operations `BEGIN IMMEDIATE` | P1 逐个方言化 |

## 2 · 分阶段

### P0 连接与方言抽象层（本轮落地）

- **入口**：`QWENPAW_HUB_DB_URL`（`postgresql://...`）；未设置=SQLite，**行为零变化**（回归即证）。
- **组件**（`hub/db_adapter.py` 新增文件，09 白名单行）：
  1. `open_hub_connection()`：URL 判别 → sqlite3 或 psycopg（`dict_row` row_factory 对齐 `sqlite3.Row` 的 `row["col"]` 访问）
  2. 占位符翻译：语句统一写 `?`，适配层 PG 路径翻译 `%s`（编译期正则，跳过字面量）
  3. DDL 方言表：`CREATE TABLE` 双版本（SQLite 版保留 json_valid 等；PG 版去 CHECK 换 jsonb 校验交由应用层 + `INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`）
  4. `BEGIN IMMEDIATE` → PG `BEGIN`（写串行化交由默认隔离级别 + 后续 advisory lock 升级）
  5. `executescript` → PG 逐语句执行
- **依赖**：`qwenpaw[postgres]` optional extra（psycopg[binary]）——默认安装不带入。
- **验证**：方言翻译/DDL 改写单测（纯函数）+ SQLite 全量回归（零变化证明）+ PG 侧建库冒烟（有实例则跑，无则标注待联调）。

### P1 store 逐个迁移（下批，1 周）

顺序按依赖+风险：database.py 核心 DDL → auth（randomblob→pgcrypto 或应用层随机）→ config/extensions → groups/templates/prompt/policy/key_pool/model_catalog/registry → usage/operations（BEGIN IMMEDIATE 语义）→ credentials vault（secrets 目录不变，仅元数据表）。
每个 store：PG 方言语句修正 + 双库回归套件（同一测试参数化跑 sqlite/pg）。

### P2 多副本 + HPA 上线（P1 全绿后，2-3 天）

- K8s Deployment replicas≥2 + 26 §1 HPA 模板启用
- 会话/缓存面复查：rate limiter 内存态→每副本独立（可接受）或外置（后续票）
- SQLite→PG 数据搬迁脚本（`qwenpaw hub migrate-sqlite-to-pg`，一次性工具）

## 3 · 风险与对策

| 风险 | 对策 |
|---|---|
| SQL 方言遗漏（隐式 sqlite 语法散落） | P1 参数化双库回归，PG 报错即测试红 |
| `BEGIN IMMEDIATE` 写串行弱化 | P0 先默认隔离级别；P2 若现竞态再加 advisory lock（`pg_advisory_xact_lock`） |
| randomblob/hex 差异 | 应用层 `secrets.token_hex` 替换（两侧一致） |
| 迁移期双栈维护成本 | 方言差异集中在 db_adapter + 少数语句；store 代码写兼容 SQL 子集 |

## 4 · 前置依赖（IT 侧）

- 🛠 内网 PG 实例（版本 ≥14）+ 一个库 + 账号（schema 权限）
- 未到位前 P0/P1 以方言单测+本地 PG（docker）推进
