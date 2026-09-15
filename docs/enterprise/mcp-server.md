# MCP Server（EP-2-20）

把 QwenPaw agent 与图智能体暴露为 MCP server，供外部宿主（Claude Desktop 等）调用。

## 传输

| 传输 | 入口 | 说明 |
|---|---|---|
| stdio | `python -m qwenpaw.mcp_server` | 每行一个 JSON-RPC 2.0；stdout 回写，通知无输出（Claude Desktop 配置形态） |
| HTTP | `POST /api/mcp-server` | 单端点 JSON 模式（请求一条消息，回一条 JSON；通知回 202）。注意：`/api/mcp/*` 是既有的 MCP **客户端**管理面，语义相反 |

## 工具（schema 自动生成于 `tools/list`）

| 工具 | 参数 | 行为 |
|---|---|---|
| `agent_chat_submit` | `message`, `agent_id?` | 向 agent 提交一条后台对话任务，返回 task_id |
| `agent_task_status` | `task_id`, `agent_id?` | 轮询任务状态与最新输出 |
| `graph_run` | `template_id`, `inputs?` | 从已发布图模板启动 run；human gate 挂起返回 suspended 状态 |
| `graph_resume` | `run_id`, `route` | 裁决挂起的 gate（approve/deny），返回终态 |

## Claude Desktop 配置示例

```json
{
  "mcpServers": {
    "qwenpaw": {
      "command": "python",
      "args": ["-m", "qwenpaw.mcp_server"],
      "env": { "QWENPAW_WORKING_DIR": "/path/to/working/dir" }
    }
  }
}
```

（agent 对话经本地 runtime 的 inter-agent chat task API；确保 runtime 在运行。）

## HTTP curl 示例

```bash
curl -X POST http://127.0.0.1:8088/api/mcp-server \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## 协议细节

- `initialize` → protocolVersion `2025-03-26` + `capabilities.tools` + `serverInfo`
- 工具级错误（模板不存在、runtime 不可达）以 `isError: true` 的 tool result 返回，不炸协议层
- 协议级错误：未知方法 `-32601`、未知工具 `-32602`、内部错误 `-32603`
