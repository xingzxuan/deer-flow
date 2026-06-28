# PR7 · CI boundary 静态扫描（`langgraph.checkpoint.*` 直接 import 围栏）

> 实现笔记。对应 [docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md) PR7（T7.1-T7.5）。
>
> 状态：**已落地**，4 个 commits 提交到 `docs/multi-tenant-redesign`（PR7 起始 `1a6ccc9a..` 结束 `d8b13afc`）。

## 范围

把 `tests/test_harness_boundary.py` 的模式从"harness 不能 import app"扩到第二条 workspace-isolation 围栏：**只有显式 allowlist 里的文件才能直接 import `langgraph.checkpoint.*`**。其他任何位置必须穿 `app.gateway.deps.get_checkpointer` DI 或 `deerflow.runtime.checkpointer` 工厂——避免业务代码自己 new 个 saver 绕过 workspace 边界。

1. **`tests/boundary_allowlist.toml`** — 4 个当前合法 importer，每行配注释解释为什么允许：
   - `app/gateway/routers/threads.py`（thread 初始化时 `empty_checkpoint`）
   - `packages/harness/deerflow/runtime/checkpointer/async_provider.py` / `provider.py`（唯一 saver 工厂处）
   - `packages/harness/deerflow/runtime/runs/worker.py`（resumed run seed `empty_checkpoint`）
2. **`tests/test_workspace_boundary.py`** — AST 扫描 backend 所有 `*.py`（排除 `tests/`、`docs/`、build artefacts），命中 `langgraph.checkpoint.*` / `langgraph_checkpoint_postgres` / `langgraph_checkpoint_sqlite` 且不在 allowlist 即 fail，error message 直接列 `<path>:<line>  imports <module>` + 修复指引。
3. **TYPE_CHECKING 豁免** — 扫描器通过 parent-walk 检测 `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` 块（含嵌套），块内 import 不算违规。因此 `agents/factory.py` 里 `BaseCheckpointSaver` 作为类型注解的 type-only import **不进 allowlist**，更准确地反映 runtime boundary 语义。
4. **`tests/test_workspace_boundary_self.py`** — 9 个 self-test 防止扫描器静默空跑：用 `tmp_path` 合成 `.py` 片段喂给 `collect_runtime_checkpoint_imports`，覆盖 from-import / bare import / 第三方包 / TYPE_CHECKING（Name 形 + Attribute 形 + 嵌套）/ 字符串字面量 / 语法错误 / 不相关 import。
5. **`backend/CLAUDE.md` Boundary check 段** 加两行说明新增的两个 test 文件 + allowlist 维护契约（"新 importer 同 PR 加 allowlist"）。

**不在范围**：
- 运行时 import-hook 拦截（plan 明确不做——静态 AST 扫描足够 + 不引入 runtime 开销）。
- 把 plan 草稿里的 `thread_runs.py` / `gateway/app.py` 列入 allowlist——grep 实际证实它们走 `get_checkpointer` DI，**不直接 import**，列入会假阳放水。
- `app.* → deerflow.runtime.*` 的反向间接环检查（plan "一句话状态" 提到的扩展，留 Stage 1 follow-up）。

## Tasks 完成清单

| Task | Commit | 关键改动 |
|---|---|---|
| **T7.1** | `1a6ccc9a` | `tests/boundary_allowlist.toml` 4 entry + 每行注释解释合法性 |
| **T7.2** | `ba30d140` | `tests/test_workspace_boundary.py` AST scanner + TYPE_CHECKING parent-walk + allowlist 加载 |
| **T7.3** | `d0f18770` | `tests/test_workspace_boundary_self.py` 9 个 self-test |
| **T7.4** | _no commit_ | 临时在 `app/gateway/routers/feedback.py:13` 加 `from langgraph.checkpoint.postgres import AsyncPostgresSaver` → scanner 红灯且 line 号正确 → revert。无测试改动，按 plan "仅 commit 测试自身完善" 原则不留 empty commit |
| **T7.5** | `d8b13afc` | `backend/CLAUDE.md` Boundary check 段加 workspace boundary + self-test 两行 |

## 验收

- [x] **scanner 红→绿循环**：empty allowlist → 14 violations across 4 files（threads / async_provider / provider / worker，TYPE_CHECKING-only 的 factory.py 正确不在内）；填入 4 entry → PASS
- [x] **scanner self-test 9 个全过**（防静默空跑）
- [x] **T7.4 反注入实验**：往 `feedback.py:13` 加一行违规 import → `pytest tests/test_workspace_boundary.py` 单条 fail，error 精准指 `app/gateway/routers/feedback.py:13  imports langgraph.checkpoint.postgres`；revert 后立即返绿
- [x] **全套 `make test` 3241 passed + 31 skipped + 18 caplog flake**（PR6 末 3214 + 30 + 17；+27 passed / +1 skip / +1 flake — passed delta 包含 PR7 新增 10 个测试以及环境差异导致的 17 个之前 flake 这次稳过，flake 列表形态与 STATUS.zh-CN.md 既有 17 项 + PR6 引入的 `test_path_migration_pending_warning` 一致，与 PR7 改动无关）
- [x] **CI workflow 接入**：扫描器是普通 pytest，已被 `.github/workflows/backend-unit-tests.yml` 全套 run 覆盖；无需新 workflow

## 文件结构

**新增**：
- `backend/tests/test_workspace_boundary.py` — AST 扫描器（127 行）
- `backend/tests/test_workspace_boundary_self.py` — 扫描器 self-test（93 行）
- `backend/tests/boundary_allowlist.toml` — 4 个合法 importer + 每行注释（28 行）
- `docs/multi-tenant-redesign/03-impl/pr7-ci-boundary-scan.zh-CN.md` — 本文件

**修改**：
- `backend/CLAUDE.md` — Boundary check 段 +2 行

**未改动**（验证后无需触碰）：
- `app/gateway/routers/threads.py`、`runtime/checkpointer/async_provider.py`、`runtime/checkpointer/provider.py`、`runtime/runs/worker.py` — 当前合法 importer，已被 allowlist 显式覆盖
- `agents/factory.py` — TYPE_CHECKING-only import，扫描器自动豁免

## 关键设计决策

1. **TYPE_CHECKING 豁免 vs allowlist 收纳**：`agents/factory.py` 把 `BaseCheckpointSaver` 当类型注解用。两种实现方式都能让扫描通过——加 allowlist / 加 TYPE_CHECKING 检测。选后者：boundary 的真实语义是 "runtime path 不要构造 saver"，type-only import 不进 runtime，本就不构成违规，把它列 allowlist 是给后人一个错误信号（"看，这文件可以直接 import"）。parent-walk 实现 `_inside_type_checking` 多 15 行代码，换 1 条更准的语义边界。
2. **Allowlist 形态：toml list of paths，而非正则 / 模块通配**：4 个 entry，未来增长慢，精确路径列表最易审计 / diff。toml 配 frontmatter 注释每条合法性来源；任何 PR 加新 entry 都被 review 看到。
3. **扫描器作为 pytest 而非独立 CI step**：复用现有 `backend-unit-tests.yml`，零 workflow 改动，本地 `make test` 也覆盖。如果未来想跑成独立 step（更快失败），切出来代价低。
4. **不引入 deerflow / app 边界以外的更细规则**：plan 顶部"一句话状态"提到 `app.*` 反向再 import `deerflow.runtime.*` 的间接环检查——这条作为 Stage 1 follow-up 保留，PR7 范围里只做 langgraph.checkpoint.* 这一条单点围栏，保持每个 PR 单一关注。
5. **Plan 草稿 allowlist 修正**：plan 列了 `thread_runs.py` / `gateway/app.py`，但 grep 实际状态两者都通过 `get_checkpointer` DI 拿 saver，不直接 import；同时漏掉了 `provider.py`（同步版 checkpointer）和 `worker.py`（run resume seed）。impl 按 ground truth 取 4 个真实 importer——这是 plan 设计阶段无法预知的代码事实，应该以代码为准。

## Live smoke 命令（用户跟进）

PR7 是纯静态检查，落地即生效，无 runtime 行为变化，**无需 live smoke**。`make test` 全套绿就完成。
