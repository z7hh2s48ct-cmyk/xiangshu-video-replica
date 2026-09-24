# 客户版唯一默认部署包（CW-032）

本目录是空白环境可重建的客户后端唯一默认交付包：`compose.yaml` +
`bootstrap-base-image.sh` + `healthcheck.py` + 本手册。除本目录与 `deploy/postgres/`、
`deploy/nginx/customer.conf.example`、`deploy/customer.env.example` 之外，
部署不再依赖任何未登记的主机文件（旧 `/opt/video-replica-candidate/compose.yaml`
由 `customer-git-rollout.sh` 的 `CUSTOMER_COMPOSE` 覆盖通道兼容，但不再是必需品）。

正式部署产物排除项（PG-08/PG-09）：internal P0 systemd 单元
（`deploy/systemd/video-replica-api*.service` 等内部单元）与 SQLite backup
timer（`video-replica-backup.*`）不属于本包；历史 operator 工具由 CW-060 的
独立制品交付（`deploy/operator/`），同样不在客户镜像内（构建期实物扫描强制）。

## 0. 前置

- Docker Engine + Compose v2；两份受控配置文件（不入 Git）：
  - `/etc/video-replica/compose.env`：`APP_IMAGE`、`POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB`
  - `/etc/video-replica/customer.env`：应用配置（模板 `deploy/customer.env.example`；
    `VIDEO_REPLICA_DATABASE_URL` 默认指向本 compose 网络的 `db:5432`）
- nginx 反代（模板 `deploy/nginx/customer.conf.example`）：upstream 即本包 api-1/api-2
  的 `127.0.0.1:8001` / `127.0.0.1:8002`。

## 1. 空白环境重建（API/Worker/Admin）

```bash
# 1) 首个应用镜像（干净基底 + 锁定依赖 + 与 rollout 相同的镜像内检查）
deploy/customer/bootstrap-base-image.sh <git-short-sha>

# 2) 写 /etc/video-replica/compose.env（APP_IMAGE=video-replica-rehearsal-app:<sha> …）
# 3) 先只运行迁移；接着按 §2 provision-empty-customer 导入私有 COS/管理员
docker compose --env-file /etc/video-replica/compose.env -f deploy/customer/compose.yaml run --rm migrate
# 4) 完成 §2 后拉起 API/Worker（生产启动门要求 COS 已配置）
docker compose --env-file /etc/video-replica/compose.env -f deploy/customer/compose.yaml up -d
```

`migrate` 是唯一执行 schema DDL 的角色（一次性容器）；api/worker 通过
`depends_on: service_completed_successfully` 等待其完成后才启动，自身不运行
alembic——多实例不会竞争改 schema。后续发版沿用 `deploy/customer-git-rollout.sh`
（灰度、健康检查、回滚均按既有脚本执行，其迁移步骤同样是单次执行）。

Admin 页面（`client/dist-admin`）由 `customer-git-rollout.sh` 的 SITE 步骤发布到
nginx 的 `/admin/`；空白环境首装时先按手册构建前端制品后再执行该脚本。

## 2. 首次管理员（first admin）

迁移完成后、对空库执行一次性开通（命令与实现一致，重复执行会被
pristine 检查拒绝）：

```bash
docker compose --env-file /etc/video-replica/compose.env -f deploy/customer/compose.yaml run --rm --no-deps \
  --volume /etc/video-replica/cos-bootstrap.json:/etc/video-replica/cos-bootstrap.json:ro api-1 \
  sh -lc 'cd /opt/video-replica/server && python -m app.bootstrap \
  provision-empty-customer --admin-username <ops-admin> \
  --admin-display-name "<显示名>" --cos-config-file /etc/video-replica/cos-bootstrap.json \
  --confirm-empty-database'
```

输出的单次 exchange credential 按运维手册写入 root-only 文件，不进终端回滚历史。

## 3. 连接池预算（全进程）

| 进程 | 数量 | 每进程池上限 |
| --- | --- | --- |
| api-1/api-2 | 2 | `VIDEO_REPLICA_PG_POOL_MAX`，默认 8（硬顶 64，`server/app/db_pg.py`） |
| worker-1..4 | 4 | 同上 |
| worker-viral | 1 | 独立每周关键词采集及云归档，`--viral-collection --idle-seconds 30` |
| worker-publish | 1 | 平台发布投递（协议优先、浏览器兜底）与账号登录态探测，`app.publish_worker --idle-seconds 3`；抖音签名依赖基础镜像内 Node.js |
| migrate | 一次性 | 短连接 |
| 合计 | — | 8×8=64 + 1 + 3（superuser_reserved）= 68 ≤ `max_connections=120`（compose db command） |

上调任一 `VIDEO_REPLICA_PG_POOL_MAX` 前必须复核上式（契约测试：
`server/tests/test_cw032_delivery_package.py`）。

### 3.1 并发度与取小关系

全平台的实际并发由三者取最小，**只调其中一个通常看不到效果**：

```
真实并发 = min( h3_provider_accounts 的 Σconcurrency_limit ,
               worker 实例数 × VIDEO_REPLICA_WORKER_CONCURRENCY ,
               PG 连接余量 ÷ 每个 worker 的连接占用 )
```

- 账号池额度在管理端「视频生成 · 多账号」配置（表 `h3_provider_accounts`）。
- `VIDEO_REPLICA_WORKER_CONCURRENCY` 是**单进程内**并行推进的任务数，默认 1
  （历史串行行为）。4 个 worker 实例 × 并发 4 = 16 路并行。
- 该值**不得超过 `VIDEO_REPLICA_PG_POOL_MAX`**，否则线程在连接池上排队。
  提高并发时请同步上调池上限，并重新复核 §3 的总连接预算。

## 4. 备份与恢复（backup 角色）

- 物理备份：宿主机 `deploy/postgres/pitr-backup.sh`（pg_basebackup + WAL，
  `video-replica-pitr-backup.timer`），归档加密/跨区/不可变由
  `VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER` 平台承担——轮换即平台保留策略，
  本机不删除已发布 base（与实际命令一致）。
- 恢复演练：`deploy/postgres/pitr-restore-drill.sh` + `scripts/pitr_recovery_facts.py`。
- SQLite backup timer 不属于本包（PG-08：正式部署中 SQLite backup=0）。

## 5. 健康检查与发布验证

- 容器级：db `pg_isready`；api `GET /ready`（容器内 `python /opt/video-replica/customer-healthcheck.py`，保留 Host/HTTPS/单客户端转发头）。
- 发布级：`customer-git-rollout.sh` VERIFY——镜像 revision 标签逐容器比对、
  前端/admin 资产指纹、`/health?release=<sha>`、OpenAPI 路径存在性。
- 配置 fail-fast（每项独立、可定位）：缺/错 PG DSN、缺 settings/admin/激活码等
  Fernet/HMAC 根密钥、缺私有 COS、缺 ffmpeg/ffprobe —— 均在启动或就绪检查中
  拒绝启动（可执行验收见 `server/tests/test_cw032_delivery_package.py` 的矩阵，
  其 executable 落点为既有 bootstrap/安全门测试套件）。

## 6. 制品哈希

- 镜像标签：`org.opencontainers.image.revision`（release SHA）、
  `org.opencontainers.image.source-tree`、`video-replica.database-head`。
- 发布备份：`$ROOT/backups/<...>/BACKUP-SHA256SUMS`（站点包 + pg_dump）；
  rollout 从本目录取 compose 后会把 `compose.sha256` 一并写入。
- 锁定依赖：`server/pyproject.toml` + `server/uv.lock`（镜像内 `uv sync --locked`）。

## 7. 部署文件、TLS 与受控升级（上线评审 R02—R06）

所有服务从 `/etc/video-replica/customer.env` 读取环境变量；Compose 插值使用独立的
`/etc/video-replica/compose.env`（chmod 0600，不入 Git）。`env_file` 不会挂载文件：
默认包已将 CA 与 metrics token 以只读文件挂载到应用相同绝对路径，源文件缺失即失败。
COS 导入文件只通过 §2 的一次性 `--volume` 挂载，不留在常驻服务内。
自定义宿主路径可用 `CUSTOMER_ENV_FILE`、`POSTGRES_CA_FILE`、`METRICS_TOKEN_FILE`；
容器目标路径固定，应用配置仍指向 `/etc/video-replica/`。以上变量写入 compose.env。

本包是单宿主拓扑，不能替代生产 PG HA 验收。内置 PG 强制 TLS：

- 管理员配置 `/etc/video-replica/postgres-tls/server.crt` 和 `server.key`，
  证书 SAN 必须含 `DNS:db`；CA 放 `/etc/video-replica/postgresql-ca.pem`。
- `postgres:16-alpine` 使用 postgres UID/GID 70；私钥设 root:70、0640，
  证书 0644，目录 0750、root:70。密钥不入镜像/Git。
- 应用 DSN 使用 `db:5432`、`sslmode=verify-full` 和上述 CA；默认 hba 拒绝非 TLS TCP。
  应用账号按数据库规范使用专用最小权限身份，`POSTGRES_USER` 是数据库引导管理员。
- 默认 bridge 为 `172.30.42.0/24`、gateway `172.30.42.1`；如与宿主冲突，
  在 compose.env 改 `CUSTOMER_NETWORK_SUBNET`/`CUSTOMER_NETWORK_GATEWAY`，同时把
  customer.env 的可信代理改为该 gateway 的精确 `/32`，保留 loopback 供容器探针。
  不信任整个容器子网。Host Nginx 覆盖转发头，API 使用 `--no-proxy-headers`。
  Linux 宿主首次安装必须核验实际原始对端；Docker Desktop 仅作测试，不作为此宿主拓扑。

升级脚本默认从**脚本所在的已安装交付包**读取 compose，与待 clone 的新 SHA 分开；
`CUSTOMER_COMPOSE` 可指定另一个经审核拓扑，`CUSTOMER_COMPOSE_ENV` 可指定配置文件。
先安装/审核本包及其 `../postgres/customer-pg_hba.conf`，再运行升级脚本；源码构建更新
不会隐式更改运行拓扑。拓扑/网段/TLS 变更先在维护窗口按新包完成，不由应用滚动升级代办。

每次升级写 `/opt/video-replica-candidate/app-image.override.json`，覆盖 API、所有已配置
Worker 和 migrate 的镜像，不编辑 compose 原文或密钥文件。升级后人工 Compose 操作必须：

```bash
docker compose --env-file /etc/video-replica/compose.env \
  -f deploy/customer/compose.yaml \
  -f /opt/video-replica-candidate/app-image.override.json ps
```

失败时恢复原覆盖文件（原本不存在则删除），恢复站点、启动并检查原有服务；新引入的
可选 Worker 保持停止。回滚未就绪标记 `FAILED_ROLLBACK_INCOMPLETE`，不会冒报恢复成功。
数据库只前向兼容回滚，保留 pg_dump 证据。首次基础镜像与后续镜像都记录完整 Git SHA、
源码树和唯一 migration head，镜像构建内核验 head；需从已提交、干净 server 目录构建。
应用修复不等同部署批准，真实 HA/COS/反代及故障回滚仍需 staging 验收。

探针脚本来自已安装交付包，以只读 bind 挂载到镜像外固定路径；回滚到没有新探针模块的旧应用镜像时也能执行，无需为探针预先升级应用。

旧镜像若没有 Node，升级镜像构建会明确失败：先用本包 bootstrap 构建含 Node 的新基底，再通过 `VIDEO_REPLICA_BUILD_BASE_IMAGE` 指定它；不在运行容器里临时安装依赖。

PITR 的物理复制连接有独立 TLS-only HBA 规则，备份账号仍须按数据库规范授予 REPLICATION 权限；`all` 数据库规则不能替代 replication 条目。

## 8. 日志轮转与留存

应用日志只写 stderr（`server/app/logging_setup.py` 不装文件 handler），容器内由
docker json-file 驱动收集——它是唯一落盘路径。本包在 `compose.yaml` 顶部以
`x-logging: &default-logging` 锚点为**全部 10 个服务**（db/migrate/api/worker 全量）
统一约束轮转：

- `max-size: "10m"`、`max-file: "5"`：每容器最多 5 份 × 10 MB（约 50 MB），
  超限滚动丢弃最旧一份；全栈上限约 500 MB。
- 当前与轮转文件位于宿主 `/var/lib/docker/containers/<id>/<id>-json.log*`。
- 参数是契约：调整后 `server/tests/test_cw032_delivery_package.py` 的轮转断言会失败，
  必须同步本节与测试。

查看与排障（`--env-file` 同 §1）：

```bash
# 最近 30 分钟的 API 日志（服务名任选 api-1/api-2/worker-*/db）
docker compose --env-file /etc/video-replica/compose.env \
  -f deploy/customer/compose.yaml logs --since 30m api-1

# 持续跟踪发布 worker
docker compose --env-file /etc/video-replica/compose.env \
  -f deploy/customer/compose.yaml logs -f worker-publish
```

应用日志行格式 `%(asctime)s %(levelname)s %(name)s %(message)s`；请求与任务日志带
`request_id`/任务编号，可直接 `grep` 过滤。管理端「生成记录 → 任务诊断」按任务编号/
问题编号查重试历史与上游诊断（P1-8），是不依赖日志文件的直接入口。

留存边界：json-file 轮转只保留最近约 50 MB/容器，**不承担长期审计留存**；需要长期
留存或集中检索时由宿主侧采集承担（journald 转发、对象存储归档或外部日志平台），本包不内置。
