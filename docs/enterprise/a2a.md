# A2A 服务端与核心客户端（EP-2-21）

QwenPaw 以标准 A2A 1.0（JSON-RPC profile）对外提供智能体能力；`a2a/client.py` 是所有 QwenPaw 侧集成（cloudpaw 插件、hub 桥、示例）复用的核心客户端。

## 服务端

| 端点 | 说明 |
|---|---|
| `GET /.well-known/agent-card.json` | 标准发现文档；skills = 已发布图模板 + 内置 chat |
| `POST /api/a2a` | JSON-RPC 2.0：`message/send` / `message/stream` / `tasks/get` |

- `message/send`：提交 agent chat 任务并轮询——完成返回 agent `Message`，仍在跑返回 `Task{status:"working"}`（客户端用 `tasks/get` 续查）
- `message/stream`：SSE 事件流（首个事件带 `first:true`，终态事件带 `final:true`，随后 `data: [DONE]`）
- 协议错误：未知方法 `-32601`、参数缺失 `-32602`、runtime 不可达 `-32000`

agent 对话复用既有 inter-agent chat task API（`agent_id` 可在 message 里指定，默认 `default`）。

## 核心客户端 `qwenpaw.a2a`

```python
from qwenpaw.a2a import A2AClient, discover_agent_card

card = discover_agent_card("https://peer.example.com")
client = A2AClient("https://peer.example.com")
answer = client.send_message("你好")          # Message 或 working Task
for event in client.stream_message("你好"):    # SSE 迭代
    ...
```

## 测试形态（DoD）

`tests/unit/a2a/test_server.py`：7 例——card 模板注入、send 完成回 agent Message（提交载荷透传验证）、慢任务回 working Task + `tasks/get` 查完成、空文本 `-32602`、SSE 事件序列（first/working/最终 message/[DONE]）、**真 uvicorn 端口上核心客户端发现+调用+流式**（DoD：标准 A2A 客户端发现并调用成功）。
