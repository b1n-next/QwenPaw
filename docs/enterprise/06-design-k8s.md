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
  hub-deployment.yaml  # env: QWENPAW_HUB_* + provisioner=k8s 配置；initContainer bootstrap_admin；readinessProbe 探 `/`（healthz 需登录，见 §7.1）
  hub-config.yaml      # ConfigMap: hub.yaml（version: 1 + public_base_url + provisioner）
  hub-pvc.yaml         # sqlite + 审计数据
  hub-service.yaml     # 内网 LB/NodePort（不默认公网）
  rbac.yaml            # SA + Role（runtimes ns 内 pods/pvcs/services 最小集）+ ClusterRole（namespaces get，探测用只读）
  NOTES.txt            # helm install 后的访问/下一步提示
  # secrets.yaml 未实现：凭据走 bootstrap env 直投（A4 K8s Secret 投递已归置 Phase 2，见 02 矩阵注记）
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

## 7. 实现状态（2026-09-13，EP-1-6..1-9）

| 节 | 状态 | 落点 |
|---|---|---|
| §2 K8sRuntimeProvisioner | ✅ | `src/qwenpaw/hub/provisioners/k8s/`（client/manifest/provisioner/caps），六方法全实现，注册进 hub 工厂，`QWENPAW_HUB_K8S_*` 环境变量配置 |
| §2 代理放行 | ✅ | `require_loopback_runtime` 接受 `QWENPAW_HUB_RUNTIME_HOST_SUFFIXES` 后缀白名单（仅 provisioner=k8s，fail-closed） |
| §3 Helm Chart | ✅ | `deploy/helm/qwenpaw-hub/`（Deployment+PVC+Service+RBAC+ConfigMap+initContainer bootstrap） |
| §4 能力声明 | ✅ | `caps.py` `RuntimeCaps`（声明+校验；协商 Phase 2） |
| §6 验收 | ✅ | ①-④ kind 实测全过（见 §7.1 演练记录） |

**实现备注**：

- 客户端为零依赖自研（httpx + kubeconfig/in-cluster 自动探测），未引
  `kubernetes` 官方 SDK——hub 镜像不用加重型依赖，MockTransport 全覆盖单测；
- `RuntimeRecord` 是 frozen dataclass，状态回填走 `dataclasses.replace`
  （与 docker provisioner 一致）；
- 首管理员：公网绑定（0.0.0.0）要求已有 enabled admin，chart 用
  initContainer 跑 `python -m qwenpaw.hub.bootstrap_admin`（幂等）解决鸡生蛋；
- `hub.provisioner` schema Literal 扩为 `local|docker|k8s`（config.py 单行）。

安装/升级/排障手册：`docs/enterprise/runbook-k8s-install.md`。

### 7.1 EP-1-9 kind 实测记录（2026-09-13）

环境：kind v0.33.0（K8s v1.37 单节点）+ 自建验证镜像（`deploy/Dockerfile.ep19`，
控制台 dist 直拷 + 阿里 apt/pypi 镜像；生产构建仍走 `scripts/docker_build.sh`）。

| 验收项 | 结果 | 证据 |
|---|---|---|
| ① helm install 一次成功、注册即用 | ✅ | initContainer `bootstrap_admin` 幂等建 owner（重启后 `users-exists:1`）；注册 kinduser → 登录 200 |
| ② per-tenant Pod+PVC+Service | ✅ | 首次 `/api/chats` 代理触发惰性创建：Pod 1/1 Running、PVC Bound 10Gi RWO（standard）、Service ClusterIP 8088 |
| ③ stop→Pod 删 PVC 留；重启数据延续 | ✅ | stop 后 `No resources found`（Pod 删）+ PVC 仍 Bound；start 重建 Pod 后**会话 `ep19-proof-session` 跨停启仍存在**（PVC 数据延续铁证） |
| ④ 断配置 fail-closed | ✅ | 删除 `QWENPAW_HUB_RUNTIME_HOST_SUFFIXES` 后 start 立即拒绝（`Managed runtime host must be loopback-only`），不半启动 |

实测中修掉的三处真问题（都已进单测/镜像）：

1. **事件循环绑定**：`httpx.AsyncClient` 绑定创建时的 loop，provisioner 同步方法内
   `asyncio.run` 复用跨 loop client → `Event loop is closed`。改为**每次操作一次性
   client 工厂**（client 与私有 loop 同生命周期）；
2. **就绪探针 401**：managed runtime boundary 对匿名探针全 401，Pod 永不 Ready。
   readinessProbe 改 `exec` + `curl -H "X-QwenPaw-Runtime-Token: $..._INTERNAL_TOKEN"`；
3. **Service 层第二道 loopback 守卫**：`service.py:_start_locked` 对 stop 后重 start
   的 DNS host 拒绝。抽公共谓词 `utils/http.py:runtime_host_allowed`（loopback 恒过；
   k8s + suffix 白名单过；否则拒），control_app 与 service 统一走它。

另：hub 容器 readiness 探针不能用 `/api/hub/healthz`（需登录）→ chart 用 `/`。

## 8. 对表点

官方 Helm/K8s PR 落地 → 优先评估 chart 兼容与 provisioner 合流；自研 manifest 渲染层薄，
可快速对齐官方 CRD（若有）。
