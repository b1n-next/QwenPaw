# 上游 Issue 草稿 · Conversation-level token accounting

> 状态：**草稿，待用户过目后提交**（英文正文在下，可直接粘贴）
> 提交目标：agentscope-ai/QwenPaw · labels 建议：`enhancement`, `billing`, `token-usage`

---

**Title:** Track session_id in persisted token usage (conversation-level accounting)

## Problem

`TokenRecordingModelWrapper` already buckets usage by session (`_usage_by_session[session_id]`, model_wrapper.py), but that dimension is discarded at persist time:

- `TokenUsageRecord` (token_usage/manager.py) has no session field — a row is `(date, agent_id, provider_id, model)`.
- Reporting to a control plane therefore aggregates away the conversation granularity.

For multi-tenant deployments that need per-conversation billing or per-user dashboards, the data is collected but cannot be stored or reported.

## Proposal

Carry the session dimension through the existing pipeline (no new instrumentation):

1. `TokenUsageRecord` gains optional `session_id: Optional[str] = None` (backwards compatible).
2. Persistence keeps a per-session detail table (append-only), leaving the existing daily aggregate table untouched — quota/limit reads stay on the aggregate path with zero migration risk.
3. The usage reporting payload carries `session_id` as an optional field; older control planes ignore it.
4. Optional: a query API `usage_for_session(session_id)` for conversation drill-down.

## Context

We run QwenPaw behind a multi-tenant control plane (fork docs: fork-side evaluation doc, seven-step migration checklist C1–C7 with a side-table design that keeps aggregate semantics intact). Happy to contribute a PR for items 1–3 if maintainers agree on the side-table approach — the alternative (widening the main aggregate PK) changes every quota read path and seems riskier than it's worth.

## Alternatives considered

- Application-level join of logs: brittle, loses precision at rotation.
- Rewriting the aggregate PK to include session_id: explodes row count and touches every existing consumer.
