# 备份与恢复手册（EP-2-5 / A8）

> 适用：Helm 部署的 QwenPaw Hub（`deploy/helm/qwenpaw-hub`）+ per-tenant
> runtime。目标 RPO ≤ 24h（日备）/ 可压缩到 1h（见 §5 调频）；
> RTO ≤ 30min（单 hub 副本 Recreate 重启路径）。

## 1. 有状态面盘点（备份什么）

| 面 | 位置 | 内容 | 丢失后果 |
|---|---|---|---|
| hub 主库 | hub PVC `/var/lib/qwenpaw/control.db` | 用户/凭据密文/审计/runtime 登记表（单 SQLite 库） | **致命**：全部账号与审计不可恢复 |
| vault 密钥 | hub PVC `/var/lib/qwenpaw/secrets/.vault_key` | Fernet 密钥 | **致命**：凭据密文永久不可解 |
| 治理 overlay | hub PVC `/var/lib/qwenpaw/`（`quota.json`、策略 overlay） | 配额/ACL 叠加层 | 可重建但有漂移风险 |
| runtime 数据 | runtime PVC `personal-*-working`（10Gi） | `sessions.db`/`knowledge.db`/agent 记忆 | per-tenant 会话与知识丢失 |

> kind 实测（2026-09-17，`qwenpaw-ep19`）：hub Pod label
> `app.kubernetes.io/name=qwenpaw-hub`；PVC 内容与上表吻合。

## 2. 层一：SQLite 在线备份（必做，独立于 Velero）

`deploy/scripts/backup-hub-sqlite.sh` 用 sqlite3 `.backup` 取**页级一致
快照**（WAL 下运行中调用安全，不会撕裂）；同时整份复制 `secrets/`，
产出 `SHA256SUMS` 清单并按时间戳轮转。

```bash
# K8s 内：从 hub Pod 直接备份到 PVC 内的 backup 目录（再由 Velero/宿主带走）
kubectl exec -n qwenpaw-hub deploy/qwenpaw-hub -- \
    /opt/qwenpaw/backup-hub-sqlite.sh /var/lib/qwenpaw \
    /var/lib/qwenpaw/backups 7

# 或本机/裸机部署：
deploy/scripts/backup-hub-sqlite.sh /var/lib/qwenpaw /backups/qwenpaw 7
```

定时（cron，每日 02:00 UTC）：

```cron
0 2 * * * deploy/scripts/backup-hub-sqlite.sh /var/lib/qwenpaw /backups/qwenpaw 7
```

### 恢复（SQLite 层）

```bash
SNAP=/backups/qwenpaw/20260917T021924Z
cd /var/lib/qwenpaw
# 关键纪律：恢复主库前先清掉 WAL/SHM 残留，避免旧预写日志与新主库
# 混放（SQLite 会做 salt 校验并忽略不匹配 WAL，但清掉才是可审计的确定性路径）
rm -f control.db control.db-wal control.db-shm
cp "$SNAP/control.db" .
mkdir -p secrets && cp "$SNAP/secrets/.vault_key" secrets/
sqlite3 control.db "PRAGMA integrity_check;"   # 必须输出 ok
sqlite3 control.db "SELECT count(*) FROM hub_users;"   # 与备份时对账
```

## 3. 层二：Velero（卷级）

CLI 缺失时的安装（macOS：`brew install velero`；Linux：release tarball）。
本地/kind 用 MinIO 做对象存储端点；生产替换为企业对象存储。

```bash
# 1) MinIO（kind 内或独立）
kubectl create namespace velero
kubectl -n velero run minio --image=minio/minio:RELEASE.2025-04-22T22-12-26Z \
    --restart=Always -- /bin/sh -c "minio server /data --console-address :9001"
kubectl -n velero expose pod minio --port=9000 --type=NodePort
kubectl -n velero create secret generic minio-creds \
    --from-literal=aws=QWENPAW-BACKUP-KEY --from-literal=secret=QWENPAW-BACKUP-SECRET

# 2) Velero server（node-agent 卷备份 = 文件级，不依赖存储类快照）
velero install \
    --provider aws --plugins velero/velero-plugin-for-aws:v1.11.0 \
    --bucket qwenpaw-backups --secret-file ./minio-creds \
    --use-node-agent --wait

# 3) 日备（只收 QwenPaw 面：hub + runtime 的 PVC）
velero schedule create qwenpaw-daily \
    --schedule="0 18 * * *" \
    --include-namespaces qwenpaw-hub,qwenpaw-runtimes \
    --snapshot-volumes=false
```

> `--snapshot-volumes=false` + node-agent：kind 的 `standard`
> (local-path) 无快照能力；生产若 storageclass 支持 CSI 快照可改
> `--snapshot-volumes=true` 并叠加 `defaultVolumesToFsBackup=false`。

### 恢复（Velero 层）

```bash
velero backup describe qwenpaw-daily-202609172200 --details
velero restore create --from-backup qwenpaw-daily-202609172200 \
    --include-namespaces qwenpaw-hub,qwenpaw-runtimes --wait
# 恢复后按 §2 做一次 integrity_check + 行数对账（Velero 是文件级复制，
# SQLite 一致性由层一的 .backup 快照保证——两层职责分离）
```

## 4. 恢复演练记录（2026-09-17，本机 L1 实测）

环境：macOS arm64 · sqlite3 3.43 · 真实初始化链路
（`initialize_hub_database` + `HubAuthService` + `HubOperationsStore`
+ `TenantCredentialVault`）。

| 步 | 操作 | 结果 |
|---|---|---|
| 1 | 播种：1 用户 + 1 审计事件 + `.vault_key`（WAL 开启） | `hub_users=1` `hub_audit_events=1` |
| 2 | `backup-hub-sqlite.sh hub backups 7` | `backed up 2 item(s)`；SHA256SUMS 含 `control.db` 与 `.vault_key` |
| 3 | 破坏：`rm control.db` 与 `secrets/` | 目录仅剩 WAL/SHM 残留 |
| 4 | 恢复（§2 纪律：清 WAL → 拷回 → 校验） | `integrity_check=ok`；行数对账 1/1；vault 密钥重载成功（`TenantCredentialVault` 构造通过） |
| 5 | 轮转：连打 8 次备份 keep=3 | 目录数恒为 3，旧快照自动清除 |

结论：**L1（SQLite+secrets）备份/恢复路径实测闭环**；RTO 实测 < 1min（单库）。
L2（Velero）步骤如上；本机 velero CLI 未装，部署时按 §3 首次执行后把
真实 `velero backup describe` 输出补记到本节。

## 5. 调频与保留策略

| 场景 | 频率 | 保留 | 手段 |
|---|---|---|---|
| 默认 | 日 02:00 | 7 份 | 层一脚本 cron |
| 高价值租户期 | 时 | 24 份 | 层一脚本 + `--include` 细化（分库粒度） |
| 卷级容灾 | 日 | 30 天 | Velero schedule |

密钥（`.vault_key`）额外离线一份（密码管理器/保险柜）；**密钥与密文同盘
备份只防误删不防盗**。

## 6. 验收（02 §6 对应）

- [x] sqlite 备份手册化 + 脚本化（§2，实测）
- [x] 恢复演练记录（§4，L1 闭环）
- [ ] 生产首次 Velero 全流程（§3，部署时执行后补记 §4）
