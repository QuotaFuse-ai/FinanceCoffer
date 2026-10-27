# 部署说明

本目录提供两套运行方式：

- **Compose 编排**：PostgreSQL + pgvector、Redis、MinIO、API、ETL worker，适合具备 Docker 的主机或服务器。
- **本地进程脚本**：`scripts/start-local.*` / `stop-local.*` / `status-local.*`，适用于没有 Docker、但四项依赖（PostgreSQL、Redis、MinIO、embedding）已经在跑的开发机。

## 依赖顺序

```
postgres / redis / minio  (健康检查通过)
            │
            ▼
        migrate           (一次性迁移，退出码 0)
            │
            ▼
       api / worker
```

- `postgres`、`redis`、`minio` 通过 `condition: service_healthy` 放行。
- `migrate` 通过 `condition: service_completed_successfully` 放行。
- 只有 `api` 发布主机端口，默认绑定 `127.0.0.1:8100`；其余服务仅在编排网络内可见。
- API 容器内监听 `0.0.0.0`，主机侧仍只绑定回环地址。对外暴露前请在前面加带认证的反向代理。

## 环境变量

变量名与 `src/financecoffer/core/config.py` 一致，模板见 `.env.example`，全部为 `CHANGE_ME` 占位符：

- Compose 专用：`POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB`
- 数据库与事件：`FC_DB_URL`、`FC_REDIS_URL`、`FC_EVENT_CONSUMER`、`FC_EVENT_STREAM`、`FC_EVENT_GROUP`
- 向量检索：`FC_EMBEDDING_PROVIDER`、`FC_EMBEDDING_MODEL`、`FC_EMBEDDING_DIM`、`FC_EMBEDDING_URL`、`FC_EMBEDDING_API_KEY`
- 对象存储：`FC_MINIO_ENDPOINT`、`FC_MINIO_ACCESS_KEY`、`FC_MINIO_SECRET_KEY`、`FC_MINIO_BUCKET`
- 运行开关：`FC_COMPLIANCE_ENABLED`、`FC_RATE_LIMIT_ENABLED`、`FC_RATE_LIMIT_PER_MINUTE`、`FC_LOG_LEVEL`、`FC_COLD_AFTER_YEARS`
- 令牌：`FC_MCP_TOKEN`

约定：

- 真实 `.env` 只留在本机，已被 `.gitignore` 忽略；不要提交、不要贴进日志。
- Compose 文件中没有任何字面量密钥，全部通过 `.env` 插值；缺失变量会让 `docker compose` 直接报错。
- `fcoffer config show` 的脱敏行为不受部署脚本影响。

## 首次启动

```sh
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env，替换所有 CHANGE_ME

docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
curl -fsS http://127.0.0.1:8100/health
```

`up -d` 会按上面的顺序自动等待依赖；也可以分步执行：

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d postgres redis minio
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm migrate
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d api worker
```

说明：

- `FC_EMBEDDING_URL` 不指向 Compose 服务，必须是从容器内可达的地址。API/worker 已加入 `host.docker.internal` 映射，Docker Desktop 与 Linux（Docker 20.10+）都可访问宿主机上的 embedding 服务。
- MinIO 官方镜像在部分网络不可达，`compose.yaml` 头部注释给出了替代方式；本地二进制部署见 `docs/decisions/DEC-0014-object-storage-minio-local-binary.md`。
- worker 执行 `fcoffer etl consume`；ETL 消费端尚未实现前，容器会退出，这是预期行为，待消费端上线后随编排一起拉起。

## 升级与回滚

升级：

1. 先备份数据库（`pg_dump`），确认迁移只向前。
2. 重新构建镜像并打新 tag，更新 Compose 中的镜像 tag。
3. `docker compose --env-file deploy/.env -f deploy/compose.yaml up -d`。
4. 观察 `docker compose ps`、`/health`、worker 日志和 Prometheus 告警。

回滚：

1. 把镜像 tag 改回上一个版本，再次 `up -d`。
2. 迁移是单向的（见 `docs/decisions/DEC-0008-forward-only-migrations.md`），代码可回滚，数据库不自动降级；若新迁移与旧代码不兼容，用升级前的备份恢复。
3. 回滚后重新检查 `/health` 与 ETL 消费延迟。

## 本地进程脚本（无 Docker）

脚本只管理自己启动的 API 进程：

- `start-local`：先探测端口；已有健康服务则直接复用，否则用 `PYTHON` 指定的解释器拉起 API。
- `status-local`：显示 PID、端口、健康状态和日志位置。
- `stop-local`：只停止运行目录中记录、且命令行仍属于 API 的 PID；其他进程不受影响。

前置条件：

- 设置 `PYTHON` 为 Python 3.11+ 解释器；机器相关路径放在 `scripts/local-env.local.cmd` / `local-env.local.sh`（已被忽略）。
- 仓库根目录 `.env` 提供 `FC_DB_URL` 等真实配置；脚本启动时加载，不覆盖已存在的环境变量。
- 可选 `API_PORT`（默认 8100）和 `FC_STATE_DIR`（运行目录与日志根目录）。

运行目录与日志不在仓库内：

- Windows：`%LOCALAPPDATA%\FinanceCoffer\{run,logs}`
- POSIX：`${XDG_STATE_HOME:-~/.local/state}/financecoffer/{run,logs}`

Windows 用法：

```bat
scripts\start-local.cmd
scripts\status-local.cmd
scripts\stop-local.cmd
```

POSIX 用法：

```sh
scripts/start-local.sh
scripts/status-local.sh
scripts/stop-local.sh
```

## 监控

`prometheus/prometheus.yml` 抓取 `api:8100` 的 `/metrics`；`prometheus/rules.yml` 覆盖以下告警：

- ETL 消费延迟超过 60 秒
- ETL 失败事件持续增长
- 缺失分块数大于 0
- 归档任务失败持续增长
- 数据库连接池接近上限（上限 15，超过 12 告警）

Prometheus 需要与 API 在同一编排网络内，或把 target 改成实际可达地址。规则文件可用 `promtool check rules` 校验。

指标名取自 `docs/specs/cli.md` §3.14 的清单。API 目前实际导出 `fc_api_requests_total` 和
`fc_api_request_seconds_total`；ETL 延迟、失败计数、缺失分块、归档任务和连接池这五类指标的
采集点仍待实现，在此之前对应告警处于无数据状态，不会误报。

## 常见故障

| 现象 | 排查 |
| --- | --- |
| 端口被占用 | `docker compose ps` 或 `netstat -ano` / `ss -ltnp` 找到占用者；也可以改用 `API_PORT`（本地脚本）或调整 Compose 主机侧端口，主机侧继续绑定 `127.0.0.1`。 |
| 迁移失败 | 看 `migrate` 日志、`ops.schema_version` 记录和 `FC_DB_URL` 凭据；确认 PostgreSQL 健康、迁移目录完整且版本连续。 |
| MinIO 不可达 | 检查 `minio` 健康检查 `/minio/health/live`、`FC_MINIO_ACCESS_KEY`/`FC_MINIO_SECRET_KEY`、bucket 是否存在，以及容器内 `FC_MINIO_ENDPOINT` 是否为 `minio:9000`。 |
| embedding 不可达 | 检查 `FC_EMBEDDING_URL` 从容器内是否可达、embedding 服务是否在监听、维度是否为 1024；宿主机服务使用 `host.docker.internal`。 |
| API 启动即退出 | 读 API 日志与 `fcoffer config show` 输出，先排除缺失或非法的 `FC_*` 变量。 |
