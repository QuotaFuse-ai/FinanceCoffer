# 部署说明

> **状态声明（2026-10-29）**：本目录的编排**只在本地 Docker 上做过静态解析和有限的人工实跑**，
> 从未部署到真实服务器，也从未在生产环境运行过。文中所有「验证」「实跑」都指本地那几次人工操作，
> 且当时没有留存日志或截图；需要可审计的证据，请按本文档自行复跑。

本目录提供两套运行方式：

- **Compose 编排**：PostgreSQL + pgvector、Redis、MinIO、API、ETL worker，适合具备 Docker 的主机。
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
- 具体到服务：`api` 等待 `postgres` / `redis` / `minio` 健康，`worker` 只等待 `postgres` / `redis`
  健康（不等待 `minio`），两者都再等 `migrate` 成功。
- `migrate` 通过 `condition: service_completed_successfully` 放行。
- 只有 `api` 发布主机端口，默认绑定 `127.0.0.1:8100`；其余服务仅在编排网络内可见。
- API 容器内监听 `0.0.0.0`，主机侧仍只绑定回环地址。对外暴露前请在前面加带认证的反向代理。

## 环境变量

变量名与 `src/financecoffer/core/config.py` 一致，模板见 `.env.example`。模板里**只有凭据与部署身份留空**，其余项都带可用默认值：

- Compose 专用：`POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB`
- 数据库与事件：`FC_DB_URL`、`FC_REDIS_URL`、`FC_EVENT_CONSUMER`、`FC_EVENT_STREAM`、`FC_EVENT_GROUP`
- ETL 消费调优：`FC_ETL_CONSUMER_NAME`、`FC_ETL_DLQ_STREAM`、`FC_ETL_CONTROL_KEY`、`FC_ETL_BATCH_SIZE`、`FC_ETL_BLOCK_MS`、`FC_ETL_MAX_ATTEMPTS`、`FC_ETL_BACKOFF_MS`、`FC_ETL_CLAIM_IDLE_MS`
- 向量检索：`FC_EMBEDDING_PROVIDER`、`FC_EMBEDDING_MODEL`、`FC_EMBEDDING_DIM`、`FC_EMBEDDING_URL`、`FC_EMBEDDING_API_KEY`
- 对象存储：`FC_MINIO_ENDPOINT`、`FC_MINIO_ACCESS_KEY`、`FC_MINIO_SECRET_KEY`、`FC_MINIO_BUCKET`
- 运行开关：`FC_COMPLIANCE_ENABLED`、`FC_RATE_LIMIT_ENABLED`、`FC_RATE_LIMIT_PER_MINUTE`、`FC_LOG_LEVEL`、`FC_COLD_AFTER_YEARS`
- 令牌：`FC_MCP_TOKEN`

约定：

- 真实 `.env` 只留在本机，已被 `.gitignore` 忽略；不要提交、不要贴进日志。
- 模板里的凭据项**一律留空**，仓库因此可以直接公开：空串不是值，复制出去也不构成泄漏。
- 留空不等于能启动：`compose.yaml` 把必填项写成 `${VAR:?}`，为空时 `docker compose`
  拒绝渲染并点名该键；应用侧 `FC_DB_URL` 为空则报 `E_CONFIG`（not configured）。
  照抄模板而不填的副本启动不了，这是刻意的。
- 配置层另有一道兜底：凭据字段拒绝字面量 `CHANGE_ME`（DEC-0024），旧模板留下的
  占位符同样无法启动。
- Compose 文件中没有任何字面量密钥，全部通过 `.env` 插值；`compose.yaml` 里标了必填（`:?`）的项缺失或为空会让 `docker compose` 直接报错，标了默认值（`:-`）的 ETL 调优项缺失时静默回落到默认值。
- `fcoffer config show` 的脱敏行为不受部署脚本影响。

## 照抄模板时要填哪几项

`cp deploy/.env.example deploy/.env` 之后，**只有下面 9 项要你决定**（模板里这 9 项都是空值），
其余保持模板原样即可。其中 `FC_EMBEDDING_API_KEY`、`FC_MCP_TOKEN` 在特定部署下**本就该留空**，
表里已注明；空值在配置层等同「未设置」，会回落到内置默认值：

| 变量 | 是什么 | 从哪来 |
| --- | --- | --- |
| `POSTGRES_USER` | 应用连接数据库用的角色名，Compose 用它在 `postgres` 容器里建账号 | 自定，只需符合 PostgreSQL 标识符规则 |
| `POSTGRES_PASSWORD` | 上面这个角色的口令 | 自定，用密码管理器或 `openssl rand -base64 24` 生成 |
| `POSTGRES_DB` | 应用数据库名 | 自定，通常取 `financecoffer`；必须与 `FC_DB_URL` 里的库名一致 |
| `FC_DB_URL` | 应用侧 PostgreSQL DSN，内含上面那个口令 | 由前三项拼出：`postgresql+psycopg://<user>:<password>@<host>:5432/<db>`。Compose 内会用 `POSTGRES_*` 重建该 DSN，这一项只在同一份 `.env` 被容器外的命令复用时才生效 |
| `FC_REDIS_URL` | Redis 连接串，Redis 开了 `requirepass` 时把口令写进去 | 本编排的 `redis` 服务不开认证，填 `redis://127.0.0.1:6379/1` 即可（容器内会被编排覆盖为 `redis://redis:6379/1`）；容器外复用同一份 `.env` 时按实际部署填 |
| `FC_EMBEDDING_API_KEY` | embedding 服务的访问密钥 | `FC_EMBEDDING_PROVIDER=openai` 时由服务提供方签发；`local` 用不到，**留空** |
| `FC_MINIO_ACCESS_KEY` | MinIO 根用户（access key） | 自定，`minio` 容器用它初始化根账号 |
| `FC_MINIO_SECRET_KEY` | MinIO 根口令（secret key） | 自定，至少 8 位，用密码管理器生成 |
| `FC_MCP_TOKEN` | MCP 接口的 bearer token | 自行生成，例如 `python -c "import secrets; print(secrets.token_urlsafe(32))"`；**仅回环地址部署**可留空 |

其余项是可直接生效的默认值：枚举（`FC_EVENT_CONSUMER`、`FC_EMBEDDING_PROVIDER`、
`FC_LOG_LEVEL`）、布尔（`FC_COMPLIANCE_ENABLED`、`FC_RATE_LIMIT_ENABLED`）、
整数（`FC_ETL_*`、`FC_RATE_LIMIT_PER_MINUTE`、`FC_COLD_AFTER_YEARS`）与普通名字
（stream/group/bucket 等）。需要调优时直接改对应项，`compose.yaml` 会把它传进容器；
改完请重新 `up -d`（`docker compose` 不会热加载 `.env`）。

### 空值在这份模板里的两种含义

模板把凭据留空，是为了让仓库可以直接公开（空串不是值）。但同样是空值，落到运行时的
后果并不一样，填之前要分清楚：

- **必填项留空 = 起不来。** `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`、
  `FC_MINIO_ACCESS_KEY` / `FC_MINIO_SECRET_KEY` 在 `compose.yaml` 里是 `${VAR:?}`，
  为空时 `docker compose` 直接拒绝渲染并点名该键；`FC_DB_URL` 为空则应用侧报
  `E_CONFIG`（not configured）。
- **可选项留空 = 不启用。** `FC_EMBEDDING_API_KEY` 在 `local` provider 下用不到；
  `FC_MCP_TOKEN` 为空表示不做 bearer 校验，**只允许回环部署**，任何能被其他机器访问的
  部署都必须填上。

另外，配置层仍拒绝字面量 `CHANGE_ME`（DEC-0024）：从旧模板复制过来的 `.env` 若还留着
占位符，启动时同样会被 `E_CONFIG` 拦下并点名变量。

填完先渲染一遍再启动，渲染能过就说明必填项都已有值：

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml config >/dev/null \
  && echo '必填项已齐，可以启动' || echo '还有必填项为空，看上一条报错里的键名'
```

`deploy/.env.example` 原样渲染会直接失败（必填项为空），那是正常现象——只有填过的
`deploy/.env` 才能渲染成功。渲染结果里 `api`、`worker`、`migrate` 三个应用服务都应带上
`FC_ETL_*` 等项目（缺了说明该 `.env` 是按旧模板写的，或是编排文件漏了转发）。

## 首次启动

```sh
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env，填掉 9 个空着的凭据项（见上一节「照抄模板时要填哪几项」）

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

口径先说清楚：这份编排**只在本地做过「compose 静态解析 + 数次人工实跑」**，实跑时没有留存日志、
截图或脚本，仓库里也没有对应产物。下面是对那几次操作的追述，可追溯性到此为止——它证明不了
「已上线」，也不能作为生产验证的证据。

| 项 | 记录 |
| --- | --- |
| 环境 | 本地 Docker（Linux 内核，`docker compose` v2），不是服务器 |
| 时间 | 2026-10-28：10:23（+0800）之前跑过一轮（迁移 / ETL / 接口链路），12:10 之前又跑过一轮（worker 存活探针改动）——两个时刻都取自记录它们的提交时间，实跑本身在此之前。无日志留档，精确时刻无法确认 |
| 应用镜像 | 本地构建的 `financecoffer/app:local`（即 `compose.yaml` 的 `image` 值，未推送，digest 未记录） |
| PostgreSQL | 本机已有的 pgvector **pg17** 镜像，代替编排固定的 `pgvector/pgvector:pg16`；该镜像的具体 tag 未留档，仓库仍以固定的 `pg16` 为准 |
| Redis | `redis:7.4-alpine`（编排固定值） |
| MinIO | **未启动**：`minio/minio` 镜像在各镜像源拉不到（403 / not found），当轮临时去掉 `api`、`worker` 对 `minio` 的 `depends_on` |

那几轮人工操作覆盖的点（全部无日志留档）：

| 验证点 | 结果 |
| --- | --- |
| `build` 构建应用镜像 | 成功（builder 内装 `.[api,db,etl,vector,s3,mcp]`，runtime 为非 root 用户） |
| `migrate` 在空库执行迁移 | 退出码 0，迁移一路到 head，`db status` 显示 `pending 0` |
| `postgres` / `redis` / `api` / `worker` 健康 | 四个容器均达到 `healthy` |
| `GET /health` | 200，`database: ok`；未接入 embedding 服务时为 `embedding: unreachable` |
| `GET /metrics` | 导出请求计数、仓库侧 ETL/归档计数与向量层计数；每族注册即输出，无数据时为 0 值基线（见「监控」） |
| `db partitions create --month <YYYY-MM>` | 建出当月 ODS 分区 |
| `ingest event`（CLI 同步落库） | ODS 与 DWD 同时写入，`source` 由 `source_info.hub` 推导 |
| 向 Stream 投递事件 → worker 消费 | `ops.etl_event_log` 记为 `done`，ODS 与 DWD 各新增一行 |
| 重复投递同一事件 | 判为 `duplicate` 跳过，不产生第二行 |
| 投递 `event_id` 不合法的事件 | 进入 DLQ，日志给出 `event_id` 校验原因 |
| `/v1/structured/<symbol>`（库中无宽表行） | 404 `E_NOT_FOUND`，符合预期 |
| `/v1/risk/<symbol>` | 200 |
| worker 存活探针 | 正常时 `healthy`；冻结消费进程后约两分钟转 `unhealthy`（存活键过期 + 连续 3 次探测失败），恢复后自动回到 `healthy` 并继续消费 |

### 与仓库内 `compose.yaml` 的差异

那一轮用的是一份**临时改过、未入库**的 `compose.yaml`：删掉了 `api`、`worker` 对 `minio` 的
`depends_on`，只跑 postgres / redis / migrate / api / worker。仓库里提交的版本**仍然依赖 minio**，
这是有意的：

- 应用的对象存储走 MinIO（`s3` extra，容器内 `FC_MINIO_ENDPOINT=minio:9000`），去掉依赖会让
  API 在对象存储健康之前启动；
- `tests/unit/test_deploy_assets.py::test_startup_order` 已经把「`api` 依赖 `minio` 健康」写成断言，
  删掉依赖会让该测试直接失败。

所以：**按仓库文件复跑，跑的不是当时那一轮**——会多一个 `minio` 服务，并顺带真正校验 MinIO 的
启动与健康检查。要复现当时的形态，请自行在本地临时删掉依赖（不要改仓库文件），并接受对象存储
此时不可用。

### 仍未验证的部分

- **MinIO 读写**：容器内没起 MinIO，S3 路径没在容器里跑过；本地二进制方式见
  `docs/decisions/DEC-0014-object-storage-minio-local-binary.md`。
- **embedding 调用**：宿主机 embedding 服务未在容器网络内应答，`/health` 因此显示
  `embedding: unreachable`。
- **真实服务器 / 生产环境**：从未部署，没有任何相关记录。

## 升级与回滚

备份与恢复不是手敲几条 `pg_dump`：`scripts/backup.py` 产出可校验的备份，`scripts/restore.py` 只允许恢复到空库，并在恢复后逐项比对。升级前先用前者留一份，回滚到旧数据时用后者。

### 备份

```sh
export FC_DB_URL='postgresql://fcoffer:<password>@127.0.0.1:5432/financecoffer'
python scripts/backup.py                              # 默认写到 var/backups/<UTC 时间戳>/
python scripts/backup.py --out /srv/fc-backups/manual-1029
```

一次备份产出两个文件：

- `dump.pgc`：`pg_dump -Fc` 自定义格式归档，用 `pg_restore` 还原。
- `manifest.json`：备份时间、git 提交（含工作区是否脏）、迁移版本（`ops.schema_version` 账本）、表 / 索引 / 约束 / 序列清单、**逐表行数与内容校验和**，以及归档文件的字节数与 sha256。

DSN 只从环境变量读取（默认 `FC_DB_URL`，可用 `--dsn-env` 换名），命令行、日志和清单里都不出现密码。`--lock-wait` 控制 `pg_dump` 等锁时长，默认 120 秒。

### pg_dump / pg_restore 依赖

两个脚本调用宿主机的 PostgreSQL 客户端，**大版本需与服务器一致**（本编排为 16）。客户端不在 `PATH` 时脚本不会抛栈，而是打印三条可操作路径：装客户端、用 `PG_BIN` 指向客户端 `bin` 目录、或把整条命令塞进 `PG_DUMP` / `PG_RESTORE`。

宿主机完全没有客户端时，直接用 `postgres` 容器里的那一份——容器内本地连接免密，无需 `-U` / `-h`：

```sh
docker compose -f deploy/compose.yaml exec -T postgres \
  pg_dump --format=custom --no-owner --no-privileges --no-password financecoffer > dump.pgc

docker compose -f deploy/compose.yaml exec -T postgres \
  pg_restore --no-owner --no-privileges --no-password --dbname financecoffer_restore_check < dump.pgc
```

对应的脚本写法：

```sh
export PG_DUMP="docker compose -f deploy/compose.yaml exec -T postgres pg_dump"
export PG_RESTORE="docker compose -f deploy/compose.yaml exec -T postgres pg_restore"
```

### 恢复

目标库必须先建好、且是空的（`-T template0` 保证不带任何对象）：

```sh
createdb -h 127.0.0.1 -U fcoffer -T template0 financecoffer_restore_check

export FC_RESTORE_DSN='postgresql://fcoffer:<password>@127.0.0.1:5432/financecoffer_restore_check'
python scripts/restore.py --backup-dir var/backups/20261029T031500Z \
  --target-dsn env:FC_RESTORE_DSN
```

安全规则（任何一条不满足都直接退出，不写入数据）：

- `--target-dsn` 必填。给字面 DSN 或 `env:NAME`，推荐后者：密码不进 shell 历史。
- 目标必须是**空库**，非空库默认拒绝。
- 覆盖非空库必须同时给 `--force` 和 `--confirm <库名>`，且 `--confirm` 要逐字敲对库名。
- `postgres` / `template0` / `template1` 这类集群维护库一律拒绝：它们看起来"空"，但恢复进去只会污染集群的管理库。
- 拒绝恢复到备份的来源库本身。
- 动手前先校验 `dump.pgc` 的 sha256 与清单一致；归档被截断或替换时立即停止。
- `--dry-run` 只跑上面这些检查，不落库。

恢复完成后脚本从恢复后的库重建清单，与备份清单逐项比对（迁移账本、表 / 索引 / 约束 / 序列的数量与对象集合、逐表行数、逐表内容校验和、序列位置），打印差异表并把报告写进 `var/restore-reports/<时间戳>/`。末行 `== 比对结果：N/N 项一致 ==` 且退出码为 0 才算恢复成功；有差异时退出码非 0。

### 空库重建演练

一条命令复跑整条链路：建空库 A → `db migrate` 到 head → 写样例数据 → 备份 → 建空库 B → 恢复 → 比对结构与数据 → 负向用例（故意改一行，比对必须报差异）：

```sh
python scripts/backup_drill.py
```

演练只用 `financecoffer_qa_*` 临时库，`financecoffer` / `postgres` / `template0` / `template1` 被硬编码拒绝，结束后自动 DROP 临时库（`--keep` 保留现场）。末行 `== 备份恢复演练：N/N 项通过 ==`，退出码 0 表示通过；过程证据落在被 git 忽略的 `qa_evidence/` 目录。

### 升级流程

1. 先备份：按上文跑 `scripts/backup.py`，确认 `manifest.json` 里的 `schema_version` 与当前库一致，并确认迁移只向前（见 `docs/decisions/DEC-0008-forward-only-migrations.md`）。
2. 重新构建镜像并打新 tag，更新 Compose 中的镜像 tag。
3. `docker compose --env-file deploy/.env -f deploy/compose.yaml up -d`。
4. 观察 `docker compose ps`、`/health`、worker 日志和 Prometheus 告警。

### 回滚流程

1. 把镜像 tag 改回上一个版本，再次 `up -d`。
2. 迁移是单向的：代码可回滚，数据库不自动降级。若新迁移与旧代码不兼容，用升级前的备份恢复——建一个空库，用 `scripts/restore.py` 还原，确认 `== 比对结果：N/N 项一致 ==` 后再切换连接串。
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

Prometheus 需要与 API 在同一编排网络内，或把 target 改成实际可达地址。规则文件可用 `promtool check rules` 校验。

指标名取自 `docs/specs/cli.md` §3.14 的清单。`/metrics` 实际导出三组，来源不同，看到同名指标要先看它来自哪一组：

| 指标 | 来源 | 说明 |
| --- | --- | --- |
| `fc_api_requests_total{method,path,status}`、`fc_api_request_seconds_total` | API 进程内中间件 | 只统计这一个 API 副本收到的请求 |
| `fc_etl_events_total{status}`、`fc_dirty_data_total{reason}`、`fc_archive_job_total{status}`、`fc_etl_consume_lag_seconds` | 每次抓取读 `ops.etl_event_log` / `ops.etl_dlq` / `ops.archive_job` | 跨进程、跨副本一致；每个家族注册即输出，库读不到时仍输出 0 值基线 |
| `fc_missing_chunk_total{reason}`、`fc_vector_search_seconds*` | 向量层进程内计数 | 只反映**当前 API 进程**做出的切片与检索；切片实际发生在 `fcoffer vector ingest` 与 worker 里，所以标准编排下这两族通常没有样本 |

**零值基线有副作用，排查时必须记住**：`ops.*` 读不到时这些族照样输出 0，**采集失败看起来和「真的为 0」一模一样**，`increase(...) > 0` 那类告警不会响。采样失败时 API 日志会打一条 `metrics: ops sample unavailable` 的 WARN；怀疑采集链路时，认准「业务在动但指标恒为 0」这个特征，先看日志再信面板。

`fc_etl_consume_lag_seconds` 取的是 `received` / `processing` 里最早一条的 `received_at` 距今秒数，
也就是「最早那条排队事件等了多久」，不是「落后 stream 队头多少」，也不看 `event_time`——按业务时间算会把正常的历史补录误报成巨大延迟。仓库里只要有排队事件就一定有值，消费端把它清完才回落。
所以这个值持续偏高有两种可能——消费停了，或者有条事件被重置成排队态却没人再投递它。

`fc_missing_chunk_total` 在标准编排下**没有采集点**，对应告警恒为无数据：不会误报，但也不会报——计数留在切片进程内，见上表。

连接池水位**不监控**：本产品不暴露连接池指标家族，`rules.yml` 里也没有以它为条件的告警，原因见 `docs/specs/financecoffer.md` §7。API 的读路径用 psycopg 直连、请求结束即归还，没有连接池可采样；持有 SQLAlchemy 池的 worker 不暴露 HTTP 端口。与其伪造一个恒为 0 的读数、让「池未饱和」的告警永远静默，不如明确不监控；等真正引入连接池后再补真实数值。

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
