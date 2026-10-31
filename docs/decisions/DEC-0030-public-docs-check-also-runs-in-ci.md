# DEC-0030 公开文档卫生检查的第二执行点是 CI 硬门禁

- 日期：2026-10-31
- 状态：已采纳
- 关联：`DEC-0025`（判定口径）、`DEC-0026`（内部过程材料只留本地）、`DEC-0029`（第一执行点：提交前钩子，本决策更新其第 5 条的结论，不回改原文）、`.github/workflows/ci.yml`、`tools/check_public_docs.py`、`.pre-commit-config.yaml`、`tests/unit/test_public_docs.py`、`docs/CI.md`、`README.md` 第 10 节

## 背景

DEC-0029 第 5 条自己记下了缺口：钩子可以被 `git commit --no-verify` 跳过，未安装钩子的检出不会自动检查，所以主干流水线合入后要补一个 CI step 调用同一份脚本。主干 CI 已经落地（行为见 `docs/CI.md`），缺口该补。

事实核对：钩子的拦截力从未被真实执行点验证过——维护机上没有跑 `pre-commit install`（原因见决策第 1 条），所以「提交时拦下」这句在 DEC-0029 里是设计意图，不是已观察到的行为。本决策把硬门禁放在一个不依赖个人环境的执行点上。

## 决策

1. **执行点结论从「仅提交前钩子」更新为「钩子配置随仓库下发（本地可选）+ CI 硬门禁」**。`.pre-commit-config.yaml` 继续留在仓库里作为可选项，但它不再是唯一执行点，也不再被描述成门禁。

2. **CI 只加一个 step，直接调用同一个脚本，不复刻规则。** 位置在 `backend` job 内、`pip install` 之前：

   ```yaml
   - name: public-docs
     run: python tools/check_public_docs.py
   ```

   规则、词表、豁免路径的唯一出处仍是 `tools/check_public_docs.py`。在 CI 里另写一份正则（或改用第三方 markdown lint 插件）会产生两份口径，公开面规则一旦调整就出现「CI 与本地说法不一致」这种最难查的分歧。

3. **不新增依赖、不新增 job、不新增 required check 名称。** 脚本 import 的全在标准库内（`re`、`sys`、`collections.abc`、`dataclasses`、`pathlib`），`actions/setup-python` 给的 Python 直接能跑，因此放在装依赖之前也不会因为环境缺失而假报错。留在 `backend` job 内，check 名仍是 `backend (pytest / ruff / mypy)`，`docs/CI.md` 第三节的 required-checks 配置不用跟着改；拆成独立 job 反而要管理员在分支保护里补一个新名称。

4. **不在机器上执行 `pre-commit install`。** 三条理由，按重要性排：
   - `.git/hooks` 是共享目录，装一次会影响该仓所有 worktree 与所有 agent 的提交，而这不属于本单范围；
   - 钩子是 `language: system`、`entry: python tools/check_public_docs.py`，走的是 PATH 上的 `python`；本机约定解释器是固定 venv 的绝对路径，装上后可能把别人的提交卡在解释器或依赖上——把别人的提交卡死不是安全措施，是故障；
   - `--no-verify` 本就能跳过，它从来不是硬门禁，把它装齐并不增加拦截力。

5. **本地复检口径**（都不改仓库状态）：`python tools/check_public_docs.py`，或 `pre-commit run public-docs --all-files`（后者要求调用者自行装过 pre-commit，属于个人环境，不由本仓库统一安装）。

6. **验收要在真实 CI 上看红绿**，本地退出码只算前置。红测落点用**一次性验证分支 + 验证 PR**：在开发分支之上新建分支，往 `docs/` 加一个含违规行的新文件，PR 变红；删掉该文件再推，PR 变绿；关闭 PR 且不合并，删除临时分支。不在交付分支上做「加了再删」，因为本仓主干历史按 merge commit 合并（`main` 顶端即是），中间 commit 合并后会永久可达 `main` 历史——违规内容不得合入任何主分支这条要求，包含「不得留在历史里」。

7. **「CI 硬门禁」这句话有一个前提，没配之前它只是红灯**：CI 给出红绿信号，但要它真的挡住合并，需要仓库管理员把 check 设为 required。实测口径（核对方式见 `docs/CI.md` 第三节）：main `protected: false`、rulesets `[]`。在这个状态下，一个变红的 PR 仍然能被合进 `main`——跳过提交前钩子的人换成了无视红灯的人，`--no-verify` 并没有被真正堵死。所以本决策同时要求：把 `backend (pytest / ruff / mypy)` 这个 check 名设为 required（`public-docs` 在同一 job 内，跟着这一个 check 一起生效，不需要额外名称）。在这条配置落地之前，第 1 条的「硬门禁」应当读作「不可跳过、且当前唯一的机器执行点」。

## 权衡

- **既然 pytest 已经会跑同一条基线用例，为什么还要显式 step？** `tests/unit/test_public_docs.py::test_repo_public_surface_is_clean` 确实在 CI 的 pytest 步骤里跑同一个脚本，且已被 PR #2 验证过拦截力（main 上 1 failed，清理后全绿）。但它失败时报的是测试用例名加断言 diff，违规行号和原因藏在 pytest 的 stdout 里；显式 step 直接把 `文件:行: 命中原因` 摊在 step 日志首屏，谁都能看到该改哪一行。重复一次的成本是几毫秒（公开面百余个文本文件全量扫描），换来的是可读的失败信号，值得。
- **放在 `pip install` 之前**：违规的 PR 通常连依赖都不用装就该被拒，省掉一次完整安装；代价是这个 step 的失败会挡住后面的测试步骤，而这正是想要的顺序。
- **钩子留在仓库里不删**：它对新克隆的开发者仍是零成本的提醒（README 第 10 节写了安装命令），只是不再被当门禁。删掉等于把「本地能提前发现」这个便利也扔掉。
- **`docs/decisions/` 被 `.gitignore` 标为「只留本地」，本决策仍然随仓库发布（`git add -f`）**：这是沿用 DEC-0029 的做法——执行点写进决策才有出处，`README.md` 与 `docs/CI.md` 要链到它，脚本的豁免前缀也按这个目录存在来设计。代价是发布出去的那几份决策必须自己写干净（不写内部任务编号、团队称谓、本机路径）：豁免是按路径生效的，一旦该目录随公开面发布，路径豁免就挡不住内容外泄。顺带记一个既有缺口，不在本单修：`README.md` 第 10 节链到 `docs/decisions/DEC-0025-...md`，那份决策没有发布，链接在公开仓库里是死的。
- **代价**：每次 PR 多一个 step，公开面检查在 CI 与 pytest 里各跑一次；重复扫描同一份公开面是有意为之的冗余，两处口径来自同一个脚本，不会漂移。

## 影响

- 修改：`.github/workflows/ci.yml`（`backend` job 新增 `public-docs` step）、`docs/CI.md`（步骤表、原始命令、本地等价命令、required checks 说明）、`README.md` 第 10 节（补一句 CI 执行点）。
- 新增：本决策。
- 未改动：`tools/check_public_docs.py`、`.pre-commit-config.yaml`、`tests/` 检查规则与用例一律不变——本单不碰规则本身，只加执行点。
- 待验证（本决策落地时如实标记，不假称已完成）：CI 红 / 绿两次 run 的证据。开发分支已推送，但 `pull_request` 是 CI 的触发条件之一，PR 由谁创建、创建后 run 的红绿结论需要在本决策的关联工单评论里补齐链接；在那之前第 6 条只算「方法已定」，不算「已验证」。
