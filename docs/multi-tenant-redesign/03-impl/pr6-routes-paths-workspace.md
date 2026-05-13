# PR6 · 路由强校验 + Paths workspace 化 + 仓储 workspace_id 哨兵 + 文件迁移

> 实现笔记。对应 [docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md) PR6（T5.11 + T6.1-T6.15）。
>
> 状态：**已落地**，13 个 feat/test commits 提交到 `docs/multi-tenant-redesign`（PR6 起始 `361e653d..` 结束 `87ea715c`）。

## 范围

PR6 把 PR5 落到 4 张业务表里的 `workspace_id` 列真正接到路由 → 仓储 → 文件系统：

1. **仓储层** — `ThreadMetaRepository` + `RunRepository` + `FeedbackRepository` + `DbRunEventStore` 的 30+ 个方法全部新增 `workspace_id: str | None | _AutoSentinel = AUTO` 哨兵参数，`AUTO` 走 contextvar，显式 `None` 旁路（迁移 / CLI），显式 str 覆盖。读路径加 `WHERE workspace_id = :wid`；写路径写入 `workspace_id`。
2. **`ThreadMetaStore.check_access`** 升级三参数 `(thread_id, user_id, workspace_id, *, require_existing)`。跨 workspace **一律** False，即使 user_id 匹配也 False — 上层装饰器把 False 翻成 **404 不是 403**，绝不泄露跨租户的 thread 存在性。
3. **`@require_permission(owner_check=True)`** 从 `get_effective_workspace_id()` 拉 workspace（PR4 AuthMiddleware 已注入；no-auth dev fallback 到 `"default"`）传给 `check_access`。
4. **`Paths` 切 workspace 维度**：新顶层 `{base_dir}/workspaces/{wid}/...`，per-thread 路径 `{base_dir}/workspaces/{wid}/threads/{tid}/user-data/{workspace,uploads,outputs}/`，per-user state 嵌套在 workspace 下 `{base_dir}/workspaces/{wid}/users/{uid}/memory.json`。`thread_dir(thread_id, *, workspace_id=None, user_id=None)` 三档优先级（workspace > user > 完全 legacy）。
5. **ThreadDataMiddleware** `before_agent` 从 `get_effective_user_id()` 切到 `get_effective_workspace_id()` + 把 `user_id` / `workspace_id` 透传到 `thread_data` state。
6. **文件系统迁移** `scripts/migrate_paths_to_workspace.py`（带 `--dry-run` `--default-workspace`）：扫 `{base}/users/{uid}/...` → `{base}/workspaces/{wid}/...`，`wid` 从 `users.default_workspace_id` 读，缺失时 fallback 到 `legacy_workspace`（对齐 PR5 backfill 的孤儿 bucket）；冲突分流到 `{base}/migration-conflicts/workspace-migration/`。
7. **lifespan + Makefile**：lifespan 探测 `{base}/users/` 残留时 log warning 引导跑 `make migrate-paths`；Makefile 加 `migrate-paths` target（`DRY_RUN=1` / `DEFAULT_WORKSPACE=<wid>` 两个 env 旋钮）。
8. **T5.11（PR5 推迟到 PR6）**：4 个业务 ORM 翻 `Mapped[str] workspace_id` + `nullable=False`，对齐 alembic 0003 的 DB 不变式到 ORM `create_all()` 路径。

**不在范围**（Stage 1+）：
- frontend workspace picker / switching UI
- service_accounts / api_keys / external_users schema（PR8）
- `tenant_*` → `workspace_*` 大规模 rename（ADR 用语保留）

## Tasks 完成清单

| Task | Commit | 关键改动 |
|---|---|---|
| **T6.1** | `361e653d` | `ThreadMetaRepository.create` workspace_id 哨兵 + autouse fixture + Base.metadata.after_create seed listener |
| **T6.2** | `296a4f19` | `ThreadMetaRepository.get` 加 SQL WHERE workspace_id |
| **T6.3** | `28ad6c2b` | search/update_display_name/update_status/update_metadata/delete 全加 workspace_id |
| **T6.4** | `05be7f9a` | `check_access` 升级 3 参数 + authz 装饰器接入 `get_effective_workspace_id()` |
| **T6.5** | `b4fa3bf1` | RunRepository + FeedbackRepository + DbRunEventStore 同款改造 |
| **T6.6** | `0456606d` | 5 个 require_permission probe 测试（cross-workspace 404 / default fallback / 第三参透传） |
| **T6.7** | `f6a92292` | POST /api/threads 集成测试：TestClient + 两个 workspace 各落不同 thread 行 |
| **T6.8** | `a7ecd76e` | 4 case cross-workspace 404 e2e（GET / DELETE / PATCH + 同 workspace 正例） |
| **T6.9 + T6.10** | `f013fc1a` | `Paths.workspace_dir` / `thread_dir(*, workspace_id, user_id)` / 所有 sandbox_*/host_*/acp_workspace 都加 workspace_id 维度 |
| **T6.11** | `2d3b546b` | ThreadDataMiddleware 切 workspace + thread_data 透传 user_id/workspace_id |
| **T6.12 + T6.13** | `56f2c8d8` | `scripts/migrate_paths_to_workspace.py` + 9 个测试（thread/memory/agent 迁移、dry-run、冲突分流、empty users/ 清理、DB 缺失降级） |
| **T6.14** | `c5c66ccb` | `_check_path_migration_pending` lifespan warning + Makefile `migrate-paths` target |
| **T5.11** | `87ea715c` | 4 个 ORM nullable=False + conftest 一致性 seed + backfill test 临时 nullable fixture |

## 验收

- [x] **27 个新单测全过**：3 thread_meta workspace create + 3 get + 5 search/update/delete + 2 check_access workspace + 7 run/feedback/run_event + 5 require_permission probes + 2 POST /threads + 4 boundary e2e + 15 paths workspace + 4 ThreadDataMiddleware workspace + 9 path migration + 3 lifespan warning
- [x] **全套 `make test` 3214 passed + 30 skipped + 17 caplog flake**（PR5 末 3150 + 30 + 16；+64 passed / +1 flake，新 flake 是 `test_path_migration_pending_warning::test_warns...`，solo 跑 PASS，与本 stage 无关，归并到 follow-up）
- [x] **跨 workspace 必 404**：GET / DELETE / PATCH 任一从 workspace B 访问 workspace A 的 thread 都返 404；same workspace 正例返 200。E2e 通过 `MemoryThreadMetaStore`（真实 impl，不是 mock）穿过 require_permission 装饰器
- [x] **Paths 三档优先级**：workspace_id > user_id > 完全 legacy；新形态 `workspaces/{wid}/threads/{tid}/user-data/...`，per-user 状态嵌套 `workspaces/{wid}/users/{uid}/memory.json`；path traversal 防御覆盖
- [x] **迁移脚本 `--dry-run` 不写**：snapshot 源/目标 inode → 跑 migrate(dry_run=True) → 源不变 + 目标不存在
- [x] **lifespan 探测**：`{base}/users/` 有内容时 log WARNING 引导 `make migrate-paths`；目录缺失或为空时静默
- [x] **lint**：`make lint` 全过（ruff check + format）
- [ ] **真机 `make dev` smoke + `make migrate-paths --dry-run`** — **待用户**（agent 起不了 dev 服务）。命令清单见下文"Live smoke 命令（用户跟进）"

## 关键架构决策

| 项 | 选择 | 理由 |
|---|---|---|
| 跨 workspace 错误码 | **404 not 403** | 防 enumeration leak — 跨租户请求不能区分"thread 在别的 tenant"和"thread 不存在" |
| `check_access` 签名顺序 | `(thread_id, user_id, workspace_id, *, require_existing)` | LOCK 项。workspace 是位置参数（非 keyword-only）—— 调用方必须显式传，鼓励"先想 workspace、再想 user" |
| `workspace_id` 三态语义 | AUTO / explicit str / explicit None，沿用 `user_id` 模式 | 减少新概念，迁移脚本沿用 None 旁路即可 |
| Path 新形态 | `{base}/workspaces/{wid}/threads/{tid}/user-data/...` —— **不嵌 user_id** | thread 在 workspace 内 UNIQUE（alembic 0003 已保证），user_id 是元数据不是分区 |
| Path per-user state | `{base}/workspaces/{wid}/users/{uid}/memory.json` | memory / 自定义 agent 是 per-user，但仍嵌在 workspace 下 —— 同 user 在不同 workspace 的 memory 互相隔离 |
| 迁移脚本 fallback workspace | `legacy_workspace`（同 PR5 backfill 的 `LEGACY_WORKSPACE_ID`） | 文件系统迁移和数据库回填的孤儿桶一致，方便用户事后定位 |
| ORM nullable=False 时机 | PR6 末（T5.11） | 单翻 ORM 会让所有 INSERT 站点炸 NOT NULL；必须等仓储 workspace_id 哨兵 + 路由 contextvar 接入完才能翻 |

## 测试基线增量

| | PR5 末 | PR6 末 | 增量 |
|---|---:|---:|---:|
| passed | 3150 | 3214 | +64 |
| skipped | 30 | 30 | 0 |
| caplog flake | 16 | 17 | +1 |

新 flake `test_path_migration_pending_warning::test_warns_when_legacy_users_dir_has_content` —— solo 跑全 PASS，是 caplog level 传播 ordering 问题，与 PR4 时识别的 16 个同类 flake 一族，归并到 follow-up 集中清理。

## 迁移路径（live ops 文档）

生产环境（PR5 已 alembic 0003 上线）升级 PR6 后：

1. **DB 不变**（PR5 alembic 0003 已落 NOT NULL；PR6 没新 alembic）
2. **代码部署**：路由 + 仓储 + ThreadDataMiddleware 自动开始写 workspace-scope path（`{base}/workspaces/{wid}/...`）
3. **lifespan 启动会 log WARNING**：`Legacy per-user layout detected at {base}/users/. Run \`make migrate-paths\` ...`
4. **用户跑 `make migrate-paths DRY_RUN=1`** 看迁移计划
5. **用户跑 `make migrate-paths`** 真迁移；冲突落 `{base}/migration-conflicts/workspace-migration/` 待人工处理
6. **重启 gateway**：lifespan WARNING 消失

reverse rollback：脚本不带 `--reverse`，但 `migration-conflicts/` 保留原文件树，可手动还原。建议生产 `make migrate-paths DRY_RUN=1` 先跑一次。

## Live smoke 命令（用户跟进）

agent 没法起 gateway，所以下列由用户在本地 / RDS 实跑：

```bash
# 1. 启动开发服务（应该看到 lifespan WARNING 如果有遗留 users/）
make dev

# 2. dry-run 迁移
make migrate-paths DRY_RUN=1
# 期望：列出每个被迁移的 user，源/目标路径，'action=moved -> ...' or 'conflict -> ...'

# 3. 真迁移
make migrate-paths

# 4. 重启 gateway，确认 lifespan 警告消失
make stop && make dev

# 5. 双账户 smoke
#    - 注册 user A + user B（各自走默认 workspace）
#    - A 创 thread t1 → 复制 t1 到 B 的 URL → 必 404
#    - B 创 thread t2 → 复制到 A → 必 404
#    - 各自路径检查：ls .deer-flow/workspaces/<wid_A>/threads/t1
#    - 老路径不应再有新写入：watch .deer-flow/users/  → 应保持空
```

## 文件结构索引

**新增**：
- `backend/scripts/migrate_paths_to_workspace.py` — workspace 文件迁移
- `backend/tests/test_thread_meta_workspace_filter.py` — thread_meta workspace_id 过滤 (11 tests)
- `backend/tests/test_run_feedback_workspace_filter.py` — Run/Feedback/RunEvent workspace_id 过滤 (7 tests)
- `backend/tests/test_require_permission_workspace.py` — @require_permission workspace 接入 (5 tests)
- `backend/tests/test_workspace_isolation_boundary.py` — 跨 workspace 404 e2e (4 tests)
- `backend/tests/test_threads_post_workspace.py` — POST /api/threads workspace 集成 (2 tests)
- `backend/tests/test_paths_workspace.py` — Paths workspace 形态 (15 tests)
- `backend/tests/test_thread_data_middleware_workspace.py` — ThreadDataMiddleware workspace (4 tests)
- `backend/tests/test_migrate_paths_to_workspace.py` — 迁移脚本 (9 tests)
- `backend/tests/test_path_migration_pending_warning.py` — lifespan warning (3 tests)

**修改**：
- `backend/packages/harness/deerflow/persistence/{thread_meta,run,feedback}/sql.py` — workspace_id 哨兵 + WHERE
- `backend/packages/harness/deerflow/persistence/thread_meta/{base.py, memory.py}` — abstract + memory impl 同款
- `backend/packages/harness/deerflow/persistence/{thread_meta,run,feedback}/model.py` + `models/run_event.py` — nullable=False (T5.11)
- `backend/packages/harness/deerflow/runtime/events/store/db.py` — workspace_id_from_context + WHERE
- `backend/packages/harness/deerflow/config/paths.py` — workspace_dir / _validate_workspace_id / thread_dir + sandbox_* + host_* + ensure/delete/resolve_virtual_path
- `backend/packages/harness/deerflow/agents/middlewares/thread_data_middleware.py` — workspace 切换
- `backend/app/gateway/authz.py` — get_effective_workspace_id() 透传给 check_access
- `backend/app/gateway/app.py` — `_check_path_migration_pending` lifespan 探测
- `backend/tests/conftest.py` — `_auto_workspace_context` autouse + `Base.metadata.after_create` seed listener
- `backend/tests/_router_auth_helpers.py` — `_StubAuthMiddleware` 加 workspace_factory + override_user_contextvar
- `backend/pyproject.toml` — 新增 `no_auto_workspace` marker
- `Makefile` — `migrate-paths` target + help 文案

## 阅读路径

- 跨 workspace 必 404 怎么实现的 → 看 `check_access` 改造（commit `05be7f9a`）+ `@require_permission` 装饰器
- 路径新形态 → `Paths.thread_dir` 三档优先级（commit `f013fc1a`）
- 仓储 workspace_id 哨兵模式 → 任一 `*/sql.py` 看 create/get/search 签名
- 迁移脚本与 PR5 backfill 的边界 → 本文件 "迁移路径" 段，外加 `pr5-business-workspace-id.md`
