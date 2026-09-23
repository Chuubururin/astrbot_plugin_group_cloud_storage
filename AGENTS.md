# AGENTS.md

给在本仓库工作的 AI agent 的约定。门禁命令清单见 [README.md](README.md#开发与测试)，
这里只写 README 不覆盖的部分：改动边界、注释口径、测试证明力、本机环境坑。

## 改动边界

- **修正即替换，不是追加。** 一次改动交付的是最终形态，不是改动过程。被否决的
  方案、试过的中间实现、"原本想怎么做"都不进代码树。
- **不写兼容垫片。** 仓库内零调用方的分支/参数/别名直接删。例外：`webapi/` 的
  请求体形状可能被插件外部调用方使用，删之前先确认——`pages/storage-ng/testing/unit/chain.test.mjs`
  里的链路测试就是这层确认，它钉住的兼容分支不是死代码。
- **不做顺手清理。** 修 bug 不附带重构，一次性操作不抽公共函数。三行相似代码
  优于一个过早的抽象。
- **不为不可能的状态写防御。** 只在校验系统边界（用户输入、外部 API）时兜底，
  内部代码信任调用约定。

## 注释与命名

注释只写**当前的、非显而易见的约束**：隐藏的前置条件、微妙的不变量、针对具体
行为的 workaround、会让读者意外的语义。

- 不写"这段代码做了什么"——命名已经说了。
- 不写演进史：`修复前…`、`曾经…`、`used to…`、`no longer…` 一律不进源码。
  这些属于 commit message 和 PR 描述，会随代码演化而失真。
- 不写工单/事件编号：`Bug-13`、`Item 8`、`FE-16`、`P0a` 这类标签对读者是噪音。
  测试名描述行为，不描述它来自哪一轮整改。
- **例外**：外部协议事实可以带观测日期，例如
  `_STORAGE_MARKERS` 上的 `(2026-09-21 live: ...)`。那是证据来源，不是改动记录。
- **例外**：`adapters/persistence/sqlite/migrations.py` 的版本链、
  `tests/unit/test_architecture.py` 的棘轮基线、`tools/check_doc_drift.py` 的
  `K1-K5`/`R1-R3` 规则号，都是有意保留的历史/编号，不要"清理"。

## 测试证明力

新测试必须**改前失败、改后通过**，并且要用变异实测确认，而不是靠推理：把源码里
的那一行防护去掉（或换成错误实现），跑一次，看它真的红。跑完恢复。

- 负向断言要配对照组。"跨账号不互相等待"单独成立没有意义——无限速时也成立；
  必须同时断言"同账号必须等满窗口"。
- 时序断言留 ≥2× 余量。窗口 0.6s 对应上界 0.3s / 下界 0.5s；0.15s 的上界在全量
  跑动下会偶发抖动。
- 断言"某个查询计划/某个索引被选中"优于断言耗时：前者确定性，后者依赖机器负载。
  但只断言在任意代价模型下都站得住的选择——要钉的是数量级差异（每键 1 行 vs
  每键数百行），不是小样本均匀夹具下某版本的临界点：CI 的 libsqlite 比开发机旧，
  临界点会翻红而本机全绿。
- 依赖数据库版本启发式的行为（如 `PRAGMA optimize` 决定要不要分析）不能当测试前提。
  用数据自身能回答的守卫（"这张表有没有统计行"）替换，行为跨版本一致。
- 跳过不是通过。`REQUIRE_NO_SKIP=1` 下任何 skip 都是失败。skip 一般来自未装的可选
  测试依赖（`impacket`/`pysmb`/`paramiko`/`pillow`，全在 `requirements-dev.txt`），
  装上即归零。

## 本机环境

- `node` 不在可用 PATH 上（`/root` 符号链接失效）：用 `/home/nuc/.hermes/node/bin/node`。
- `pyright` 不在 PATH：用 `~/.local/bin/pyright`。
- `node --test` 需要 glob 而不是目录：
  `node --test "testing/**/*.test.mjs"`（在 `pages/storage-ng` 下跑）。
- `tests/unit/test_queue_index_invariants.py` 是仓库唯一的 **CRLF** 文件。脚本改它
  必须按二进制处理，否则整份文件变成 636 行空白差异。
- `.mimosa/`、`.opencode/`、`node_modules/`、`out/`、`dist/` 是被 gitignore 的工具
  目录，会污染全仓 grep——搜索时排除。
- 装 dev 依赖要 `python3 -m pip install --user --break-system-packages ...`：
  本机是 PEP 668 externally-managed 环境，裸 `pip install` 被拒；既有依赖
  （pytest / paramiko / pillow）都在 `~/.local/lib/python3.13/site-packages`，
  `--user` 只写这个用户站点，不碰系统站点。
- `pytest -n auto` 需要 `pytest-xdist`。缺它就只有串行口径，而 CI 跑的是
  `-n auto --dist loadfile`——并行度与负载都是 CI 独有的失败维度，本机复现
  不了它，就别把"本机全绿"当成"CI 全绿"。
- 未提交的工作树里不要用 `git checkout --`/`git restore`/`git reset --hard` 之类的
  破坏性命令"顺手清理"。要看文件内容就读文件。

## 提交

- 不在未经确认的情况下 `git commit` / `git push`。
- `dev` 与 `main` 远端受保护，force-push 会被拒（`GH006`）。
- 推送 `v*` tag = 公开发布 Release，不可逆，必须显式确认。
