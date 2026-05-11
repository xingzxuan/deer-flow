# Stage 0 进度面板

> **每完成 1 个 PR 后必更新**。本文是 Stage 0 唯一的"现在到哪了"权威来源——其它文件（plan、ADR、各 PR impl note）都是静态的，不反映执行进度。
>
> 上次更新：2026-05-11，PR2 merge 进 docs branch 后

## 一句话状态

PR1 + PR2 已 merge 进 `docs/multi-tenant-redesign`（领先 origin 25 commits）。**下一个：PR3（workspaces + workspace_memberships 表 + 仓储）**，plan 推荐 Subagent-Driven 模式。

## 8 PR 状态表

| PR | 状态 | Commits | 分支 / 落点 | impl note |
|---|---|---|---|---|
| **PR0** | ✅ merged | 1 | `a74b88a4` on docs branch | — |
| **PR1** | ✅ merged | 8 (T1.1-T1.10) | merged into docs branch (`fab85b14..85a14f4c`) | [pr1-postgres-setup.md](./pr1-postgres-setup.md) |
| **PR2** | ✅ merged | 8 (T2.1-T2.10) | merged into docs branch (`404135a1..1112a197`) | [pr2-postgres-default.md](./pr2-postgres-default.md) |
| **PR3** | 🟡 pending | 0 | （会起 `feat/stage-0-pr3-workspaces`） | — |
| **PR4** | 🟡 pending | 0 | — | — |
| **PR5** | 🟡 pending | 0 | — | — |
| **PR6** | 🟡 pending | 0 | — | — |
| **PR7** | 🟡 pending | 0 | — | — |
| **PR8** | 🟡 pending | 0 | — | — |

**测试基线**：3087 passed + 23 skipped + 0 failed（PR2 末测试），PR1 之前是 3086 passed + 18 skipped。期间 1 个偶发 flaky `tests/test_client_live.py::TestLiveStreaming::test_stream_ai_content_nonempty`（单跑 PASS，env 相关，与本 Stage 无关）。

## 用户必须跟进的事（live verification / 决策）

下列任务**只能用户做**，agent 没权限或没环境：

| 项 | 状态 | 谁做 | 怎么做 |
|---|---|---|---|
| 启动 Docker daemon 后实跑 PG smoke 测试 | ⏳ | 用户 | `docker compose -f docker/docker-compose-dev.yaml up -d postgres && cd backend && PYTHONPATH=. uv run pytest -m postgres -v` |
| ~~远程 RDS 大版本对齐 testcontainers 镜像~~ | ✅ done 2026-05-11 | — | RDS = PostgreSQL 17.9（`make doctor` 确认），fixture 已调到 `postgres:17-alpine` |
| Push docs branch 到 origin 跑 CI（含新 `backend-postgres-tests` workflow） | ⏳ | 用户 | `git push origin docs/multi-tenant-redesign` 后看 GitHub Actions |
| 7 项 schema 不可逆 LOCK 决策团队 review | ⏳ | 用户 + 团队 | 见 [workspace-schema-design §5](../01-redesign/workspace-schema-design.zh-CN.md#5-不可逆决策清单)；PR3 合入前必须签字 |
| 远程 RDS 密码轮换 | ⏳ | 用户 | 之前在聊天里给过明文密码——建议事后轮换 |

## 跳过 / 推迟的子任务（agent 当时主动跳的，需用户认可或后续补）

| 来源 | 跳过项 | 原因 | 建议 |
|---|---|---|---|
| PR1 T1.10 | 本地实跑 PG smoke 测试 | docker daemon 未起 | 用户跟进表第 1 项 |
| PR2 T2.7 | 写 setup_wizard 推荐 PG 的代码 | 已在 PR1 T1.8 完整实现（empty commit `745a33e0` 仅做 task tracking） | 无需跟进 |
| PR2 T2.8 | sqlite→pg 数据迁移工具 (`scripts/migrate_sqlite_to_postgres.py`) | plan 标 optional + Stage 0 没生产数据 | 如果出现"dev 用 SQLite 跑过一段、想保留数据迁 PG"的需求再补 |
| PR2 T2.9 | `backend/CLAUDE.md` Database 段更新 | README 已覆盖 80% 价值 | 写 PR3 时顺手补一句（agent 自己能做，不阻塞） |

## 即将遇到的开放问题（plan 末尾列的，下个 session 处理）

详见 plan [关键开放问题](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md#关键开放问题执行-session-第一件事处理)：

1. **PR4 起会真正用到 alembic**——首个 revision 之前要不要加 baseline？plan agent 建议 0001 直接当首个 revision，`down_revision = None`。验证：跑 `cd backend && PYTHONPATH=. uv run alembic current` 看默认行为
2. **`_ensure_admin_user(app)` 现状的孤立 thread 迁移逻辑**——PR4 / PR5 假设它能扩；先 grep `app.py` 看现状
3. **PG 大版本对齐**（同上"用户必须跟进"#2）

## 下一步建议

按 plan 推荐的 PR3 = Subagent-Driven 模式。如果继续 Inline（像 PR1/PR2 那样）也可以，trade-off：

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
