# Runbook · K8s 内网安装（EP-1-8）

> 适用：企业内网 Kubernetes 集群（1.24+），Helm 3.8+。
> 对应设计：`06-design-k8s.md`。代码落点：`src/qwenpaw/hub/provisioners/k8s/`。

## 0. 前置条件

| 项 | 要求 |
|---|---|
| 集群 | 内网 K8s 1.24+，默认 StorageClass 可建 PVC（RWO） |
| 镜像 | 内网镜像仓库可达（Harbor 等）；或采用 pre-pull（§3） |
| 客户端 | `kubectl`（集群管理员）、`helm` 3.8+ |
| Hub 镜像 | `scripts/docker_build.sh` 产物推送至内网仓库 |

## 1. 镜像准备

```bash
# 构建并推送（hub 与 runtime 同一镜像，入口不同）
./scripts/docker_build.sh
docker tag qwenpaw:2.2.0 harbor.corp.local/qwenpaw/qwenpaw-hub:2.2.0
docker tag qwenpaw:2.2.0 harbor.corp.local/qwenpaw/qwenpaw-runtime:2.2.0
docker push harbor.corp.local/qwenpaw/qwenpaw-hub:2.2.0
docker push harbor.corp.local/qwenpaw/qwenpaw-runtime:2.2.0
```

无内网仓库时（**pre-pull 方式**，代码不感知）：在每个允许跑 runtime 的
节点上手工 `docker load < qwenpaw.tar`，chart 保持
`image.pullPolicy: IfNotPresent`（默认值）即可。

## 2. 安装

```bash
helm install qwenpaw deploy/helm/qwenpaw-hub \
  --namespace qwenpaw-hub --create-namespace \
  --set image.registry=harbor.corp.local/ \
  --set image.tag=2.2.0 \
  --set hub.adminPassword='<首次登录管理员密码>' \
  --set runtimes.namespace=qwenpaw-runtimes
```

chart 会创建：

- `qwenpaw-hub` Deployment（**单副本**，sqlite PVC RWO，`strategy: Recreate`）；
- `qwenpaw-hub-data` PVC（sqlite/审计/密钥）；
- `qwenpaw-hub` Service（默认 ClusterIP；内网 LB/NodePort 自行改 values）；
- `qwenpaw-runtimes` Namespace + 最小 RBAC（namespaced：pods/pvcs/services
  CRUD；cluster-scoped：仅 `namespaces get` 只读，用于 preflight）。

## 3. 首次验证

```bash
kubectl -n qwenpaw-hub rollout status deploy/qwenpaw-hub
kubectl -n qwenpaw-hub port-forward svc/qwenpaw-hub 8088:80
# 浏览器打开 http://127.0.0.1:8088 → 用 adminUsername/adminPassword 登录
```

功能验收（对应 06 §6）：

1. 注册普通用户 → 自动创建 per-tenant Pod + PVC + Service：
   `kubectl -n qwenpaw-runtimes get pods,pvc,svc -l app.kubernetes.io/managed-by=qwenpaw-hub`；
2. 停止 runtime → Pod 删除、**PVC 保留**；rebuild → 会话历史延续；
3. 断开集群凭据（或删除 ClusterRoleBinding）→ hub 日志出现
   `provisioner 'k8s' is unavailable`，**不半启动**，普通用户执行 k8s 运行
   时被 403/409 拒绝；
4. hub 代理 runtime：`QWENPAW_HUB_RUNTIME_HOST_SUFFIXES=svc,svc.cluster.local`
   （chart 已注入；手工部署时必须显式设置，否则 preflight fail-closed）。

## 4. 关键环境变量（chart 自动注入，手工部署对照）

| 变量 | chart 来源 | 语义 |
|---|---|---|
| `QWENPAW_HUB_K8S_NAMESPACE` | `runtimes.namespace` | runtime Pod/PVC/Service 所在 ns |
| `QWENPAW_HUB_K8S_IMAGE` | `image.registry`+`runtimes.image` | runtime 容器镜像 |
| `QWENPAW_HUB_K8S_PORT` | `runtimes.port` | runtime 容器端口（默认 8420） |
| `QWENPAW_HUB_K8S_CLUSTER_DOMAIN` | `runtimes.clusterDomain` | Service DNS 域 |
| `QWENPAW_HUB_K8S_PVC_SIZE` | `runtimes.storage.size` | 每租户 PVC 大小 |
| `QWENPAW_HUB_K8S_CPU/MEMORY_REQUEST/LIMIT` | `runtimes.resources` | 每租户 Pod 资源 |
| `QWENPAW_HUB_K8S_STARTUP_TIMEOUT` | `runtimes.startupTimeoutSeconds` | 就绪等待上限 |
| `QWENPAW_HUB_RUNTIME_HOST_SUFFIXES` | 固定 `svc,svc.<domain>` | 代理允许的 runtime 主机后缀（fail-closed） |

## 5. 升级 / 回滚

```bash
helm upgrade qwenpaw deploy/helm/qwenpaw-hub --set image.tag=2.2.1 ...
# runtime 升级：管理台逐租户 rebuild（换镜像 → Pod 重建，PVC 数据保留）
# 金丝雀：先对 1 个试点租户 rebuild，观察后全量
helm rollback qwenpaw
```

单副本 hub 升级 = Recreate 重建（秒级中断窗口）；hub PVC 与 runtime PVC
均不受升级影响。

## 6. 故障排查

| 症状 | 定位 |
|---|---|
| preflight: `set QWENPAW_HUB_RUNTIME_HOST_SUFFIXES` | 未配后缀白名单（手工部署漏 §4 最后一行） |
| preflight: `kubernetes API unusable: no kubeconfig` | in-cluster SA 凭据缺失 → 检查 RBAC/SA 挂载 |
| Pod Pending | PVC 未绑定（StorageClass）或资源不足 |
| 代理 503 `must be loopback-only` | 后缀白名单与实际 Service 域名不匹配 |
