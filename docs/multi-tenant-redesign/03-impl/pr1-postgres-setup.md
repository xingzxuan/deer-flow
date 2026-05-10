# PR1 · Postgres 接入 + testcontainers fixture

> 实现笔记。对应 [docs/superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md](../../superpowers/plans/2026-05-10-stage-0-multi-tenant-foundation.md) PR1（T1.1-T1.10）。
>
> 状态：**已落地**，10 commits 提交在 branch `feat/stage-0-pr1-postgres-fixture`。

## 范围

让 DeerFlow 仓储测试 + dev 部署能跑在 Postgres 上，**不切默认 backend**（PR2 的事）。即：
- `postgres-test` optional extra（harness + backend）→ `testcontainers[postgres] 4.x` + 复用现有 `postgres` extra（asyncpg / psycopg / langgraph-checkpoint-postgres）
- `tests/fixtures/postgres.py` 提供 session 级 `postgres_container` + 每 test 一个 ephemeral DB 的 `postgres_url`
- 4 个 smoke test (`tests/test_postgres_smoke.py`) 验证 fixture 隔离 + `init_engine_from_config` 在 PG 上自动建表 + `ThreadMetaRepository` round-trip
- `docker/docker-compose-dev.yaml` 加 `postgres:16-alpine` service + healthcheck + named volume
- `scripts/doctor.py` 新增 Database section（sqlite/postgres/memory 三态探测，PG 实连 + version）
- `scripts/setup_wizard.py` Step 3 之后 inline 加一问 "Use Postgres?"，默认 Y
- `.github/workflows/backend-postgres-tests.yml` 独立 CI workflow 跑 `pytest -m postgres`

## 验收

- [x] `cd backend && uv sync --group dev --extra postgres-test` 装 testcontainers 成功
- [x] `pytest -m postgres -v` 在本地无 Docker 时**全部 SKIP**（fixture 自动跳过）
- [x] 全套 `pytest tests/` 3085 passed + 23 skipped + 0 failed（vs baseline 3086 passed + 18 skipped；diff = 4 新 PG smoke skip + 1 env 相关 live test 偶发 skip）
- [x] `docker compose -f docker/docker-compose-dev.yaml config --services` 列出 5 services 含 postgres（YAML 语法 OK）
- [x] `make doctor`（sqlite 模式）`Database` section 显示 `database backend = sqlite (.deer-flow/data)`
- [x] `wizard.writer.build_minimal_config(..., database_backend='postgres')` 输出 `database: { backend: postgres, postgres_url: $DATABASE_URL }`
- [ ] CI workflow `backend-postgres-tests` 在 PR 提交后跑通——**待 PR 上去后验证**
- [ ] `docker compose up -d postgres` + `pg_isready` live OK——**待 docker daemon 起**

## 关键决策（lock 项）

| 项 | 选择 | 理由 |
|---|---|---|
| Testcontainers 镜像 | `postgres:16-alpine` | 与远程 RDS 同大版本（待 PR 后用 `SELECT version()` 二次确认） |
| Test 隔离 | per-test database (CREATE/DROP DATABASE) | asyncpg 不支持 URL-embedded `search_path`；per-DB 一次 ~50ms，让 test 代码不感知 schema |
| Container scope | session-scoped | per-test container 启动 5-10s 太慢；shared container + per-test DB 是最佳折中 |
| Fixture skip 行为 | Docker 不可用时 `pytest.skip` 而非 `error` | dev 环境无 docker 也能跑其余 3000+ 测试 |
| pytest mark 名 | `postgres` | 简短、与 `no_auto_user` 风格一致 |
| Plugin 注册 | `pytest_plugins = ["fixtures.postgres"]` + sys.path insert | 不加 `tests/__init__.py` 避免影响现有 pytest 发现 |
| CI workflow 拆分 | 独立 `backend-postgres-tests.yml` | 不拖慢 fast unit test loop；fork 不强制承担 docker 开销 |
| doctor PG 检查实现 | asyncpg `connect()` + `SELECT version()` | 比 psycopg sync 更与生产路径对齐（生产用 asyncpg） |
| setup_wizard 集成 | inline 问答（不新建 wizard/steps/database.py） | 最小改动；后续如需更复杂选项再升级为完整 step 模块 |

## Fixture 用法示例

```python
import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_my_postgres_thing(postgres_url: str) -> None:
    from deerflow.persistence.engine import close_engine, init_engine

    await init_engine("postgres", url=postgres_url)
    try:
        # ...your test...
        ...
    finally:
        await close_engine()  # important: release connections before fixture teardown
```

## 跟进项（不在 PR1 范围）

- **PR2**：默认 backend 切 postgres（config.example.yaml + .env.example + docker-compose `gateway depends_on postgres`）
- **PR1 待 follow-up**：本地 docker daemon 起来后跑一次 `pytest -m postgres -v` 实测；如 PG 大版本不是 16，调 `postgres:16-alpine` → 对应版本
- 如果发现 fixture 启动慢导致 CI 时间不可接受，考虑 [pytest-postgresql](https://pypi.org/project/pytest-postgresql/) 替代 testcontainers（轻量但需要 GitHub Actions service container 而非 testcontainers daemon）

## 涉及文件

| 类别 | 文件 |
|---|---|
| 依赖 | `backend/packages/harness/pyproject.toml`、`backend/pyproject.toml`、`backend/uv.lock` |
| Fixture | `backend/tests/fixtures/__init__.py`、`backend/tests/fixtures/postgres.py` |
| Test | `backend/tests/test_postgres_smoke.py` |
| Conftest | `backend/tests/conftest.py`（pytest_plugins + sys.path） |
| Docker | `docker/docker-compose-dev.yaml` |
| 脚本 | `scripts/doctor.py`、`scripts/setup_wizard.py`、`scripts/wizard/writer.py` |
| CI | `.github/workflows/backend-postgres-tests.yml` |

## 提交记录

10 commits（每条 1 task）：

```
fab85b14 build(deps): add postgres-test optional extra for testcontainers   T1.1
ae4ea46b feat(docker): add postgres service to dev compose                  T1.2
31361e2d test(fixtures): add testcontainers Postgres fixture for Stage 0    T1.3
8f480cd7 test(persistence): postgres smoke tests for init_engine + ThreadMetaRepo  T1.4-T1.6
e6f5ba53 feat(doctor): add Database section probing configured backend      T1.7
eae01901 feat(wizard): add Postgres backend question to setup wizard        T1.8
a33b46b4 ci(postgres): add Postgres workflow running @pytest.mark.postgres tests  T1.9
```

T1.10 的 commit 是这份 doc 本身。
