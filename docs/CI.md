# CI 门禁：实际命令、本地等价命令与 required checks

本文件描述 `.github/workflows/ci.yml` 的真实行为，以及在本机复现同一门禁的命令。
最后核对：**2026-10-31**（`main@15d30b6`，CI 绿）。

## 一、CI 实际跑什么

触发：`pull_request` + `push` 到 `main`。同一 ref 的新运行会取消旧运行（`concurrency.cancel-in-progress`）。

权限：`permissions: contents: read`。只使用仓库内置的 `GITHUB_TOKEN`，**不需要配置任何 secret**。

| Job（同时是 check 名） | Runner | 步骤 |
| --- | --- | --- |
| `backend (pytest / ruff / mypy)` | `ubuntu-latest`，Python 3.11 | `pip install -e ".[all,dev,s3]"` → `pytest` → `ruff` → `mypy`（非阻断） |

原始命令（与 workflow 逐字一致）：

```bash
python -m pip install -e ".[all,dev,s3]"
python -m pytest -o addopts=""
python -m ruff check .
python -m mypy
```

两点说明：

- `s3` extra 必须装：测试收集期会导入 `boto3`。
- `mypy` 步骤带 `continue-on-error: true`，**当前不阻断合并**。截至 2026-10-31 有 24 条既有错误（13 个文件），属历史基线；清账后删掉 `continue-on-error` 即变成硬门禁。
- `-o addopts=""` 覆盖 `pyproject.toml` 里的 `addopts = "-q"`，本地复现时不要省略。

## 二、本地等价命令

前置：Python ≥ 3.11（3.12 已验证）。测试可在无 PostgreSQL / Redis 的机器上离线跑完，跳过的集成用例会自行 skip。

### Windows（PowerShell / cmd）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[all,dev,s3]"

.\.venv\Scripts\python.exe -X utf8 -m pytest -o addopts=""
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
```

### macOS / Linux

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[all,dev,s3]"

./.venv/bin/python -m pytest -o addopts=""
./.venv/bin/python -m ruff check .
./.venv/bin/python -m mypy
```

venv 解释器路径差异：Windows 是 `.venv\Scripts\python.exe`，POSIX 是 `.venv/bin/python`。

`mypy` 退出码当前为 1（24 条既有错误），与 CI 的非阻断行为一致；`pytest` 与 `ruff` 必须为 0。

### 本机实测（2026-10-31，Windows 10 / Python 3.12.10）

- `pytest`：1043 passed, 33 skipped（75.77s）
- `ruff check .`：All checks passed
- `mypy`：24 errors in 13 files（与 CI 基线一致，非阻断）

## 三、required checks 现状

**当前未设为 required，仅为参考。**

核对方式（2026-10-31，公开 API）：

- `GET /repos/QuotaFuse-ai/FinanceCoffer/branches/main` → `"protected": false`
- `GET /repos/QuotaFuse-ai/FinanceCoffer/rulesets` → `[]`

即 main 既没有 branch protection，也没有 repository ruleset。CI 会跑并给出红绿信号，但**不会阻止合并**；要获得强制力，需要仓库管理员在 Settings → Branches（或 Rulesets）里配置。

若管理员启用保护，应把下面的 check 设为 required，名称需逐字一致：

- `backend (pytest / ruff / mypy)`

注意：`mypy` 是同一 job 内的一个 step，不是独立 check；把它设成硬门禁要先在 workflow 里去掉 `continue-on-error`。配置完成后本文件需同步更新为实际值。
