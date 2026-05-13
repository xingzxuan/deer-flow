# Stage 0 进度面板

> **每完成 1 个 PR 后必更新**。本文是 Stage 0 唯一的"现在到哪了"权威来源——其它文件（plan、ADR、各 PR impl note）都是静态的，不反映执行进度。
>
> 上次更新：2026-05-13，PR6 merge 进 docs branch 后

## 一句话状态

PR1 + PR2 + PR3 + PR4 + PR5 + **PR6** 已 merge。**PR6 (2026-05-13)** 落地：4 个业务仓储 30+ 方法的 `workspace_id` 哨兵 + WHERE；`check_access` 升级三参数 (`thread_id, user_id, workspace_id`)；`@require_permission` 装饰器接入 `get_effective_workspace_id()`，跨 workspace **404 not 403**；`Paths` 切 workspace 维度（`{base}/workspaces/{wid}/threads/{tid}/...` + per-user state 嵌套）；`ThreadDataMiddleware` 切 workspace；`scripts/migrate_paths_to_workspace.py` 文件迁移（带 dry-run + 冲突分流）；lifespan 探测残留 `users/` 时 WARNING 引导跑 `make migrate-paths`；**T5.11 ORM `nullable=False` 一并翻**（PR5 推迟项就位）。**3214 passed + 30 skipped + 17 caplog flake**（PR5 末 3150 + 30 + 16；+64 测试，+1 flake——新 flake `test_path_migration_pending_warning::test_warns`，solo 跑 PASS）。**下一个：PR7（CI boundary 静态扫描）**。

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
| **PR7** | 🟡 pending | 0 | — | — |
| **PR8** | 🟡 pending | 0 | — | — |

**测试基线**：**PR6 末 3214 passed + 30 skipped**（PR5 末 3150 + 30；+64 PR6 新测试，覆盖 thread_meta workspace_id 过滤、Run/Feedback/RunEvent 同款、require_permission probes、跨 workspace 404 e2e、Paths workspace 形态、ThreadDataMiddleware workspace、文件迁移脚本、lifespan warning）。PR4 末 3136 + 26；PR3 末 3134 + 25；PR2 末 3087。**17 个 caplog 排序 flake 持续存在**（16 个 pre-existing + 1 新增 `test_path_migration_pending_warning::test_warns`）→ isolate 跑全 PASS，与 stage 无关；集中清理仍推迟到 follow-up。

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

**PR7（CI boundary 静态扫描）**。Stage 0 收尾的最后一项；plan 描述："静态扫描 ban `deerflow.* → app.*` 反向 import"。PR1-PR6 的代码已经维持这条边界，PR7 是把单测 `tests/test_harness_boundary.py` 上的检查升级为 grep 级 / CI workflow 级扫描，加更细粒度的禁止规则（如禁止 `app.*` 反向再 import 回 `deerflow.runtime.*` 等不应有的间接环）。**PR8 (service_accounts / api_keys / external_users schema) 可并行**，依赖只到 PR3 的 workspaces 表。

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
