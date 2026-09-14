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

> EP-2-4 交付物含"告警规则样例"：PrometheusRule 样例覆盖
> runtime 缺失 / 采集滞后（usage_last_success 年龄）/ ACL 拒绝速率
> 三条基线告警，随 `/metrics` 一并交付。

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

## 7. 实现状态（2026-09-13，EP-1-4）

| 节 | 状态 | 落点 |
|---|---|---|
| §2 计量管道 | ✅（**架构偏差：拉取式**） | 见下 |
| §2 admin 用量页 | ✅ | Hub 控制台新增"用量统计"区（按用户/模型/日期三表 + 日期过滤 + 立即采集） |
| §2 审计事件扩展（quota 预留字段） | ☐ **降级 Phase 2** | EP-1-5 并入 EP-2-3 配额票（acl.denied 事件 EP-0-3 已有；quota_exceeded/policy_hit 随配额实现）；2026-09-14 追溯审计裁定 |
| §6① 成本估算 | ☐ Phase 2 | 依赖 04 模型目录单价字段（EP-2-3 联动）；验收①按阶段拆分——token 计量与对账 Phase 1 已过，成本估算 Phase 2 |

**废弃接口对账**（2026-09-14 补记）：§2 设计的 `POST /api/hub/usage/batch`
上报路由与 `usage_events` append-only 事件表**未建**，由
`POST /api/hub/admin/usage/collect`（手动触发采集）+ `usage_counters`
快照表（PK upsert last-seen-wins 幂等）取代——拉取式架构下 runtime 无需
主动上报面。by_user 汇总以 tenant_id（personal-<uuid>，与用户 1:1）为键，
username 解析留待 Phase 2 组织视图。

**拉取式偏差说明**：原设计为 runtime 上报 hook（30s/50 条 flush + 失败重试）。
实现改为 **hub 侧 UsageCollector 每 60s 拉取**各 running runtime 的既有
`GET /api/token-usage/details`（ACL 规则 10 本就放行、hub 持 per-runtime
internal token）：

- **零 runtime patch**（优于"最薄 patch"）；无上报认证/重试缓冲的复杂度；
- runtime 的 token_usage.json 仍是唯一事实源，重复拉取天然幂等（计数器快照表 `usage_counters` upsert-last）；
- 已知取舍：非 running 的 runtime 不采集（停机期间的存量计数在下一次运行时补齐）；事件级明细（per-request）不可得，Phase 2 配额改用计数器同样可用；
- `agent_id` 归一为空串入库（SQLite 主键 NULL 不参与唯一冲突的坑）。

验证（真实 hub）：member runtime 注入 2 行（corp-gpt/demo-model 1200+340+3、
dashscope/qwen-turbo 500+90+1）→ `POST /api/hub/admin/usage/collect` 采 2 行 →
summary 总计 1700/430/4，by_user/by_model/by_date 与注入值逐项一致；
member 访问 summary 得 403。注意：**注入须在 runtime 停机时进行**——运行中的
runtime 会用内存缓存周期性覆写 token_usage.json。

## 8. 对表点

官方"Usage and observability"落地 → usage_events 表/上报协议与官方对齐；自研 hook 保持薄
（一个 flush 循环），替换成本低。
