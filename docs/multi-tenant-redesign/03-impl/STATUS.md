# Stage 0 进度面板

> **每完成 1 个 PR 后必更新**。本文是 Stage 0 唯一的"现在到哪了"权威来源——其它文件（plan、ADR、各 PR impl note）都是静态的，不反映执行进度。
>
> 上次更新：2026-06-28——`multi_tenant.py` live smoke 跑通（2026-06-27 用户运行，用户确认 PASS），退出门「手工 smoke」项关闭。分支已由 `docs/multi-tenant-redesign` 改名为 `feat/multi-tenant`（origin + kcim 均同步）。

## 一句话状态

**PR1-PR8 全部已 merge——Stage 0 工程层面收尾。** **PR8 (2026-05-14)** 落地：3 张新表 schema-only 为 Stage 1 headless API 准备底座——`service_accounts`（workspace-scoped 非人身份，`identity_mode` 三态：`collapsed` / `external_passthrough` / `both`，`created_by` FK RESTRICT）/ `api_keys`（service_account 凭证，`key_prefix` 全局 UNIQUE + 双驱动部分索引 `idx_api_keys_active` WHERE `revoked_at IS NULL`，`scopes` 用 String(1024) 不用 PG `text[]` 保 SQLite 兼容）/ `external_users`（passthrough 终端身份，复合 UNIQUE `(service_account_id, external_id)`，`workspace_id` 冗余存储加速聚合）。FK 行为：workspace/SA delete CASCADE、creator user delete RESTRICT。9 新单测（3 service_account + 3 api_key + 2 external_user + 1 反向 metadata registration）。**3250 passed + 31 skipped + 18 caplog flake**（PR7 末 3241 + 31 + 18；+9 passed，flake 数 0 增）。Stage 0 工程层面**仅剩用户跟进的 live smoke**（见下）；业务层面看 "Stage 0 退出 Go/No-Go"。

## Live smoke 结果（2026-06-27 `multi_tenant.py`）

**用户运行 `apps/examples/http-chat/multi_tenant.py` 打到运行中的 Gateway，确认 verdict = PASS。** 这是退出门「手工 smoke」项的实跑验证，且比清单要求更强。脚本（代码层面核实）实际断言的不变量：

- **注册 → workspace 自动建**：每租户走 `POST /api/v1/auth/register` 新建用户，`GET /api/v1/auth/me` 返回 `default_workspace_id`（workspace 随注册自动创建）。
- **真并发**：N 个租户各自独立 `requests.Session`（独立 cookie）放进线程池同时跑，输出「对话时间窗重叠」证据证明是真并发而非串行。
- **多轮上下文保持**：每租户复用同一 thread 跑 N 轮链式对话（T1=a×b，之后每轮 +d，步长 d 每租户不同），逐轮校验上一轮结果，验证并发下各租户上下文互不串扰。
- **双向隔离**：① `POST /api/threads/search` 只返回自己的 thread（不泄漏他人）；② 直接 `GET /api/threads/{他人 thread_id}` 一律返回 **404**（不是 403）。

> 未核实数字（用户选择不编造）：本次运行的具体租户数 `DF_TENANTS`、轮数 `DF_TURNS`、逐轮通过数。如需精确记录，贴终端输出（含 `>>> PASS ✅` 行）后回填。
>
> **已知覆盖缺口**：退出门 smoke 文字里的「JWT 含 wid」脚本未显式解码 token 断言 wid claim——由隔离端到端工作间接覆盖，非字面级验证。

**本次关闭的项**：退出门「手工 smoke：注册 → workspace 自动建 → 创建 thread → 跨 workspace 互调 404」✅；PR4 T4.14 真机注册 smoke ✅；PR6 T6.15 的「双账户互访 404」隔离部分 ✅（该 task 的文件迁移部分 `make migrate-paths` 仍 ⏳，见跳过表）。

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
| **PR8** | ✅ merged | 5 (T8.1 + T8.2/T8.3 合并 + T8.4 + T8.5 + T8.6) | merged into docs branch (`1fb07e48..f803f393`) | [pr8-headless-api-schema.md](./pr8-headless-api-schema.md) |

**测试基线**：**PR8 末 3250 passed + 31 skipped**（PR7 末 3241 + 31；+9 passed，PR8 新增 3 + 3 + 2 + 1 = 9 个 schema 测试）。PR6 末 3214 + 30；PR5 末 3150 + 30；PR4 末 3136 + 26；PR3 末 3134 + 25；PR2 末 3087。**18 个 caplog 排序 flake 持续存在**（17 个 pre-existing + 1 PR6 引入，PR7/PR8 均未引入新 flake）→ isolate 跑全 PASS，与 stage 无关；集中清理仍推迟到 follow-up。

### Stage 0 整体测试增长
PR1 起到 PR8 末，从既有 ~3087 增到 3250 passed（+163 测试，覆盖：PG fixture / sqlite→pg 默认切换 / workspaces + memberships / auth + JWT + register + workspace 自建 / alembic + backfill / 业务表 workspace_id 哨兵 + cross-workspace 404 e2e + Paths workspace + 文件迁移 / langgraph.checkpoint boundary 围栏 / service_accounts + api_keys + external_users schema）。plan 测试规模预估栏目原本估 ~70 新增，实际 ~163——PR4/PR5/PR6 都比预估多 2-3x，主要是 cross-workspace 隔离的 boundary e2e 比 plan 估的更稠密。

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
| PR4 T4.14 | 真机 `make dev` smoke 注册流程 | agent 无法实际起 gateway daemon | ✅ done 2026-06-27——`multi_tenant.py` PASS 覆盖（注册 → workspace 自建 → me 返回 wid） |
| PR4 follow-up | Regular user pre-PR4 backfill 脚本 | login 路径已 lazy backfill 覆盖；如果生产有大量预存 regular user，可补 batch 脚本 | 等真出现这个场景再写 |
| PR4 follow-up | 17 个 pre-existing caplog flake 集中清理 | 跨多个 test 文件的 propagation 问题，与 PR4/5/6 无关 | 单独 follow-up 处理 |
| ~~PR5 T5.11~~ | ~~ORM model.py `nullable=False` 翻转~~ | **PR6 已落** (commit `87ea715c`) | — |
| PR5 T5.12 真机 PG smoke | `alembic 0002 → backfill → 0003` 端到端 | agent 不能起 RDS 操作 | 用户跟进；命令清单见 [pr5-business-workspace-id.md "Live smoke 命令"](./pr5-business-workspace-id.md#live-smoke-命令用户跟进) |
| PR6 T6.15 真机迁移 smoke | `make migrate-paths --dry-run` → 真迁移 → lifespan warning 消失 → 双账户互访 404 | agent 起不了 dev 服务 | 🟡 部分 done——「双账户互访 404」✅ 由 `multi_tenant.py`（2026-06-27 PASS）覆盖；文件迁移 `make migrate-paths` 部分仍 ⏳。命令清单见 [pr6-routes-paths-workspace.md "Live smoke 命令"](./pr6-routes-paths-workspace.md#live-smoke-命令用户跟进) |
| PR8 RDS 三张表存在 | `psql "$DATABASE_URL" -c "\dt service_accounts api_keys external_users"` 看 3 行；`\d+ api_keys` 看 `idx_api_keys_active ... WHERE revoked_at IS NULL` | agent 没 RDS 凭证 | 用户跟进；命令清单见 [pr8-headless-api-schema.md "Live smoke 命令"](./pr8-headless-api-schema.md#live-smoke-命令用户跟进) |

## 即将遇到的开放问题（plan 末尾列的，下个 session 处理）

详见 plan [关键开放问题](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md#关键开放问题执行-session-第一件事处理)：

1. ~~**PR4 起会真正用到 alembic**——首个 revision 之前要不要加 baseline？~~ **✅ T4.1 (2026-05-12) 已验证**：`versions/` 空 + `alembic heads`/`history` 都空输出 → 0001 直接当首个 revision、`down_revision = None`，**不需要 baseline**。`alembic_version` 表首次 `upgrade head` 时自动建；现有 create_all() 已建好的 schema 不冲突（0001 只 ADD COLUMN）。`doctor.py` 不需要加自动检测
2. ~~**`_ensure_admin_user(app)` 现状的孤立 thread 迁移逻辑**~~ **✅ T4 准备阶段 (2026-05-12) 已 grep**：`app.py:52` 当前只做两件事——(a) admin_count==0 时仅日志提示去 `/setup`，(b) admin 已存在时跑 LangGraph store 孤立 thread 迁移。**不自建 admin**。所以 T4.13 真实任务范围 = "admin 已存在但无 workspace"的 idempotent backfill 分支（plan 顶部"风险与缓解"段写的才对，task 措辞"建完 admin 顺带建"是误导，实际归 T4.8）
3. **PG 大版本对齐**（同上"用户必须跟进"#2）

## 下一步建议

**Stage 0 工程层面 8 个 PR 全部 merge，agent 这一侧的代码工作收尾。** 剩下都是**用户必须做的 live verification**：

1. **PR8 RDS 表存在** — `psql "$DATABASE_URL" -c "\dt service_accounts api_keys external_users"`
2. **PR6 真机文件迁移** — 起 dev 服务、跑 `make migrate-paths --dry-run`、确认 lifespan warning 消失
3. **PR5 RDS alembic 0002→backfill→0003** — 端到端验证业务表 workspace_id 列
4. **PR1 testcontainers PG smoke** — docker daemon 起来后跑 `pytest -m postgres -v`
5. **远程 RDS 密码轮换**（之前在聊天里给过明文）
6. **Push docs branch** 跑 GitHub CI（已 push，监 [backend-postgres-tests workflow](../../../.github/workflows/backend-postgres-tests.yml) 在 PG matrix 全绿）

工程门 [Stage 0 退出 Go/No-Go](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md#stage-0-退出-gono-go来自-phased-rollout-by-scale) 已基本满足：8 PR 全合 / 新增 ~163 测试全过 / CI 绿（待 push 后确认）/ 7 项不可逆 LOCK 已 sign-off。**业务门**（"第一个付费意向客户"）等业务进展；生产稳定运行 ≥ 2 周也属业务时序。

Stage 1 可启动的方向（plan 没排，但已具备底座）：
- **headless API 鉴权层**接 PR8 三张表（API key middleware / token 生成与 sha256 / `@require_permission` scope 升级 / Pattern A vs B 路由分流）
- **frontend workspace picker / switching UI**
- **platform admin 管理 workspace 的 CLI / UI**（plan self-review 标记的 gap）
- **17 个 pre-existing caplog flake 集中清理**（一直推迟）

---

PR8 经验回顾：纯 schema PR，**Inline + 严格 TDD（红→绿）** 跑得很顺。6 个 task 单链条但每个 task 互相独立——SA / api_key / external_user 三张表之间只通过 FK 关联，没有跨 task signature 协调。每个表都按"先建模型 → 写 insert smoke 红→绿 → 加 cascade 测试 → 加 constraint 测试"四步走，3 张表 25 分钟内全落。T8.6 反向 metadata registration 测试是踩过坑后的肌肉记忆——历史上多次"模型类写了但 persistence/models/__init__.py 漏 import → create_all 不建表 → 上线 SELECT 时炸"，T8.6 把这条 invariant 永久锁住。

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
