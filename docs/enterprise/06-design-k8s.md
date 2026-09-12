# 06 · 设计：K8s Provisioner 与 Helm（Phase 1）

> 对应需求：A2-A5、G1、G3、G6 ｜ 撞车风险 **高**（官方 K8s 方向在列，先行对表）
> 基线事实：`hub/provisioner.py` 定义六方法接口（configure / validate_config / preflight /
> start / stop / status），Local 与 Docker 双实现可参照；Docker 实现已演示"按租户注入 env +
> 边界令牌 + 资源限制"的全流程。

## 1. 模型选择：per-tenant Pod（已决策，见 01 §4）

```text
Namespace: qwenpaw-hub            Hub Deployment（1 副本，sqlite 用 PVC RWO）
Namespace: qwenpaw-runtimes      per-tenant Pod（1 Pod = 1 租户 runtime）
                                  + per-tenant PVC（RWO，workspace/记忆/会话 sqlite）
                                  + Service（cluster-local，hub 代理访问）
```

优势（相对共享化）：
- **RWO 即可**，不引入 RWX/NFS；每租户 sqlite/文件系统天然单写，无并发语义问题；
- cron/心跳单副本，**无跨副本去重问题**（A9/A10 顺延到共享化的 Phase 3）；
- 与上游 Hub 现有"每租户一 runtime"编排模型 1:1 对齐，`RuntimeService` 零改动。

## 2. K8sRuntimeProvisioner（新增 `src/qwenpaw/hub/provisioners/k8s/`）

```text
provisioners/k8s/
  __init__.py
  provisioner.py     # 实现 RuntimeProvisioner 六方法
  manifest.py        # Pod+PVC+Service 渲染（不引 helm 库，用原生 dict + k8s API）
  client.py          # 轻量 k8s client（负载 kubeconfig / in-cluster ServiceAccount）
  caps.py            # 后端能力声明（见 §4）
```

六方法映射：
- `preflight`：kubeconfig 可用、namespace 存在、可 create pods；不可用 → `RuntimeProvisionerUnavailableError`（上游 fail-closed 语义复用，G3）；
- `start(record)`：确保 PVC → 渲染 Pod（env：边界令牌、bootstrap JSON（04）、resource limits）→ 创建 → 等 `/api/healthz` 就绪（带 startup_timeout）→ 回填 cluster Service 地址到 `RuntimeRecord`（registry 已支持每实现自定义 endpoint 解析，参照 docker_provisioner）；
- `stop`：`DELETE pod`（默认保留 PVC；`rebuild` 语义走 label 差异触发重建）；
- `status`：读 Pod phase 映射 `RuntimeState`；
- `configure/validate_config`：namespace、镜像、默认 limits、nodeSelector/tolerations（GPU 亲和预留 A12）。

镜像：复用 `scripts/docker_build.sh` 产物（repo 根 Dockerfile）；内网无仓库时支持
`imagePullPolicy: IfNotPresent` + `pre-pull` 运维手册（不在代码内解决）。

## 3. Helm Chart（新增 `deploy/helm/qwenpaw-hub/`，零侵入）

```text
values.yaml            # hub 副本/镜像/存储、runtimes namespace、provisioner 选型
templates/
  hub-deployment.yaml  # env: QWENPAW_HUB_* + provisioner=k8s 配置
  hub-pvc.yaml         # sqlite + 审计数据
  hub-service.yaml     # 内网 LB/NodePort（不默认公网）
  rbac.yaml            # ServiceAccount: pods/pvcs/services 的 namespaced 权限（最小集）
  secrets.yaml         # 可选：externalSecret 引用（Vault 起步不建依赖，A4 中）
```

注意：Hub 单副本（sqlite + 内存态 registry 决定**不做多副本**；A6 HPA 仅在状态外置后讨论）。

## 4. 能力声明与协商（G2/G3，本阶段只做"声明+校验"，协商 Phase 2）

`caps.py` 输出（对齐 `sandbox/config.py:190` 的 `SandboxCapability` 风格）：

```python
RUNTIME_CAPS = RuntimeCaps(
    isolation="container",            # container | pod-ns | microvm（gVisor/Kata 后续）
    network="cluster-local",          # none | egress-only | cluster-local | full
    filesystem="tenant-pvc",
    gpu=False,
)
```

- `preflight` 校验声明与集群实际（如 runtimeClass 不存在即 fail-closed，**拒绝启动不降级**）；
- 硬拒绝/软降级：`isolation` 达不到任务要求 → 拒绝；`gpu=False` 而任务仅"偏好 GPU" → 软降级并记录审计（Phase 2 协商引擎的输入即此 schema）。

## 5. 升级与备份（衔接 A7/A8）

- 升级：hub 滚动（单副本重建，秒级中断窗口）；runtime 升级 = 换镜像 tag + 逐租户 `rebuild`（PVC 保留数据）；金丝雀 = 先 rebuild 1 个试点租户；
- 备份：Velero 对 runtimes namespace 做 PVC 快照 + hub sqlite 定期 `sqlite3 .backup` 到 PVC/对象存储（写成运维手册 `docs/enterprise/runbook-backup.md`，Phase 2 交付）。

## 6. 验收

① `qwenpaw hub` 以 provisioner=k8s 启动，注册用户后自动创建 per-tenant Pod + PVC + Service，登录即用；
② stop → Pod 删除、PVC 保留；rebuild → 数据延续（会话历史可查）；
③ kubeconfig 失效 → preflight 拒绝并给出修复提示（不半启动）；
④ `helm install` 内网集群一次成功，hub 经 Service 代理到 runtime（03 ACL 生效）。

## 7. 对表点

官方 Helm/K8s PR 落地 → 优先评估 chart 兼容与 provisioner 合流；自研 manifest 渲染层薄，
可快速对齐官方 CRD（若有）。
