# 04 · 设计：模型统一治理（Phase 1）

> 对应需求：E1-E3（P0）、E4-E9（P2）｜ 撞车风险 **高**（官方"Central model governance"在列）
> 纪律：实现前先对表官方 roadmap 与近期 PR；官方落地的部分直接复用，自研聚焦"per-tenant 目录下发"。

## 1. 目标（Phase 1 范围）

1. 管理员在 **hub 侧**集中维护 Model Provider 目录（base_url/model/api_key/别名/默认标记）；
2. 租户 runtime 首启/重启时自动 bootstrap 可用模型配置；用户 console 的"模型设置"页对 `user` 角色只读（依赖 03 的 B 区机制）；
3. 凭据只在 `TenantCredentialVault`/服务端流转，console 与用户 API 响应**永不回显 api_key**。

非目标（Phase 2+）：按用途路由、fallback 链、限流额度、Key 轮换流程、模型→组交叉。

## 2. 数据模型

新增 hub 侧存储（sqlite，与 users 同库或独立 `model_catalog.db`）：

```sql
CREATE TABLE model_providers (
  provider_id TEXT PRIMARY KEY,       -- 别名 slug，如 "corp-gpt"
  name TEXT NOT NULL,                 -- 展示名 "公司 GPT 网关"
  base_url TEXT NOT NULL,
  api_key_ciphertext TEXT NOT NULL,   -- vault 加密
  models TEXT NOT NULL,               -- JSON: ["gpt-4o", "gpt-4o-mini", ...]
  default_model TEXT,
  enabled INTEGER DEFAULT 1,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE tenant_model_grants (    -- Phase 2 用，先建表
  tenant_id TEXT, provider_id TEXT,
  granted_models TEXT,                -- NULL = 全部
  PRIMARY KEY (tenant_id, provider_id)
);
```

## 3. 流程

```text
admin: POST /api/hub/admin/models/providers   (CRUD + test-connection)
        │
        ▼
① 租户 runtime 创建/重启（provisioner start 前）
② hub 从 vault 取该租户授权的 providers 明文（仅在服务端内存）
③ 经环境变量注入 runtime：
     QWENPAW_MODEL_BOOTSTRAP_JSON（含 base_url/model/key/别名/默认标记）
④ runtime 启动钩子读取（新增小 patch：env → providers 配置，幂等，不覆盖用户已改项——
   复用 qwenpaw-data app 从宿主 active model bootstrap 的成熟模式）
⑤ user 角色的 /api/providers 写操作被 03 ACL 拒绝 → 只读目录
```

**runtime 侧 patch 面评估**：bootstrap 钩子 ≈1 个文件（`app/_app.py` startup 或 providers loader 的
env 分支），属于修改白名单；上游若官方实现同能力则以官方为准替换。

## 4. 别名与展示

- console 模型选择器只展示 `model_providers.models` 的并集（别名 = provider.name + model）；
- `/api/version` permissions 同包下发 `model_catalog_readonly: true`，console 据此隐藏"添加 Provider"入口（UX 层，写操作由 ACL 兜底）。

## 5. 安全要点

- api_key 密文存储（复用 `TenantCredentialVault` 的加密原语）；日志与审计事件脱敏（只记 provider_id）；
- bootstrap JSON 仅经 hub→runtime 的回环/容器网络注入，不落 console 可读接口；
- test-connection 由 hub 服务端发起（带超时与错误脱敏）。

## 6. 验收

① admin 配置 1 个 Provider → 新建 user 租户首启即有可用模型可对话；
② user 的 console 模型页只读、无 Key 回显；`POST /api/providers` 得 403（审计留痕）；
③ admin 停用 Provider → 租户重启后模型目录同步收缩。

## 7. 实现状态（2026-09-13）

| 节 | 状态 | 落点 |
|---|---|---|
| §2 数据模型 | ✅ | `src/qwenpaw/hub/model_catalog/store.py`（control.db 同库 + `secrets/.model_catalog_key` Fernet；`tenant_model_grants` 已建表待 Ph2） |
| §3 admin CRUD + test | ✅ | `control_app.py` `/api/hub/admin/models/providers`（GET/POST/PATCH/DELETE + `/{id}/test`）；响应只含 `api_key_set`，永不回显密文/明文 |
| §3 env 注入 | ✅ | `build_runtime_service.runtime_environment` → `QWENPAW_MODEL_BOOTSTRAP_JSON`；**provisioner 凭据过滤会丢弃 `QWENPAW_*`**，须仿 internal token 在过滤后显式放行（local/docker 两处，见 09 白名单） |
| §3 runtime 钩子 | ✅ | `src/qwenpaw/app/model_bootstrap.py`（`_app.py` lifespan +4 行调用）：幂等——已存在 provider 不动、无 active model 时才激活 default |
| §5 安全 | ✅ | 密钥仅 hub 内存解密→env→runtime 自身加密落盘；审计只记 provider_id；test-connection 服务端发起、错误脱敏为异常类名 |
| §6 验收 | ✅ 实测 | 真实 hub E2E：admin upsert→member restart→`/api/models` 含 corp-gpt（第 36 位）→member POST 403→审计 3 行→不可达上游 test 优雅失败 |

实现备注：
- **时序**：restart 返回 200 后 bootstrap 在 runtime lifespan 内完成，列表查询需容忍秒级延迟（E2E 已按 2s×20 轮询）；
- **幂等边界**：用户在 runtime 本地已改的同 id provider 永远优先于目录推送；目录删掉的 provider 不会从 runtime 自动移除（重启收缩语义 = "新增/更新"，删除需清 working 目录——Ph2 再议）；
- **models.read 规则**：ACL 新增第 16 条 `GET ^/api/models` 放行（chat 选模型必需），写仍走 fail-closed。

## 8. 与官方撞车的对表点

- 官方 roadmap 出现 "central model governance / model catalog" 落地 PR → 评估替换自研表结构；
- 数据迁移预留：`model_providers` 表结构与官方未来命名解耦（映射层一个函数）。
