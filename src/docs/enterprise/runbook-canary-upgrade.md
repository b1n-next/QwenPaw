# 金丝雀升级与环境分层手册（EP-2-10 / A7 / I2）

> 适用：Helm 部署的 QwenPaw Hub。约束前提：hub 是 **sqlite 单写者**
> （06 §3），经典双活金丝雀不可用——本手册的形态是
> **"隔离状态金丝雀 + 冒烟门 + 服务选择器切流"**：新版本以
> emptyDir 草稿态起副本，冒烟通过后一瞬切流，回退 = 一次 patch。

## 1. 环境分层（values 分档）

| 档 | 文件 | 形态 |
|---|---|---|
| dev | `values-dev.yaml` | NodePort · 1Gi/2Gi 存储 · 低资源 · 注册开 · recreate 直升 |
| staging | `values-staging.yaml` | 生产同形 · 日备 · 冒烟门 canary |
| prod | `values-prod.yaml` | 20Gi/50Gi · 高资源 · 18:00 日备 · canary + 三冒烟门 |

```bash
helm upgrade --install qwenpaw deploy/helm/qwenpaw-hub -n qwenpaw-hub \
    -f deploy/helm/qwenpaw-hub/values-prod.yaml \
    --set hub.adminPassword='...' --set image.tag="$NEW_TAG"
```

`backup.*` 与 `upgrade.*` 是**部署策略档案**（GitOps 层与本手册消费；
chart 模板不渲染这两个 key——分档内其余 key 均为模板真实消费项）。

## 2. 金丝雀流程（五步）

### 步 1：从现役 spec 派生金丝雀

```bash
kubectl -n qwenpaw-hub get deploy qwenpaw-hub -o yaml > /tmp/canary.yaml
# 三处修改（脚本化时用 yaml 处理器，勿手编）：
#   1) 名字/labels → qwenpaw-hub-canary（deployment 名、selector、pod labels）
#   2) hub-data 卷 → emptyDir{}（金丝雀绝不共 PVC：sqlite 单写者）
#   3) initContainer --password → 一次性冒烟口令
kubectl apply -f /tmp/canary.yaml
kubectl -n qwenpaw-hub rollout status deploy/qwenpaw-hub-canary --timeout=180s
```

### 步 2：冒烟门（gate）

```bash
kubectl -n qwenpaw-hub port-forward deploy/qwenpaw-hub-canary 18089:8088 &
deploy/scripts/hub-smoke.sh http://127.0.0.1:18089 owner '<冒烟口令>'
# SMOKE-PASS → 晋级；SMOKE-FAIL → kubectl delete deploy qwenpaw-hub-canary 收队
```

`hub-smoke.sh` 五关：无鉴权 401/403 · 登录换 token · 鉴权 healthz 200 ·
permissions 契约 200 · runtime registry 200（控制面 DB 可达）。

### 步 3：切流（服务选择器）

> ⚠️ **必须用 JSON patch replace，不能用 merge patch**：selector 是
> map，merge patch 只增改**不删 key**——演练实测踩坑：先设
> `track: canary` 再想"改回原 selector"时 track 残留，endpoints 归零。

```bash
kubectl -n qwenpaw-hub patch svc qwenpaw-hub --type=json \
  -p '[{"op":"replace","path":"/spec/selector",
        "value":{"app.kubernetes.io/name":"qwenpaw-hub-canary","track":"canary"}}]'
```

### 步 4：线上验证 + 固化

```bash
deploy/scripts/hub-smoke.sh http://<service-url> owner '<生产口令>'
# 通过 → helm upgrade 固化新 tag（stable deployment 重建到新版本）
helm upgrade qwenpaw deploy/helm/qwenpaw-hub -n qwenpaw-hub \
    -f values-prod.yaml --set image.tag="$NEW_TAG" ...
```

### 步 5：回退 / 清理

```bash
# 仅回退流量（秒级）：
kubectl -n qwenpaw-hub patch svc qwenpaw-hub --type=json \
  -p '[{"op":"replace","path":"/spec/selector",
        "value":{"app.kubernetes.io/name":"qwenpaw-hub"}}]'
# 清理金丝雀：
kubectl -n qwenpaw-hub delete deploy qwenpaw-hub-canary
```

runtime 升级无需金丝雀：hub 按 registry 期望态重建 per-tenant
runtime Pod（无状态 + PVC 工作区），漂移自愈。

## 3. 演练记录（2026-09-17，kind `qwenpaw-ep19` 实测）

| 步 | 操作 | 结果 |
|---|---|---|
| 0 | 三档 values `helm template` 渲染（dev/staging/prod ×14 资源） | ✅ |
| 1 | stable 冒烟（port-forward + hub-smoke.sh，5 关） | `SMOKE-PASS` |
| 2 | 派生 canary（emptyDir + 一次性口令 + configmap 复用）rollout | ✅ Ready |
| 3 | canary 冒烟（隔离库自建 admin） | `SMOKE-PASS` |
| 4 | merge patch 切流 `track: canary` → service 探测 | 401（鉴权面活）→ 冒烟 `SMOKE-PASS` |
| 5 | **踩坑**：merge patch 回切 → track 残留 → endpoints `<none>` | 学习点固化到 §2 警示框 |
| 6 | JSON patch replace 回切 → endpoints 恢复 `10.244.0.5:8088` | stable 冒烟 `SMOKE-PASS` |
| 7 | 清理 canary deployment | 无残留 |

结论：金丝雀闭环（派生 → 隔离冒烟 → 切流 → 验证 → 回切）**实测可
执行**，回退路径秒级。RTO（回切）实测 < 10s。

## 4. 验收（02 §6 对应）

- [x] 环境分层 values 三档 + 渲染验证（I2）
- [x] 金丝雀五步流程手册 + kind 全链路演练记录（A7）
- [x] 冒烟门脚本化（5 关，双环境实测 SMOKE-PASS）
