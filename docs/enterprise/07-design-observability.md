# 07 · 设计：用量计量与可观测（Phase 1-2）

> 对应需求：F1-F8 ｜ 基线：`HubOperationsStore` + `/api/hub/admin/audit` + `/api/hub/admin/overview` 已有骨架。

## 1. 原则

- **计量在 runtime 采集、hub 汇总**；不在代理层解析 SSE（流式协议脆弱，上游事件格式会变）；
- 审计先扩展现有 operations store（sqlite），不引入新存储；导出接口（F9/SIEM）留 backlog。

## 2. 计量管道（Phase 1：F1/F2/F6/F8）

```text
runtime: 每次 LLM 调用的 usage（prompt/completion tokens、model、agent_id、session）
   └─ 批量上报 POST /api/hub/usage/batch（新增 hub 路由；runtime 侧为一个上报 hook，
      每 30s 或 50 条 flush；失败本地暂存重试——hook 是 runtime 侧唯一 patch 点）
hub: usage_events 表（append-only）+ 内存聚合缓存
   ├─ admin console 用量页：per-user / per-model / per-day（复用 overview 页扩展）
   └─ 审计事件扩展：acl_denied / quota_exceeded / policy_hit（03/05 的引擎统一写入）
```

## 3. 配额（Phase 2：F3）

- 配额主体：user / group；维度：日 token、日请求数、月成本估算（token×单价表，单价在模型目录维护，04 联动）；
- 执行点：hub 代理层前置检查（在 03 ACL 之后、转发之前）；
- 软阈值（默认 80%）：写审计 + console 顶部提示；硬阈值（100%）：403 + 明确错误码 `QUOTA_EXCEEDED`；
- 余量缓存 30s，避免每请求查库。

## 4. 指标导出（Phase 2：F4）

- hub 暴露 `/metrics`（Prometheus 文本格式，无第三方依赖手写 collector）：
  `qwenpaw_hub_requests_total{role,group,decision}`、`qwenpaw_hub_tokens_total{user,model}`、
  `qwenpaw_runtime_state{tenant,state}`、ACL/配额拒绝计数；
- runtime 指标暂不直接抓取（内网多租户拓扑复杂），经 usage 管道间接聚合。

## 5. 日志（Phase 2：F7）

- runtime Pod 日志走 K8s 原生（kubectl logs / 集群日志方案），hub 不汇聚；
- hub 自身结构化日志按请求带 `request_id`（上游已有 `x-request-id` 透传，补全落日志字段）。

## 6. 验收

① admin 用量页能按用户/模型/日查询 token 与成本估算，与 runtime 侧账目抽样对账误差 0；
② 用户达软阈值见提示、硬阈值请求 403 且 UI 有明确文案；恢复窗口自动放行；
③ `/metrics` 可被 Prometheus 抓取，ACL 拒绝与配额熔断有计数；
④ 审计事件含四要素（who/what/when/reason），不可由普通 API 删除。

## 7. 对表点

官方"Usage and observability"落地 → usage_events 表/上报协议与官方对齐；自研 hook 保持薄
（一个 flush 循环），替换成本低。
