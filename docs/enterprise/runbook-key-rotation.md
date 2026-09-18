# 运行手册 · Key 轮换（E8）

> 覆盖 hub 持有的两类凭据：模型 Provider API key（管理员目录）与
> runtime 内部边界 token。OIDC client secret 走 A4 通道
> （helm Secret → env → vault 一次性导入），不在本手册范围。

## 1. 模型 Provider Key 轮换（fail-closed）

**端点**：`POST /api/hub/admin/models/providers/{provider_id}/rotate-key`

```json
{"api_key": "<new-key>"}
```

**流程**：

1. hub 先用**新 key** 探测 `GET {base_url}/models`（8s 超时）；
2. 探测 2xx–4xx（<500）→ 视为可达，新 key 落库（Fernet 加密），
   记审计 `model_catalog.key_rotated`；
3. 探测 5xx / 连接失败 → **409 拒绝轮换，旧 key 原样保留**，
   记审计 `model_catalog.key_rotate_rejected`。

**语义要点**：

- 轮换即生效——网关下一次取 key 就用新值（`catalog.key` 每请求
  现取，无缓存宽限）；
- 上游侧建议先双 key（旧 key 未过期）再轮换，天然零中断；
- 密钥永不回显（`api_key_set` 布尔）。

## 2. Runtime 内部 Token 轮换（宽限双值）

**端点**：`POST /api/hub/admin/runtimes/{runtime_id}/rotate-token`

**流程**：

1. 生成 48 字节 urlsafe 新 token；
2. 旧值另存 `..._INTERNAL_TOKEN_PREVIOUS`（同 vault scope）；
3. 新值覆盖 `..._INTERNAL_TOKEN`；
4. 审计 `runtime.token.rotated`（含 grace 24h 说明）。

**生效时序（诚实账）**：

| 时点 | 行为 |
|---|---|
| 轮换后立即 | hub→runtime 主动推送（graph/publish 等）新值若 401，自动退 `PREVIOUS` 重试一次（宽限回退） |
| runtime 下次 (重)启 | provision 注入 env=新值，全面切换 |
| 再次轮换 / runtime 重建 | `PREVIOUS` 被覆盖/清理 |

- 在跑 runtime 侧校验的仍是 env 旧值（`app/auth.py` 常驻比对），
  这正是宽限回退存在的原因；
- 用量采集（EP-1-4）与日志留存（F7）拉取同样走该 token——
  拉取失败只记 `last_error`，下轮重试，不影响轮换进行。

## 3. 建议节奏

| 凭据 | 建议周期 | 触发 |
|---|---|---|
| Provider API key | 季度或上游强制 | admin 手动（上游先双 key） |
| Runtime token | 半年或疑似泄露 | admin 手动 + 观察 runtime 重启窗口 |
| Hub admin 密码 | — | bootstrap 独立流程（#7696） |

## 4. 验证清单

- [ ] 轮换后 `GET /providers/{id}/test` 返回可达；
- [ ] 审计链 `admin/audit` 可查 `key_rotated` / `token.rotated` 事件；
- [ ] runtime 重启后 `admin/runtimes` 状态 running、推送正常；
- [ ] （泄露场景）轮换 + runtime 重建后旧 token 全部失效。
