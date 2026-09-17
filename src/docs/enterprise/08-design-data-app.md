# 08 · 设计：通用问数应用（Phase 2）

> 对应需求：J1-J9 ｜ 载体：PawApp（`src/qwenpaw/pawapp/` SDK，页面型应用，零 console 侵入）
> 结论回顾：通用版可行；推荐**路线 B 自研轻量 PawApp**（不绑 Neo4j/QwenPaw-Data 栈），
> QwenPaw-Data（`plugins/apps/qwenpaw-data/`）作为架构参照与路线 A 备选。

## 1. 路线决策记录

| | 路线 A：定制 QwenPaw-Data | 路线 B：自研轻量 PawApp（选定） |
|---|---|---|
| 语义层/图谱 | 现成（指标/维度/血缘/Neo4j） | 自建（元数据+语义描述+可选向量） |
| 依赖 | Neo4j + context service 四包 | 仅宿主 Python + sqlalchemy + sqlglot |
| 上游耦合 | 高（0.1.x 快速演进，含需 vendored 的内嵌 console） | 低（只用稳定 PawApp SDK） |
| 定制自由 | 受上游架构约束 | 完全可控 |
| 适用 | 需要图谱/血缘/自进化语义的中长期场景 | 内网问数快速可用、渐进增强 |

**触发切换到 A 的条件**：出现血缘分析、跨源语义编织、自进化记忆等硬需求。

## 2. 应用结构（新增 `plugins/apps/qa-data/`，零侵入）

```text
plugins/apps/qa-data/
  plugin.json          # type=app, launch_scope=page, entry_page=/apps/qa-data
                       # permissions: chat/storage/network(loopback)
  backend/
    main.py            # PawApp("问数", app_id="qa-data") + enable_standard_capabilities()
    datasources.py     # 数据源 CRUD + 连通测试（凭据走加密存储）
    introspect.py      # schema 内省（sqlalchemy inspector；缓存+手动刷新）
    semantic.py        # 语义层 CRUD（表/列描述、指标、维度、同义词、示例问答）
    guard.py           # 只读 SQL 护栏（sqlglot 解析：仅 SELECT/SHOW/DESCRIBE；
                       #   禁多语句；强制 LIMIT 注入；超时+行数上限）
  ui/                  # Vite+React，registerPage 挂 /apps/qa-data
    Ask.tsx            # 对话页：源/表选择器、流式输出、SQL 块、结果表格
    Sources.tsx        # 数据源管理（admin 语义见 §4）
    Semantic.tsx       # schema 标注与语义维护
  agents/qa-data/      # PROFILE.md / SOUL.md 人格
```

## 3. 智能体资产（成败关键，非 UI）

- `@app.tool search_schema(query, datasource_id)`：按问题检索相关表/列——
  实现：列名/注释/语义描述的关键词匹配起步（BM25 式打分），Phase 2 末接向量（宿主 embedding）；
- `@app.tool run_readonly_sql(sql, datasource_id, max_rows)`：经 guard.py 治理执行，返回行+列+截断标记；
- `app.prompt_section`：问数人格注入——"先 search_schema 再生成 SQL；SQL 必须展示；
  结果以紧凑表格呈现；口径不确定时引用语义描述并说明"；
- 工具描述文案按 J7 金集迭代（工具描述质量直接决定 NL2SQL 准确率）。

## 4. 与企业层联动（J9）

- 数据源**管理页**（Sources/Semantic）标记为 admin 能力：`/api/version` permissions 框架（03）
  下发 app 级 `denied_routes`，app UI 据此隐藏管理入口；对应后端路由在 hub ACL 归 admin 组；
- **问数对话页**对 user 开放：普通租户 "开箱即问"；
- 数据源凭据：存 PawApp 加密存储（宿主 plugin storage），Key 永不回显前端；
- 模型：复用 04 的集中目录（app 不自配模型）。

## 5. 里程碑（对齐总纲 Phase 2，M1 可提前至 Phase 1 空闲周）

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1 封装版 | 单数据源（配置文件写死）+ 内省 + 两工具 + Ask 页 | 10 个内网典型问题 ≥7 个正确（人工判） |
| M2 多源通用 | Sources/Semantic 页 + 选源提问 + 凭据加密 | 增删数据源不断服务；Key 无回显 |
| M3 语义层 | 指标口径/维度/同义词/示例问答 + schema 向量检索 | 金集 30 题准确率 ≥80% 并可回归 |
| M4 增强 | 图表（echarts）/导出 CSV/行列权限 | 按需 |

## 6. 金集评测（J7，随 M1 建立）

`tests/qa_data/golden/`：`question → datasource → expected_sql_shape（关键表/过滤/聚合断言）`；
CI 跑静态断言（不连真库）； nightly 连内网测试库跑结果比对；每次改 prompt/工具描述必须过金集。
