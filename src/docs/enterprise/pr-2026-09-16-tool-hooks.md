# PR: feat(agents): tool-level pre/post/failure hooks

## 问题 / Problem

工具调用是 agent 运行时最高杠杆的控制点——但「模型选了工具」与「工具执行」之间
没有任何扩展缝。治理需求（每次调用审计、拦截危险参数形态、结果脱敏、病态工具
计时）目前只能 fork `react_agent`。

Tool calls are the highest-leverage control point in an agent runtime — yet
there is no extension seam between "the model chose a tool" and "the tool
runs". Governance needs (audit every call, block dangerous argument shapes,
scrub secrets, time out pathological tools) currently require forking
`react_agent`.

## 方案 / Approach

- **新增 `src/qwenpaw/toolhooks/`**（纯增量 ~491 行）：
  - `ToolHook` 基类三个挂点：`pre_call` 返回 `ToolHookDecision`
    （allow / **block(reason)** / **mutate(arguments)**）、`post_call` 见输出、
    `on_failure` 见异常；
  - `Registry` 通配工具名模式 + 有序分发；
  - `adapt.py` 把异构 tool-call 形态（OpenAI 分部 / 字符串 JSON）归一成
    参数字典并写回——hook 永不触碰 provider 细节；
  - `builtin.py` 三个参考实现：拒绝路径拦截、参数密钥脱敏、计时护栏。
- **`react_agent._execute_tool_call`**：原有 coerce 逻辑不动，hook 漏斗包住
  基类调用（pre → execute → post 或 failure，`finally` 保证失败也分发）。
  **未注册 hook 时纯透传。**
- `examples/tool_hooks/demo.py` 可运行端到端演示。

## 兼容性 / Compatibility

零 hook 注册 = 零行为变化。本分支 agents 套件回归 **3152 全绿**。

## 测试 / Tests

16 例：context 构建 + 参数归一 · 决策语义（allow/block/mutate）· registry
模式与顺序 · 参数改写回写 · block 短路 · 失败分发 · 计时 hook · 漏斗接线 ·
demo 执行。

## 设计取舍 / Notes

- 三个挂点刻意最小——不做转换链、不做异步 hook（治理动作都是本地快速判定）；
  需要更重的编排时应该用 loop-level gate 而不是 tool hook。
- `trace_id` 在此层为可选 best-effort（请求上下文存在才有值），不引入对 app
  包的硬依赖。
