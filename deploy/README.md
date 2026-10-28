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
- worker 执行 `fcoffer etl consume`，常驻消费 Redis Stream。健康检查不是对 Redis 做 `PING`——那只能证明网络通；消费循环每轮会把 `fc:etl:live:<stream>` 写到 Redis 并带 30 秒 TTL，探针读这个键并校验时间戳新鲜度。**循环卡住、线程崩溃或被 SIGSTOP 时，容器会在一个 TTL 后转 `unhealthy`**，而不是挂着绿灯装死。暂停（`etl pause`）时循环仍会刷新该键，暂停不会被误判为故障。

## 容器化实跑记录

本编排已在 Docker（Linux 内核，`docker compose` v2）上完整跑通过一轮，用空数据库从零验证：

| 验证点 | 结果 |
| --- | --- |
| `build` 构建应用镜像 | 成功（builder 内装 `.[api,db,etl,vector,s3,mcp]`，runtime 为非 root 用户） |
| `migrate` 在空库执行迁移 | 退出码 0，迁移一路到 head，`db status` 显示 `pending 0` |
| `postgres` / `redis` / `api` / `worker` 健康 | 四个容器均达到 `healthy` |
| `GET /health` | 200，`database: ok`；未接入 embedding 服务时为 `embedding: unreachable` |
| `GET /metrics` | 导出请求计数、仓库侧 ETL/归档计数与向量层计数；无样本的族整族省略（见「监控」） |
| `db partitions create --month <YYYY-MM>` | 建出当月 ODS 分区 |
| `ingest event`（CLI 同步落库） | ODS 与 DWD 同时写入，`source` 由 `source_info.hub` 推导 |
| 向 Stream 投递事件 → worker 消费 | `ops.etl_event_log` 记为 `done`，ODS 与 DWD 各新增一行 |
| 重复投递同一事件 | 判为 `duplicate` 跳过，不产生第二行 |
| 投递 `event_id` 不合法的事件 | 进入 DLQ，日志给出 `event_id` 校验原因 |
| `/v1/structured/<symbol>`（库中无宽表行） | 404 `E_NOT_FOUND`，符合预期 |
| `/v1/risk/<symbol>` | 200 |
| worker 存活探针 | 正常时 `healthy`；冻结消费进程后约两分钟转 `unhealthy`（存活键过期 + 连续 3 次探测失败），恢复后自动回到 `healthy` 并继续消费 |

验证时用了两处替代，均不影响编排本身的正确性，但记录在案：

- `minio/minio` 镜像在该网络下无法拉取（各镜像源 403 / not found），该轮验证把 `api`、`worker` 对 `minio` 的依赖去掉，只跑 postgres / redis / migrate / api / worker。**对象存储在容器内的行为未验证**，本地二进制方式见 `docs/decisions/DEC-0014-object-storage-minio-local-binary.md`。
- `pgvector/pgvector:pg16` 拉取过慢，改用本机已有的 `pg17` 镜像。迁移与向量列在 pg17 上行为一致，但正式部署仍以 `compose.yaml` 里固定的 `pg16` 为准。

尚未在容器内验证的两项：MinIO 读写、embedding 调用（宿主机 embedding 服务未在容器网络内应答，`/health` 因此显示 `embedding: unreachable`）。

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

指标名取自 `docs/specs/cli.md` §3.14 的清单。`/metrics` 实际导出三组，来源不同，看到同名指标要先看它来自哪一组：

| 指标 | 来源 | 说明 |
| --- | --- | --- |
| `fc_api_requests_total{method,path,status}`、`fc_api_request_seconds_total` | API 进程内中间件 | 只统计这一个 API 副本收到的请求 |
| `fc_etl_events_total{status}`、`fc_dirty_data_total{reason}`、`fc_archive_job_total{status}`、`fc_etl_consume_lag_seconds` | 每次抓取读 `ops.etl_event_log` / `ops.etl_dlq` / `ops.archive_job` | 跨进程、跨副本一致；库读不到时整族省略，不填 0 |
| `fc_missing_chunk_total{reason}`、`fc_vector_search_seconds*` | 向量层进程内计数 | 只反映**当前 API 进程**做出的切片与检索；切片实际发生在 `fcoffer vector ingest` 与 worker 里，所以标准编排下这两族通常没有样本 |

`fc_etl_consume_lag_seconds` 取的是 `received` / `processing` 里最早一条的 `received_at` 距今秒数，
也就是「最早那条排队事件等了多久」，不是「落后 stream 队头多少」，也不看 `event_time`——按业务时间算会把正常的历史补录误报成巨大延迟。仓库里只要有排队事件就一定有值，消费端把它清完才回落。
所以这个值持续偏高有两种可能——消费停了，或者有条事件被重置成排队态却没人再投递它。

下面两项指标在标准编排下**没有采集点**，对应告警恒为无数据：不会误报，但也不会报。

- `fc_db_pool_in_use`：API 的读路径（`QueryService`）用 psycopg 直连、请求结束即归还，没有连接池可采样；`pool_size=5 / max_overflow=10` 的上限只对持有 `Database` 的 worker 与 CLI 成立，而 worker 不暴露 HTTP 端口。要真正监控池水位，得让 worker 把 `engine.pool.checkedout()` 写到 Redis，再由 API 暴露。
- `fc_missing_chunk_total`：见上表，计数留在切片进程内。

`rules.yml` 里出现的每个指标名都必须在本节有说明，`tests/unit/test_deploy_assets.py` 会检查这一点——改规则时请同步改这张表。

## 常见故障

| 现象 | 排查 |
| --- | --- |
| 构建阶段报 `Temporary failure in name resolution` 或拉依赖卡住 | 构建容器没有可用 DNS：用 `docker build --network=host -f deploy/Dockerfile .`，或给 builder 配置可解析 PyPI 的 DNS。 |
| 端口被占用 | `docker compose ps` 或 `netstat -ano` / `ss -ltnp` 找到占用者；也可以改用 `API_PORT`（本地脚本）或调整 Compose 主机侧端口，主机侧继续绑定 `127.0.0.1`。 |
| 迁移失败 | 看 `migrate` 日志、`ops.schema_version` 记录和 `FC_DB_URL` 凭据；确认 PostgreSQL 健康、迁移目录完整且版本连续。 |
| MinIO 不可达 | 检查 `minio` 健康检查 `/minio/health/live`、`FC_MINIO_ACCESS_KEY`/`FC_MINIO_SECRET_KEY`、bucket 是否存在，以及容器内 `FC_MINIO_ENDPOINT` 是否为 `minio:9000`。 |
| embedding 不可达 | 检查 `FC_EMBEDDING_URL` 从容器内是否可达、embedding 服务是否在监听、维度是否为 1024；宿主机服务使用 `host.docker.internal`。 |
| API 启动即退出 | 读 API 日志与 `fcoffer config show` 输出，先排除缺失或非法的 `FC_*` 变量。 |
