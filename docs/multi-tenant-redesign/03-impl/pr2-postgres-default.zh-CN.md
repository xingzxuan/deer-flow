# PR2 · 默认 backend 切到 Postgres

> 实现笔记。对应 [docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md) PR2（T2.1-T2.10）。
>
> 状态：**已落地**，7 commits 提交在 branch `feat/stage-0-pr2-postgres-default`。

## 范围

让 Postgres 成为 dev + 生产的默认 backend，SQLite 沦为可选 fallback——但**不破坏** SQLite 路径（Stage 0 期间 dev 仍可走 SQLite）。

具体动作：
- `config.example.yaml` `database` 段：postgres 活跃、SQLite 注释；bump `config_version` 9→10
- `.env.example`：`DATABASE_URL` 行从注释改为活值（指向本地 docker postgres）
- `docker-compose-dev.yaml` `gateway depends_on postgres healthcheck`（生产 compose 不动——prod 用远程 RDS）
- `scripts/check.py` 加 `check_postgres_preflight()`：`make dev` 链上 PG 不通时 FAIL 阻断启动
- `README.md` Quick Start 加 Step 3 "Database backend"
- 2 个 regression test 锁定：(a) 显式 sqlite 仍工作 (b) `config.example.yaml` 默认 postgres

## 验收

- [x] 全套 `pytest tests/` 3087 passed + 23 skipped + 0 failed（PR1 基线 3085；diff = 2 新 default-backend 测试）
- [x] `test_explicit_sqlite_backend_still_works` PASS（SQLite regression 防线）
- [x] `test_config_example_default_backend_is_postgres` PASS
- [x] `python scripts/check.py` 在 sqlite 配置下 silent skip postgres preflight（无破坏现有用户）
- [x] `make doctor` 在 postgres 配置 + DATABASE_URL 不可达时给可执行 fix（指向 docker compose 命令）
- [x] `docker compose -f docker/docker-compose-dev.yaml config` 校验通过（postgres + gateway depends_on healthcheck 都合法）
- [ ] live：`make dev` 在 postgres 模式下 preflight + 服务启动——**待 docker daemon 起后由用户实测**
- [ ] live：CI workflow `backend-postgres-tests` 跑通——**PR push 后验证**

## 关键决策（lock 项）

| 项 | 选择 | 理由 |
|---|---|---|
| 默认 backend | postgres | Stage 0 不可逆决策（plan §不可逆决策一览）；省 Stage 1 重 ALTER 4 张表的返工 |
| SQLite 处理 | 注释保留为 fallback，不删 | offline dev / 单元测试场景仍需要 |
| `config_version` bump | 9 → 10 | 默认行为变化属于 schema 变更，触发 `AppConfig.from_file()` 旧版 warning |
| `DATABASE_URL` 形态 | `postgresql+asyncpg://` 而非 `postgresql://` | 与 SQLAlchemy 异步 dialect 对齐，避免 dialect 推断警告 |
| `depends_on` 仅 dev compose | prod compose 无 postgres service | 生产用远程 RDS，不在 compose 内 |
| PG preflight 加在 check.py | 不在 serve.sh 加 bash 版 | Makefile 已串 check.py → serve.sh，避免双重实现 |
| preflight FAIL vs WARN | FAIL（阻断启动） | 配了 PG 但不通就一定崩，preflight 早死好过 runtime 崩 |
| sqlite→pg 数据迁移工具 | **不实现**（plan T2.8 marked optional） | Stage 0 没有生产数据需要迁；follow-up |

## 跟进项（不在 PR2 范围）

- **PR3+**：在新建的 PG 上跑 workspace 表 schema
- **`backend/CLAUDE.md` 更新**：plan T2.9 包括 backend/CLAUDE.md，本 PR 仅做了 README；CLAUDE.md follow-up
- **`scripts/migrate_sqlite_to_postgres.py`**（plan T2.8 optional）：如果将来有用户从 SQLite dev 迁 PG 的需求，再写
- **prod compose postgres service**：当前 docker-compose.yaml（生产）无 postgres service；如果将来不用远程 RDS，可补上

## 涉及文件

| 类别 | 文件 |
|---|---|
| 配置 | `config.example.yaml`、`.env.example` |
| Docker | `docker/docker-compose-dev.yaml`（gateway depends_on postgres） |
| 脚本 | `scripts/check.py`（postgres preflight） |
| 测试 | `backend/tests/test_default_database_backend.py` |
| 文档 | `README.md` |

## 提交记录

7 commits（每条 1 task）：

```
404135a1 feat(config): default database backend to postgres                   T2.1
7d3d3560 feat(env): activate DATABASE_URL with local-dev default              T2.2
d312bdf9 test(config): pin default postgres backend + sqlite regression       T2.3 + T2.4
3e62a0f6 feat(docker): gateway depends_on postgres healthcheck (dev only)     T2.5
83b680b2 feat(check): postgres preflight in scripts/check.py                  T2.6
745a33e0 chore: T2.7 acknowledgement (covered by T1.8)                        T2.7（empty）
c53295df docs(readme): add Database backend section in Quick Start            T2.9
```

T2.10（impl doc）是这份文档本身。
T2.8（sqlite→pg 迁移工具）跳过——plan marked optional + Stage 0 无生产数据。
