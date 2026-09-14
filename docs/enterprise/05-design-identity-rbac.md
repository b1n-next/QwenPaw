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

## 7. 实现状态（2026-09-14 追溯审计补记）

本设计主体（groups/policies/OIDC）为 Phase 2 范围，0% 实施；Phase 0/1
已落地的是其依赖的**本地身份底座**，为避免与 02 矩阵 C/D 组状态失联，
记录如下：

| 能力 | 状态 | 落点 |
|---|---|---|
| 本地账号 + 双角色（admin/user） | ✅ Ph0 | `hub/auth.py` `HubAuthService`（PBKDF2 600k 迭代、HMAC 版本化 token、末位 admin 保护、禁自改） |
| 首注册即管理员 | ✅ Ph0 | `auth.py` register()：user_count()==0 → role=admin；此后受 registration_enabled 开关控制 |
| 容器化首管理员 bootstrap | ✅ Ph1 | `hub/bootstrap_admin.py`（幂等；helm initContainer 调用，见 06 §7） |
| ACL 引擎 role 模型（D4 最小半边） | ✅ Ph0 | `hub/acl/engine.py`：admin 直通（role-admin），user 走有序规则 fail-closed + acl.json overlay 热载 |
| groups / policies 求值 / OIDC / 组映射 | ☐ Ph2 | EP-2-1（groups+policies 并入 AclEngine）/ EP-2-2（OIDC 授权码+JIT+组映射）均未开始 |

> D4 口径拆分（02 矩阵同步注记）："策略引擎最小实现"的**静态规则半边**
> 已随 Phase 0 落地（rules.py 即 subject→resource→effect）；**组级扩展**
> （groups/policies 表 + 求值合并）仍属 Phase 2。
