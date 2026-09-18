# qa-data · 问数（M1 封装版）

> 票：J4 / EP-2-6（08 号设计 §5 M1）——单数据源 + schema 内省 +
> 两把治理工具 + Ask 页。SQL 透明、结果成表、fail-closed 守卫。

## 配置（M1 单源，写死语义）

| env | 默认 | 说明 |
|---|---|---|
| `QADATA_DATABASE_URL` | `sqlite:////tmp/qa-data-demo.sqlite` | SQLAlchemy 连接串（mysql/pg 同形） |

## 组件

- `backend/guard.py` — sqlglot 只读守卫：仅 SELECT/SHOW/DESCRIBE、
  禁多语句与写型 CTE、缺 LIMIT 自动注入（硬顶 500 行）
- `backend/introspect.py` — schema 内省缓存（TTL 10min + 手动刷新，
  关键词打分检索）
- `backend/main.py` — PawApp：`search_schema` / `run_readonly_sql`
  两把 agent 工具 + `/api/qa-data/{tables,refresh,ask}` 路由
- `ui/index.js` — Ask 页（host React，无构建链）：选表（可选）→
  提问 → SQL 代码块 + 结果表格
- `agents/qa-data/` — 问数人格（先检索后生成 / SQL 必展示 /
  口径诚实）

## M1 验收状态

代码闭环 + 单测 10 例全绿；**金集 10 题人工判**（08 §5）待应用线
接入真实模型与业务库后执行。M2（多源/语义标注/admin 联动）、
M3（指标/向量/金集回归）见 08 号设计。
