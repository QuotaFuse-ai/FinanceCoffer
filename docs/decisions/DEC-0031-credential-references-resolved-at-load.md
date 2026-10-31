# DEC-0031 凭据引用在内核解析，Windows keyring 走 ctypes 而不引入 keyring 包

- 日期：2026-11-01
- 状态：已采纳
- 关联：`DEC-0025`（公开文档判定口径）、`DEC-0030`（公开文档检查执行点）、`docs/specs/financecoffer.md` §8.1（引用语法）、`docs/specs/cli.md` §3.1、§3.3、`src/financecoffer/core/secrets.py`、`src/financecoffer/core/wincred.py`、`src/financecoffer/config/loader.py`、`src/financecoffer/cli/commands/config_secret.py`、`src/financecoffer/cli/commands/serve.py`

## 背景

上一次多 worker 的配置改造（commit `5e59dad`）让 `serve api --workers N` 在 fork 前把已解析配置导出成 `FC_*`，多 worker 的配置口径终于与单 worker 一致，代价记录在当时：DSN（含密码）、MinIO AK/SK、MCP token 因此多了一个落点——主进程以及它 fork 出的每个 worker 的环境块。环境块可被同 uid 的进程读走（Linux 下 `/proc/<pid>/environ`），也会进崩溃转储与 `ps e` 一类工具的输出。

本决策要定两件事：引用在哪一层解析（决定这条路径上还有谁能看见明文），以及 Windows 上的凭据库用什么实现（决定要不要新增第三方依赖）。

## 决策

1. **解析点在 `load_config()` 内部、`Config(**values)` 校验之前**，不在 CLI 层、不在各消费者层。
   `config/loader.py::_resolve_references` 遍历五个凭据字段，把引用换成明文再交给 pydantic。放这里有三条理由，缺一条就不成立：
   - 校验必须作用在**解析后的值**上。`FC_DB_URL` 有 DSN scheme 校验，引用串 `file:/x` 过不了它；反过来若先构造 `Config` 再解析，`SecretStr` 已经把引用当明文存下，任何 `model_dump` 都会带出引用——把引用当密钥泄出去虽然不致命，但会让「配置里的值」有两种含义。
   - 消费者数量会继续增加（REST、MCP、CLI、ETL、archive）。解析放在唯一入口，新增消费者天然继承这条路径；放在 CLI 层则 `create_app()` 作为库入口（uvicorn worker 走的就是它）会绕过解析。
   - 失败点前移到「配置加载」这一次性动作，服务不带着一个解不开的引用继续启动。

2. **`Config` 的字段类型与脱敏口径一字不改**：仍是 `SecretStr`，`redacted_dict()` / `config show` / 日志 / `/health` 的输出保持原样。新增的是 `LoadedConfig.sources`——保留**未解析的原始串**，只用于跨 fork 导出与 `config secret list`。凭据字段之外的值不进 `sources`。

3. **`serve api --workers N` 导出引用本身，不导出解析结果**（`serve.py::_config_environment`）。每个 worker 自己解析一次，代价是启动时多几次文件读/凭据库查询，换来明文不进任何进程的环境块。写成明文的凭据仍按明文导出：CLI 无法凭空造出一个引用，这条改动对旧部署是零影响，不是零保护——要保护就必须改用引用，文档里把这句话写明。

4. **Windows 的 keyring 用 `ctypes` 直接调系统 API，不引入 `keyring` 包**。
   实测口径（固定 venv，Windows 10 19042）：`keyring==25.7.0` **在本机是可用的**——`keyring.get_keyring()` 选中
   `keyring.backends.Windows.WinVaultKeyring (priority: 5)`，`get_password()` 对不存在的条目返回 `None`，即它确实能直接打 Windows 凭据管理器，不需要任何额外服务。但 `pip show keyring` 的 `Required-by` 只有 `twine`（发布工具），它不是 `financecoffer` 声明的依赖：本机「能 import」是发布链路过来的传递依赖留下的巧合，换一台按 `pip install financecoffer` 装的机器就没有。把它当运行时依赖，等于让数据面服务的基础设施路径取决于发布工具的选择。
   另一边，`ctypes` + `advapi32`/`crypt32` 是标准库自带，`CredWriteW/CredReadW/CredDeleteW/CredEnumerateW` 与 `CryptProtectData/CryptUnprotectData` 全部可用，凭据管理器 GUI 能看到写入的 generic credential。所以 Windows 用 `ctypes`，`keyring` 只作可选 extra（第 6 条）。

   备选与否决理由：

   | 方案 | 结论 | 理由 |
   | --- | --- | --- |
   | A. 引入 `keyring` 包（跨三平台） | 可用，但不做**必需**依赖 | 上面实测它在 Windows 上确实工作。不选它作必需项的理由是依赖所有权而非功能：`Required-by` 里只有发布工具，说明它不是本包能主张的能力；且它自身在 Linux 上要 Secret Service，在 Windows 上后端仍是同一套凭据管理器 API，只是多一层抽象。它作为**可选 extra** 保留（见第 6 条）。 |
   | B. `win32credential` / pywin32 | 否决 | 比 A 更重，且只为 Windows 一个平台。 |
   | C. PowerShell `Get-StoredCredential` / `cmdkey` 子进程 | 否决 | 需要 shell  out、解析人类可读输出、命令注入面、非回环凭据名转义；`ctypes` 没有这些。 |
   | D. 只支持 DPAPI 加密文件，不做凭据管理器 | 不足 | 能满足 `file:`，但拿掉「凭据不进文件系统」这一档；本仓的 Windows 部署确实有用户级凭据管理器可用，没理由不接。 |
   | E. `ctypes`（选定） | 采纳 | 零新依赖、无子进程、错误码直接来自 `GetLastError`，实现约 200 行且只依赖稳定的 Win32 结构布局。 |

   代价要如实记下：结构体字段顺序与对齐由我们自己维护（`CREDENTIALW` 的 15 个字段），一次写错就是堆损坏——本决策落地过程中真实踩过一次（`Windows fatal exception: code 0xc0000374`，DPAPI 输出 blob 被 `LocalFree` 两次），修法是「复制出来」与「释放」分成两个函数、每个 blob 恰好释放一次。回归由 `tests/unit/test_secret_references.py` 的 DPAPI 往返用例守着。

5. **DPAPI 是 `file:` 的可选保护，不是独立 scheme**：文件内容以 `dpapi:v1:<base64>` 开头时按 DPAPI 解封。这样引用语法只有一个含义（指向哪个文件），加密与否是文件的属性；`fcoffer config secret set <field> --store file --dpapi` 写出来的就是这个形态。跨用户/跨机器不可解，这是 DPAPI 的语义，不是 bug，错误消息按此提示。

6. **macOS / Linux 通过 `keyring` 包（可选 extra）接入**，`core/secrets.py::PythonKeyringBackend` 惰性 import。未安装时给出 `E_CONFIG` 并点名两条出路（装 `financecoffer[keyring]`，或改用零依赖的 `file:` / `env:`），绝不静默退化成明文或空值。`pyproject.toml` 的 `keyring` extra 是可选的，主干依赖列表不变——这是第 4 条「不引入必需依赖」得以成立的前提。

7. **`env:` 保留为显式退路而不是隐藏行为**：它不带来任何保护（值仍在环境里），存在的意义是让旧的「先 `export FOO=...` 再 `FC_MCP_TOKEN=env:FOO`」部署能声明意图。`config secret` 三个命令都拒绝写 `env:`——一条命令不该去改调用它的那个 shell 并不拥有的变量。

8. **引用的判定只看三个 scheme 前缀，其余一律明文**。`postgresql://user:pass@host/db` 里就有冒号，任何「先猜是不是引用」的实现都会把一个合法 DSN 读成坏引用。大小写敏感，`file:` 之外不识别任何 scheme。语法错（`keyring:onlyservice`、`file:`、`env:BAD NAME`）是硬失败而不是「当作明文」——把 typo 静默存成密钥值，等于部署了一个没人能轮换的凭据。

## 权衡

- 换来的：明文不再进 worker 环境块；凭据可以轮换而不动 `.env`（改库里那份）；`.env` 模板可以放心进仓库（值列只有引用）；配置加载是唯一解析点，新增消费者不需要重复这件事。
- 付出的：Windows 上约 200 行 `ctypes` 绑定要自己维护（结构布局、错误码、`LocalFree` 生命周期）；macOS/Linux 要用 keyring 就必须装 extra；引用多了失败点（文件权限、SELinux/Keyring daemon 未启动、DPAPI 用户不匹配），因此第 8 条的硬失败必须先于任何「连不上」出现，否则只是把故障挪了个位置。
- 没有解决的：`--db-url` 命令行覆盖仍是明文（在 argv 里，`ps` 可见），这是既有的同级暴露，本次不收口；DPAPI 只覆盖「文件被读到但拿不走明文」，不覆盖同用户读取。

## 验证

- 单元：`tests/unit/test_secret_references.py`（语法、三 scheme 解析、五字段端到端解析、失败硬退出、DPAPI 往返与损坏 blob、脱敏与 `repr`、`sources` 只含引用）、`tests/unit/test_config_secret_commands.py`（`set/list/delete` 全部走 fake backend 与临时文件，不触碰本机凭据库）。
- 集成：`tests/integration/test_secret_reference_connectivity.py`（五个字段各一条「引用 → 能连」，外加一条负例：错密钥必须被存储拒绝，否则这些用例是假绿）。
- fork 证据：`tests/integration/test_serve_workers_config.py::test_workers_inherit_references_and_no_plaintext`——子进程用 `sitecustomize` 把自身 `FC_*` 写成 sha256 摘要表，断言引用摘要在场、明文摘要在任何一个进程里都不在场。
