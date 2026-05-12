# PR3 · workspaces + workspace_memberships 表 + 仓储

> 实现笔记。对应 [docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md) PR3（T3.1-T3.10）。
>
> 状态：**已落地**，9 commits 提交在 branch `feat/stage-0-pr3-workspaces`。

## 范围

新建 `workspaces` + `workspace_memberships` 两张表 + 仓储 + ContextVar + 47 个单测。这是 Stage 0 schema 基础设施的**核心**——后续 PR4 注册改造、PR5 ALTER 现有表、PR6 入口校验都依赖这层。

不在范围（后续 PR）：
- 注册流程改造 / JWT 加 wid+role（PR4）
- alembic revision（PR4 起，因 ALTER 现有表才需要）
- workspace_id 列加到 threads_meta 等（PR5）
- 入口路由 `(wid, tid)` 校验（PR6）

## 验收

- [x] 47 个新单测全过（16 workspace_context + 23 workspace_repo + 8 membership_repo）
- [x] 全套 `pytest tests/` 3134 passed + 25 skipped + 0 failed（PR2 末 3087 + 47 新；+2 PG-only skip）
- [x] partial unique on owner 在 SQLite ephemeral DB（test_cannot_add_second_owner）+ 远程 RDS PG 17.9（live 验证）双驱动通过
- [x] CASCADE 在 SQLite 上验过（test_delete_cascades_to_memberships / test_cascade_delete_user_removes_memberships）
- [x] RDS 上 `workspaces` + `workspace_memberships` 表 + 索引（含 partial unique）live create_all 出来：
  ```
  idx_one_owner_per_workspace
    UNIQUE INDEX ... (workspace_id) WHERE ((role)::text = 'owner'::text)
  idx_workspace_memberships_user (user_id, workspace_id)
  workspace_memberships_pkey UNIQUE (workspace_id, user_id)
  ```
- [ ] CI workflow 上跑 @pytest.mark.postgres → 实跑 partial-unique PG twin — **待 PR push 后 GH Actions 验**

## 关键决策（LOCK 项 — 与 plan §横切 + workspace-schema-design §5 对齐）

| 项 | 选择 | 理由 |
|---|---|---|
| 表名 | `workspaces` + `workspace_memberships` | workspace-schema-design 锁定 |
| ID 类型 | `String(36)` UUID v4 字符串 | 与 `users.id` 类型对齐，跨 SQLite/PG 可移植 |
| Membership PK | 复合 `(workspace_id, user_id)` | 工业实践默认；surrogate id 没必要 |
| Role 类型 | `String(16)` + 应用层校验 | 不用 PG enum（ALTER TYPE 加值痛苦）；Stage 0 写 owner，Stage 2 开 admin/member |
| Partial unique on owner | `Index(..., sqlite_where=text(...), postgresql_where=text(...))` 双 where 并存 | SQLite + PG 都支持；T3.8 PG twin 验语义一致 |
| Owner FK 删除策略 | `ON DELETE RESTRICT` | 删 user 时阻拦（必须先转让 owner）；transfer 流程放 PR4 |
| Membership FK 删除策略 | `ON DELETE CASCADE` (workspace_id + user_id) | 删 workspace / 删 user 时自动清成员；逻辑上正确 |
| `invited_by` 删除策略 | `ON DELETE SET NULL` | 邀请人离开不影响成员；只清字段 |
| Slug 校验位置 | 应用层 `_validate_slug` + DB UNIQUE | DB 不知道 regex / 黑名单；应用层规则可演进 |
| Slug 黑名单 | 25 个保留字（admin/api/auth/...）frozenset 在 workspace/sql.py | workspace-schema-design §2.1 完整列表 |
| WorkspaceRepository.create | 不自动建 owner 成员 | 单一职责；注册流程在事务内同时写 workspace + membership |
| WorkspaceRepository.get | JOIN memberships 做 access 检查 | 默认安全；显式 `user_id=None` 跳过用于迁移 |
| WorkspaceRepository.get_by_slug | **不**做 access 检查 | path-based routing 用：先 slug→wid 再 route handler 查成员 |
| Owner transfer | 不在仓储层实现 | 需要两行事务原子 swap，应在 auth router 内带权限检查 |

## 跟进项（不在 PR3 范围）

- PR4 注册流程：`/auth/initialize` + `/auth/register` 用 `WorkspaceRepository.create + WorkspaceMembershipRepository.add(role='owner')` 在一个事务里建
- PR4 alembic 0001：加 `users.default_workspace_id` 列（也 FK 到 workspaces）
- PR4 `_auto_workspace_context` autouse fixture：现在 tests/test_workspace_repo 测试自己 set/reset 而不依赖 autouse；PR4 引入后简化
- backend/CLAUDE.md：等 PR4 完成时一并补 Database + Workspace 段（PR2 follow-up + 现在的 schema）

## 涉及文件

| 类别 | 文件 |
|---|---|
| ContextVar | `runtime/workspace_context.py` |
| ORM | `persistence/workspace/{__init__.py, model.py, sql.py}` |
| ORM | `persistence/workspace_membership/{__init__.py, model.py, sql.py}` |
| 注册 | `persistence/models/__init__.py`（加 2 个 import） |
| 测试 | `tests/test_workspace_context.py`（16 test） |
| 测试 | `tests/test_workspace_repo.py`（23 test） |
| 测试 | `tests/test_workspace_membership_repo.py`（8 test） |
| 测试 | `tests/test_workspace_partial_unique_postgres.py`（2 PG-only） |

## 提交记录

9 commits（含 T3.9 live verification 不发 commit）：

```
f63089ae feat(runtime): add workspace_context module + tests             T3.1 + T3.2
8323bf68 feat(persistence): add WorkspaceRow ORM model                   T3.3
d2d2d29c feat(persistence): add WorkspaceMembershipRow ORM model         T3.6
a40df035 feat(persistence): WorkspaceRepository + 23 unit tests          T3.4 + T3.5
36ffe271 feat(persistence): WorkspaceMembershipRepository + 8 unit tests T3.7
3313047f test(workspace): partial unique on owner — Postgres twin test  T3.8
```

T3.6 比 T3.4/T3.5 早提（plan 中 T3.4-T3.5 写仓储要 JOIN memberships，所以 T3.6 必须先）。

T3.9 是 live verification，不产 commit；记在本文档的"验收"段。
T3.10（impl doc）即本文件。
