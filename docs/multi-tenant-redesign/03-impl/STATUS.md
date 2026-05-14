# Stage 0 进度面板

> **每完成 1 个 PR 后必更新**。本文是 Stage 0 唯一的"现在到哪了"权威来源——其它文件（plan、ADR、各 PR impl note）都是静态的，不反映执行进度。
>
> 上次更新：2026-05-14，PR7 merge 进 docs branch 后

## 一句话状态

PR1 + PR2 + PR3 + PR4 + PR5 + PR6 + **PR7** 已 merge。**PR7 (2026-05-14)** 落地：第二条 boundary 围栏——`tests/test_workspace_boundary.py` AST 静态扫描 backend 全树，命中 `langgraph.checkpoint.*` / `langgraph_checkpoint_postgres` / `langgraph_checkpoint_sqlite` 且不在 `tests/boundary_allowlist.toml`（4 个合法 importer：`threads.py` + `async_provider.py` + `provider.py` + `runs/worker.py`）即 fail；TYPE_CHECKING-only import 自动豁免（parent-walk 检测 `if TYPE_CHECKING:` 嵌套）；9 个 self-test 防止扫描器静默空跑；T7.4 反注入实验把违规一行加进 `feedback.py:13` → scanner 精准红灯 → revert 后即绿。**3241 passed + 31 skipped + 18 caplog flake**（PR6 末 3214 + 30 + 17；+27 passed / +1 skip / +1 flake，PR7 新增 10 个 test，flake delta 与 PR7 改动无关）。Stage 0 仅剩 **PR8（service_accounts / api_keys / external_users schema）**。

## 8 PR 状态表

| PR | 状态 | Commits | 分支 / 落点 | impl note |
|---|---|---|---|---|
| **PR0** | ✅ merged | 1 | `a74b88a4` on docs branch | — |
| **PR1** | ✅ merged | 8 (T1.1-T1.10) | merged into docs branch (`fab85b14..85a14f4c`) | [pr1-postgres-setup.md](./pr1-postgres-setup.md) |
| **PR2** | ✅ merged | 8 (T2.1-T2.10) | merged into docs branch (`404135a1..1112a197`) | [pr2-postgres-default.md](./pr2-postgres-default.md) |
| **PR3** | ✅ merged | 7 (T3.1-T3.10) | merged into docs branch (`f63089ae..dda82640`) | [pr3-workspaces.md](./pr3-workspaces.md) |
| **PR4** | ✅ merged | 14 (T4.1-T4.14) | merged into docs branch (`d98498b7..5c7753c0`) | [pr4-auth-workspace.md](./pr4-auth-workspace.md) |
| **PR5** | ✅ merged | 11 (T5.1-T5.10 + T5.12) | merged into docs branch (`a7326978..30f2bd00`) | [pr5-business-workspace-id.md](./pr5-business-workspace-id.md) |
| **PR6** | ✅ merged | 13 (T5.11 + T6.1-T6.15) | merged into docs branch (`361e653d..87ea715c`) | [pr6-routes-paths-workspace.md](./pr6-routes-paths-workspace.md) |
| **PR7** | ✅ merged | 4 (T7.1-T7.3 + T7.5; T7.4 是反注入验证无代码改动) | merged into docs branch (`1a6ccc9a..d8b13afc`) | [pr7-ci-boundary-scan.md](./pr7-ci-boundary-scan.md) |
| **PR8** | 🟡 pending | 0 | — | — |

**测试基线**：**PR7 末 3241 passed + 31 skipped**（PR6 末 3214 + 30；+27 passed / +1 skip，PR7 新增 10 个 boundary 测试 + 17 个之前 flake 这次稳过的环境差）。PR5 末 3150 + 30；PR4 末 3136 + 26；PR3 末 3134 + 25；PR2 末 3087。**18 个 caplog 排序 flake 持续存在**（17 个 pre-existing + 1 PR6 引入 `test_path_migration_pending_warning::test_warns`，PR7 未引入新 flake）→ isolate 跑全 PASS，与 stage 无关；集中清理仍推迟到 follow-up。

## 用户必须跟进的事（live verification / 决策）

下列任务**只能用户做**，agent 没权限或没环境：

| 项 | 状态 | 谁做 | 怎么做 |
|---|---|---|---|
| 启动 Docker daemon 后实跑 PG smoke 测试（testcontainers 路径）| ⏳ | 用户 | `docker compose -f docker/docker-compose-dev.yaml up -d postgres && cd backend && PYTHONPATH=. uv run pytest -m postgres -v`。注：现在 RDS 已 live 验证（`make dev` 起 gateway + 9 张表已建），但 testcontainers ephemeral 路径仍未实跑过 |
| ~~远程 RDS 大版本对齐 testcontainers 镜像~~ | ✅ done 2026-05-11 | — | RDS = PostgreSQL 17.9（`make doctor` 确认），fixture 已调到 `postgres:17-alpine` |
| ~~Push docs branch 到 origin 跑 CI（含新 `backend-postgres-tests` workflow）~~ | ✅ done 2026-05-12 | — | 38 commits pushed（dce5e959..a592319e），SSH-over-443 绕代理；CI 用户确认绿 |
| ~~7 项 schema 不可逆 LOCK 决策团队 review~~ | ✅ done 2026-05-12 | — | 全 7 项 ✅ sign-off：id=String(36) / 命名=workspace_id+wid / slug `^[a-z0-9](-?[a-z0-9])*$` 3-32 / memberships 复合 PK / JWT 一次到位 / default_workspace_id / FK CASCADE。详见 [workspace-schema-design §5](../01-redesign/workspace-schema-design.zh-CN.md#5-不可逆决策清单)。PR4 可开工 |
| 远程 RDS 密码轮换 | ⏳ | 用户 | 之前在聊天里给过明文密码——建议事后轮换 |

## 跳过 / 推迟的子任务（agent 当时主动跳的，需用户认可或后续补）

| 来源 | 跳过项 | 原因 | 建议 |
|---|---|---|---|
| PR1 T1.10 | 本地实跑 PG smoke 测试 | docker daemon 未起 | 用户跟进表第 1 项 |
| PR2 T2.7 | 写 setup_wizard 推荐 PG 的代码 | 已在 PR1 T1.8 完整实现（empty commit `745a33e0` 仅做 task tracking） | 无需跟进 |
| PR2 T2.8 | sqlite→pg 数据迁移工具 (`scripts/migrate_sqlite_to_postgres.py`) | plan 标 optional + Stage 0 没生产数据 | 如果出现"dev 用 SQLite 跑过一段、想保留数据迁 PG"的需求再补 |
| PR2 T2.9 | `backend/CLAUDE.md` Database 段更新 | README 已覆盖 80% 价值 | 写 PR3 时顺手补一句（agent 自己能做，不阻塞） |
| PR4 T4.14 | 真机 `make dev` smoke 注册流程 | agent 无法实际起 gateway daemon | 用户跟进；命令清单见 [pr4-auth-workspace.md "Live smoke 命令"](./pr4-auth-workspace.md#live-smoke-命令用户跟进) |
| PR4 follow-up | Regular user pre-PR4 backfill 脚本 | login 路径已 lazy backfill 覆盖；如果生产有大量预存 regular user，可补 batch 脚本 | 等真出现这个场景再写 |
| PR4 follow-up | 17 个 pre-existing caplog flake 集中清理 | 跨多个 test 文件的 propagation 问题，与 PR4/5/6 无关 | 单独 follow-up 处理 |
| ~~PR5 T5.11~~ | ~~ORM model.py `nullable=False` 翻转~~ | **PR6 已落** (commit `87ea715c`) | — |
| PR5 T5.12 真机 PG smoke | `alembic 0002 → backfill → 0003` 端到端 | agent 不能起 RDS 操作 | 用户跟进；命令清单见 [pr5-business-workspace-id.md "Live smoke 命令"](./pr5-business-workspace-id.md#live-smoke-命令用户跟进) |
| PR6 T6.15 真机迁移 smoke | `make migrate-paths --dry-run` → 真迁移 → lifespan warning 消失 → 双账户互访 404 | agent 起不了 dev 服务 | 用户跟进；命令清单见 [pr6-routes-paths-workspace.md "Live smoke 命令"](./pr6-routes-paths-workspace.md#live-smoke-命令用户跟进) |

## 即将遇到的开放问题（plan 末尾列的，下个 session 处理）

详见 plan [关键开放问题](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md#关键开放问题执行-session-第一件事处理)：

1. ~~**PR4 起会真正用到 alembic**——首个 revision 之前要不要加 baseline？~~ **✅ T4.1 (2026-05-12) 已验证**：`versions/` 空 + `alembic heads`/`history` 都空输出 → 0001 直接当首个 revision、`down_revision = None`，**不需要 baseline**。`alembic_version` 表首次 `upgrade head` 时自动建；现有 create_all() 已建好的 schema 不冲突（0001 只 ADD COLUMN）。`doctor.py` 不需要加自动检测
2. ~~**`_ensure_admin_user(app)` 现状的孤立 thread 迁移逻辑**~~ **✅ T4 准备阶段 (2026-05-12) 已 grep**：`app.py:52` 当前只做两件事——(a) admin_count==0 时仅日志提示去 `/setup`，(b) admin 已存在时跑 LangGraph store 孤立 thread 迁移。**不自建 admin**。所以 T4.13 真实任务范围 = "admin 已存在但无 workspace"的 idempotent backfill 分支（plan 顶部"风险与缓解"段写的才对，task 措辞"建完 admin 顺带建"是误导，实际归 T4.8）
3. **PG 大版本对齐**（同上"用户必须跟进"#2）

## 下一步建议

**PR8（service_accounts / api_keys / external_users schema only）**——Stage 0 最后一项。仿 PR3 纯 schema 模式：3 张表 + ORM，**不接路径 / 不写仓储 / 不接 API key 认证**（全留 Stage 1）。依赖只到 PR3 的 workspaces 表，与 PR4-PR7 完全独立。

PR7 经验回顾：纯静态测试 PR，Inline 模式继续合适——5 个 task 单链条强耦合（先确定 allowlist 内容才能写扫描器，扫描器函数得是导出才能 self-test）。复用 PR4 同款"红→绿"严格 TDD：故意建空 allowlist 跑红、再填→绿；T7.4 反注入实验是对静态扫描器的"集成 smoke"，确认现实 backend 文件 + 真实 allowlist 过滤路径同时生效——这一步比 9 个 self-test 都更有说服力。

PR6 经验回顾：plan 推荐 Inline 模式是对的，路由 + 仓储 + Paths 强耦合每一步都依赖前一步的接口形态。如果走 subagent 派单会反复阻塞在跨 task 的 signature 协调上。

历史模式回顾：

| | Inline（PR1/PR2 模式） | Subagent-Driven |
|---|---|---|
| 速度 | 主 agent 推全流 | 主 agent 派单到 subagent，等结果 |
| 上下文消耗 | 多 | 少（任务上下文不污染主 agent） |
| 调试 | 错了主 agent 直接看 | 错了要找 subagent log |
| 适用 | PR1/PR2 这种"一个 PR 内有强耦合 reasoning"的 | PR3 这种"10 个机械任务，每个独立"的 |

## 维护规则

**完成一个 PR 后**（merge 进 docs branch 那刻）必更新本文件：

1. 把 PR 的状态行从 🟡 pending 改 ✅ merged
2. 填 commits 数 + commit hash 范围 + impl note 链接
3. 把 PR 跳过/推迟的子任务移到"跳过 / 推迟的子任务"表
4. 把 PR 引入的开放问题加到"即将遇到的开放问题"
5. 更新"上次更新"时间 + "一句话状态"

**进入新 session 第一件事**：读本文件 + 读"即将遇到的开放问题"段 + 验证文件提到的代码锚点是否还在（防 plan 与代码漂移）。

## 阅读路径

- **新加入项目想立即了解状态** → 本文件
- **写代码前要看 plan** → [Stage 0 master plan](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md)
- **理解某个具体 PR 怎么落的** → 03-impl/prN-*.md
- **需要 Stage 0 之外的全局理解** → [README.zh-CN.md](../README.zh-CN.md)（多租户改造汇总索引）
