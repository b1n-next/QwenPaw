# 05 · 设计：身份与 RBAC 扩展（Phase 2）

> 对应需求：C2-C3、C7、D2-D5 ｜ 撞车风险 **高**（官方"用户组、RBAC"在列）
> 基线事实：`hub/auth.py` 已有 users 表（user_id/username/role/disabled/token_version/profile_json）、
> 注册开关、锁定；本设计是**扩展**，不重建。

## 1. Phase 2 范围

1. 用户组：`groups` + `group_members` 表；一个用户多组；组即策略挂载主体；
2. 策略引擎（最小版，静态配置）：subject（role/group/user）→ resource（API 组/菜单组/Agent/Skill/模型）→ effect；
3. OIDC SSO：企业 IdP 登录，JIT 建号，组映射；
4. 与 03 的联动：`denied_groups` 的计算从"role 查表"升级为"策略引擎求值"。

非目标：SCIM、四级组织、委托授权、OPA（C4/C5/C6/C8 → backlog）。

## 2. 数据模型（hub sqlite 迁移）

```sql
CREATE TABLE groups (group_id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT DEFAULT 'local'); -- local|oidc
CREATE TABLE group_members (group_id TEXT, user_id TEXT, PRIMARY KEY(group_id, user_id));
CREATE TABLE policies (
  policy_id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,      -- "role:user" | "group:finance" | "user:u123"
  resource TEXT NOT NULL,     -- "apigroup:admin" | "menu:settings" | "agent:qa-bot" | "model:corp-gpt"
  effect TEXT NOT NULL        -- allow | deny
);
```

求值顺序：user 显式 > group > role；默认 03 的 fail-closed 分组表继续作为兜底。
**enforcement 仍在 hub 代理层一处**（03 的 `AclEngine` 升级为读 policies），runtime 零改动。

## 3. OIDC SSO

- 借力点：`hub/oauth_routes.py` 已有 OAuth 回调中转骨架与 `HUB_OAUTH_CALLBACK_URL_HEADER`；
- 流程：`GET /api/hub/auth/oidc/login?next=...` → IdP 授权码 → 回调换 token → 校验 →
  映射/建号（JIT，`groups.source='oidc'`）→ 签发 hub 会话（复用 `create_token`）；
- 配置：hub settings 增 OIDC issuer/client_id/secret/claims 映射（groups claim → 本地组）；
- 降级：OIDC 不可用回退本地账密（现有 `/api/auth/login` 保留）。

## 4. 资源粒度（D2/D3）的实施边界

- Agent/Skill 级控制在 **Phase 2 只做"可见性"**（marketplace/pawapps 列表按策略过滤），
  执行级隔离依赖 per-tenant runtime 的天然边界（每租户本来就独立）；
- MCP/Channel 治理面 API 直接归入 admin 组（03 已覆盖）。

## 5. 验收

① 建 `finance` 组并授 `model:corp-gpt` → 组内用户模型目录仅此；
② OIDC 登录自动建号并入组；禁用 IdP 侧账号 → 下次登录拒绝；
③ 策略变更热生效（无需重启 hub，缓存 30s）；
④ 审计事件含 policy_id 命中路径。

## 6. 对表点

官方若落地组/RBAC（sqlite 结构大概率不同）→ 迁移脚本 + `policies` 表改挂官方主体模型；
自研求值逻辑薄（<300 行），替换成本低——**这是刻意保持的**。
