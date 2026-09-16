# PR: feat(knowledge): built-in knowledge base with retrieval tool

## 问题 / Problem

Agent 只会从参数记忆作答；想让它参考产品文档/运维手册/内部笔记，今天就得先
搭一个外部向量库（或接插件）才能问第一个问题。

Agents answer from parametric memory alone; pointing one at product docs,
runbooks, or institutional notes today means standing up an external vector
store (or wiring a plugin) before the first question.

## 方案 / Approach

**零新依赖**（stdlib + SQLite），个人/小团队够用，且留了换更重检索的缝：

- **`knowledge/`**（新包，~464 行）：
  - `store.py`：WAL `knowledge.db`——docs + chunks 表（向量 JSON 存储），
    增/列/删（级联清理）；
  - `chunker.py`：段落感知切分 + 尺寸上下界；
  - `embedder.py`：`EmbeddingGateway`——配置了 agentscope embedding 模型且
    可达就用真向量，否则**确定性 hash 兜底**（离线/CI 全功能可用；当前
    模式总是上报给调用方）；
  - `retriever.py`：全 chunk 余弦 top-k。
- **API（`/api/knowledge`）**：`POST /documents`（表单）· `POST
  /documents/upload`（multipart UTF-8）· `GET /documents` · `DELETE
  /documents/{id}` · `POST /search`——`_app.py` 仅 5 行接线。
- **agent 工具**：`knowledge_search` 经标准 `@tool_descriptor` 注册表挂入
  （`tools/__init__.py` 1 行 import），任何 agent 默认获得检索能力。

## 兼容性 / Compatibility

不配置知识库时零行为变化（工具查询空库返回空结果）。本分支回归：
knowledge+app 2383 绿 · agents/tools 736 绿。

## 测试 / Tests

12 例：chunker 边界/重叠 · embedder hash 兜底确定性 · store 级联删除 ·
检索排序 · 真实 app 的 add-search-delete 全流程。

## 设计取舍 / Notes

- 向量存 SQLite（JSON）而非外部引擎：目标场景是单机/小团队；到需要
  ANN 索引的规模时，`retriever.search` 是唯一需要替换的函数。
- hash 兜底是有意为之：embedding 服务不在（离线、CI、气隙环境）时功能
  完整可用，`embedding_mode` 字段让上游永远知道当前拿的是哪种质量。
