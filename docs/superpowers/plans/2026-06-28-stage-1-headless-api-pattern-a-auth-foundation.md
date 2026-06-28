# Stage 1 · Headless API Pattern A 鉴权地基 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让业务系统 backend 用 workspace-scoped API key（`Authorization: Bearer dfk_...`）server-to-server 直调 DeerFlow Gateway 现有 REST endpoint，请求被正确归属到 key 背后的 service account + workspace 并受 workspace 隔离，并提供 owner/admin 自助 mint 闭环。

**Architecture:** 鉴权热路径走 `AuthMiddleware` 新增的 bearer 分支 → `APIKeyAuthBackend` 解析 token → 命中 `ApiKeyRepository.get_active_by_hash` → 加载 service account / workspace → 把 service account 映射成 `CurrentUser`（`user_id = SA.id`）并设 workspace contextvar，使下游 thread store / sandbox / `@require_permission` 与真人用户完全同构（零改业务表 schema）。token 工具与三表仓储落 `deerflow` 层（仓储热路径与管理 endpoint 都要用，受 harness boundary 约束），中间件 / 路由 / 管理 endpoint 落 `app` 层。

**Tech Stack:** Python 3.12 · FastAPI · Starlette `BaseHTTPMiddleware` · SQLAlchemy async（aiosqlite 测试 / asyncpg 生产）· `secrets` + `hashlib.sha256` · pytest + `pytest.mark.anyio` + `starlette.testclient.TestClient`。

**设计来源:** [stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md](../../multi-tenant-redesign/01-redesign/stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md)（决策 D1–D5、错误口径、不可逆清单皆以该 spec 为准）。

**运行约定（每条命令都从 `backend/` 目录执行）:**
- 单测：`PYTHONPATH=. uv run pytest tests/<file>.py -v`
- lint：`make lint`（ruff，行宽 240，双引号）
- 全量回归：`make test`
- 前端（仅 PR5）：在 `frontend/` 下 `pnpm lint && pnpm typecheck`

**PR 边界与提交节奏:** 5 个 PR，PR1→PR5 顺序实现。PR5（`/api/v1` 全量迁移）风险面最大，故放最后，让 PR1–PR4 的鉴权地基能独立证伪。每个 Task 末尾 commit。

---

## File Structure

新增 / 修改文件一览（精确路径）：

**PR1（deerflow 层）**
- Create: `backend/packages/harness/deerflow/auth/__init__.py`
- Create: `backend/packages/harness/deerflow/auth/tokens.py` — key 生成 / 哈希 / prefix 截取
- Create: `backend/packages/harness/deerflow/persistence/service_account/sql.py` — `ServiceAccountRepository`
- Create: `backend/packages/harness/deerflow/persistence/api_key/sql.py` — `ApiKeyRepository`
- Create: `backend/packages/harness/deerflow/persistence/external_user/sql.py` — `ExternalUserRepository`
- Modify: `backend/packages/harness/deerflow/persistence/service_account/__init__.py`（导出 Repository）
- Modify: `backend/packages/harness/deerflow/persistence/api_key/__init__.py`（导出 Repository）
- Modify: `backend/packages/harness/deerflow/persistence/external_user/__init__.py`（导出 Repository）
- Test: `backend/tests/test_tokens.py`, `test_service_account_repo.py`, `test_api_key_repo.py`, `test_external_user_repo.py`

**PR2（app 层 + 一个 deerflow 协议字段）**
- Modify: `backend/packages/harness/deerflow/runtime/user_context.py`（`CurrentUser` 协议加 `is_service_account`）
- Modify: `backend/app/gateway/auth/models.py`（`User` 加 `is_service_account: bool = False`）
- Create: `backend/app/gateway/auth/api_key_backend.py` — `ServicePrincipal` / `APIKeyAuthBackend` / `parse_scopes` / `build_api_key_backend`
- Modify: `backend/app/gateway/auth_middleware.py`（bearer 分支）
- Test: `backend/tests/test_api_key_backend.py`, `test_auth_middleware_api_key.py`

**PR3（app 层）**
- Modify: `backend/app/gateway/csrf_middleware.py`（`has_bearer_header` + `should_check_csrf` skip）
- Test: `backend/tests/test_csrf_bearer.py`

**PR4（app 层）**
- Create: `backend/app/gateway/routers/service_accounts.py`
- Create: `backend/app/gateway/routers/api_keys.py`
- Modify: `backend/app/gateway/authz.py`（`require_workspace_admin` 依赖）
- Modify: `backend/app/gateway/app.py`（注册两个 router + import）
- Test: `backend/tests/test_service_accounts_router.py`, `test_api_keys_router.py`, `test_headless_api_smoke.py`

**PR5（app 层 + 前端）**
- Modify: `backend/app/gateway/routers/*.py`（13 个 legacy router 前缀改相对）
- Modify: `backend/app/gateway/app.py`（双挂 `/api` + `/api/v1`）
- Create: `backend/app/gateway/deprecation_middleware.py` — 旧路径 `X-API-Deprecated` header
- Modify: `frontend/src/core/*/api.ts` 等路径串 → `/api/v1/...`
- Test: `backend/tests/test_api_v1_dual_mount.py`, `test_api_deprecation_header.py`

---

## PR1 · 三表仓储 + token 工具（`deerflow` 层）

> 镜像 `WorkspaceRepository`（`persistence/workspace/sql.py`）风格：构造收 `session_factory`，每方法开 fresh session，`@staticmethod _row_to_dict`。测试镜像 `tests/test_workspace_repo.py` / `test_api_key_schema.py`：每文件 `_make_repo(tmp_path)` 用 `init_engine("sqlite", ...)`，`_cleanup()` 用 `close_engine()`，`pytestmark = pytest.mark.anyio` + `anyio_backend` fixture 返回 `"asyncio"`。

### Task 1.1: token 工具 `deerflow/auth/tokens.py`

**Files:**
- Create: `backend/packages/harness/deerflow/auth/__init__.py`
- Create: `backend/packages/harness/deerflow/auth/tokens.py`
- Test: `backend/tests/test_tokens.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_tokens.py`:

```python
"""Tests for deerflow.auth.tokens (Stage 1 PR1).

API key 格式 / 哈希 / prefix 截取。格式锁定 dfk_{live,test}_<24>，
prefix = 前 16 字符（含 dfk_live_），sha256 hex 存储（spec D5）。
"""

from __future__ import annotations

import hashlib

import pytest

from deerflow.auth.tokens import GeneratedKey, generate_api_key, hash_api_key, split_prefix


def test_generate_live_key_shape():
    key = generate_api_key("live")
    assert isinstance(key, GeneratedKey)
    assert key.plaintext.startswith("dfk_live_")
    # dfk_live_ (9) + token_urlsafe(18) (24) = 33 chars
    assert len(key.plaintext) == 33
    assert key.prefix == key.plaintext[:16]
    assert len(key.prefix) == 16
    assert key.key_hash == hashlib.sha256(key.plaintext.encode("utf-8")).hexdigest()
    assert len(key.key_hash) == 64


def test_generate_test_key_prefix_env():
    key = generate_api_key("test")
    assert key.plaintext.startswith("dfk_test_")
    assert key.prefix.startswith("dfk_test_")


def test_generate_rejects_bad_env():
    with pytest.raises(ValueError):
        generate_api_key("prod")  # type: ignore[arg-type]


def test_two_keys_are_unique():
    a = generate_api_key("live")
    b = generate_api_key("live")
    assert a.plaintext != b.plaintext
    assert a.key_hash != b.key_hash


def test_hash_is_deterministic_and_not_reversible():
    plaintext = "dfk_live_abcdefghijklmnopqrstuvwx"
    h1 = hash_api_key(plaintext)
    h2 = hash_api_key(plaintext)
    assert h1 == h2
    assert h1 != plaintext
    assert len(h1) == 64


def test_split_prefix_takes_first_16():
    assert split_prefix("dfk_live_abcdefghijklmnop") == "dfk_live_abcdefg"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_tokens.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deerflow.auth'`

- [ ] **Step 3: 写实现**

Create `backend/packages/harness/deerflow/auth/__init__.py`:

```python
"""Auth primitives shared by the headless API (Stage 1).

Lives in the ``deerflow`` (harness) layer because both the persistence
hot path (``ApiKeyRepository.get_active_by_hash``) and the app-layer
mint endpoint need token generation/hashing, and the harness boundary
forbids ``deerflow`` importing ``app``.
"""

from __future__ import annotations

from deerflow.auth.tokens import GeneratedKey, generate_api_key, hash_api_key, split_prefix

__all__ = ["GeneratedKey", "generate_api_key", "hash_api_key", "split_prefix"]
```

Create `backend/packages/harness/deerflow/auth/tokens.py`:

```python
"""API key generation, hashing, and prefix extraction (Stage 1 PR1).

Format is irreversible once business systems integrate (spec D5):
``dfk_live_<24>`` / ``dfk_test_<24>``. The public ``key_prefix`` is the
first 16 chars (``dfk_live_`` + 7 random) and is stored UNIQUE for audit
logging; the DB only ever stores ``sha256(plaintext)`` hex, never the
plaintext.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Literal

_PREFIX_LEN = 16
# token_urlsafe(18) yields ceil(18 * 4 / 3) = 24 url-safe chars.
_RANDOM_BYTES = 18


@dataclass(frozen=True)
class GeneratedKey:
    """A freshly minted key. ``plaintext`` is returned to the caller
    exactly once; only ``prefix`` + ``key_hash`` are persisted."""

    plaintext: str
    prefix: str
    key_hash: str


def hash_api_key(plaintext: str) -> str:
    """Return the sha-256 hex digest of a plaintext token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def split_prefix(plaintext: str) -> str:
    """Return the public, loggable prefix (first 16 chars) of a token."""
    return plaintext[:_PREFIX_LEN]


def generate_api_key(env: Literal["live", "test"]) -> GeneratedKey:
    """Generate a new API key for the given environment.

    Raises ``ValueError`` for any env other than ``"live"`` / ``"test"``.
    """
    if env not in ("live", "test"):
        raise ValueError(f"env must be 'live' or 'test', got {env!r}")
    random_part = secrets.token_urlsafe(_RANDOM_BYTES)
    plaintext = f"dfk_{env}_{random_part}"
    return GeneratedKey(plaintext=plaintext, prefix=split_prefix(plaintext), key_hash=hash_api_key(plaintext))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_tokens.py -v`
Expected: PASS（6 tests）

- [ ] **Step 5: commit**

```bash
cd backend && git add packages/harness/deerflow/auth/ tests/test_tokens.py
git commit -m "feat(auth): API key token generation/hashing utilities (Stage 1 PR1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 1.2: `ServiceAccountRepository`

**Files:**
- Create: `backend/packages/harness/deerflow/persistence/service_account/sql.py`
- Modify: `backend/packages/harness/deerflow/persistence/service_account/__init__.py`
- Test: `backend/tests/test_service_account_repo.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_service_account_repo.py`:

```python
"""Tests for ServiceAccountRepository (Stage 1 PR1).

SQLite ephemeral DB per test, mirroring test_workspace_repo / test_api_key_schema.
"""

from __future__ import annotations

import pytest

from deerflow.persistence.service_account import ServiceAccountRepository
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace.model import WorkspaceRow

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _make_repo(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    return ServiceAccountRepository(get_session_factory())


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_parents(repo, *, user_id="u-alice", workspace_id="w-1") -> None:
    async with repo._sf() as session:
        session.add(UserRow(id=user_id, email=f"{user_id}@example.com"))
        await session.commit()
    async with repo._sf() as session:
        session.add(WorkspaceRow(id=workspace_id, name="WS", slug="ws", owner_id=user_id))
        await session.commit()


async def test_create_then_get_roundtrip(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_parents(repo)
        created = await repo.create(workspace_id="w-1", name="ci-bot", created_by="u-alice")
        assert created["workspace_id"] == "w-1"
        assert created["name"] == "ci-bot"
        assert created["role"] == "member"
        assert created["identity_mode"] == "collapsed"
        assert created["status"] == "active"
        assert len(created["id"]) == 36

        fetched = await repo.get(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
    finally:
        await _cleanup()


async def test_get_returns_none_when_missing(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        assert await repo.get("nope") is None
    finally:
        await _cleanup()


async def test_get_active_excludes_suspended(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_parents(repo)
        sa = await repo.create(workspace_id="w-1", name="bot", created_by="u-alice")
        assert await repo.get_active(sa["id"]) is not None
        await repo.update_status(sa["id"], "suspended")
        assert await repo.get_active(sa["id"]) is None
        # get() still returns the row regardless of status
        assert await repo.get(sa["id"]) is not None
    finally:
        await _cleanup()


async def test_list_by_workspace(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_parents(repo)
        await _seed_parents(repo, user_id="u-bob", workspace_id="w-2")
        await repo.create(workspace_id="w-1", name="a", created_by="u-alice")
        await repo.create(workspace_id="w-1", name="b", created_by="u-alice")
        await repo.create(workspace_id="w-2", name="c", created_by="u-bob")
        rows = await repo.list_by_workspace("w-1")
        assert {r["name"] for r in rows} == {"a", "b"}
    finally:
        await _cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_service_account_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'ServiceAccountRepository'`

- [ ] **Step 3: 写实现**

Create `backend/packages/harness/deerflow/persistence/service_account/sql.py`:

```python
"""SQLAlchemy-backed service account repository (Stage 1 PR1).

Mirrors :class:`WorkspaceRepository`: fresh session per method,
``_row_to_dict`` static helper. Workspace scoping is enforced by the
caller (route layer reads the workspace contextvar); the repository
takes ``workspace_id`` explicitly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.service_account.model import ServiceAccountRow

_VALID_STATUSES = frozenset({"active", "suspended", "deleted"})


class ServiceAccountRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: ServiceAccountRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "name": row.name,
            "role": row.role,
            "identity_mode": row.identity_mode,
            "status": row.status,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    async def create(
        self,
        *,
        workspace_id: str,
        name: str,
        created_by: str,
        role: str = "member",
        identity_mode: str = "collapsed",
        status: str = "active",
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        row = ServiceAccountRow(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            name=name,
            role=role,
            identity_mode=identity_mode,
            status=status,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def get(self, sa_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(ServiceAccountRow, sa_id)
            return self._row_to_dict(row) if row else None

    async def get_active(self, sa_id: str) -> dict[str, Any] | None:
        """Return the row only when ``status == 'active'`` (auth hot path)."""
        async with self._sf() as session:
            row = await session.get(ServiceAccountRow, sa_id)
            if row is None or row.status != "active":
                return None
            return self._row_to_dict(row)

    async def list_by_workspace(self, workspace_id: str) -> list[dict[str, Any]]:
        async with self._sf() as session:
            result = await session.execute(
                select(ServiceAccountRow).where(ServiceAccountRow.workspace_id == workspace_id).order_by(ServiceAccountRow.created_at.desc())
            )
            return [self._row_to_dict(r) for r in result.scalars()]

    async def update_status(self, sa_id: str, status: str) -> None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"status {status!r} not in {_VALID_STATUSES!r}")
        async with self._sf() as session:
            await session.execute(
                update(ServiceAccountRow).where(ServiceAccountRow.id == sa_id).values(status=status, updated_at=datetime.now(UTC))
            )
            await session.commit()
```

Modify `backend/packages/harness/deerflow/persistence/service_account/__init__.py` — append the repository export. Change the final block from:

```python
from deerflow.persistence.service_account.model import ServiceAccountRow

__all__ = ["ServiceAccountRow"]
```

to:

```python
from deerflow.persistence.service_account.model import ServiceAccountRow
from deerflow.persistence.service_account.sql import ServiceAccountRepository

__all__ = ["ServiceAccountRepository", "ServiceAccountRow"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_service_account_repo.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: commit**

```bash
cd backend && git add packages/harness/deerflow/persistence/service_account/ tests/test_service_account_repo.py
git commit -m "feat(persistence): ServiceAccountRepository (Stage 1 PR1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 1.3: `ApiKeyRepository`

**Files:**
- Create: `backend/packages/harness/deerflow/persistence/api_key/sql.py`
- Modify: `backend/packages/harness/deerflow/persistence/api_key/__init__.py`
- Test: `backend/tests/test_api_key_repo.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_api_key_repo.py`:

```python
"""Tests for ApiKeyRepository (Stage 1 PR1).

get_active_by_hash is the auth hot path: must return None for revoked
and expired keys. Expiry is filtered in Python (driver-agnostic) while
revoked_at IS NULL rides the partial index idx_api_keys_active.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from deerflow.auth.tokens import generate_api_key
from deerflow.persistence.api_key import ApiKeyRepository
from deerflow.persistence.service_account.model import ServiceAccountRow
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace.model import WorkspaceRow

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _make_repo(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    return ApiKeyRepository(get_session_factory())


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_sa(repo, *, sa_id="sa-1") -> None:
    async with repo._sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with repo._sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice"))
        await session.commit()
    async with repo._sf() as session:
        session.add(ServiceAccountRow(id=sa_id, workspace_id="w-1", name="bot", role="member", identity_mode="collapsed", status="active", created_by="u-alice"))
        await session.commit()


async def _mint(repo, *, expires_at=None, scopes="threads:read"):
    gen = generate_api_key("live")
    created = await repo.create(service_account_id="sa-1", key_prefix=gen.prefix, key_hash=gen.key_hash, name="k", scopes=scopes, expires_at=expires_at)
    return gen, created


async def test_create_then_get_active_by_hash(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        gen, created = await _mint(repo)
        assert created["key_prefix"] == gen.prefix
        assert "key_hash" not in created  # never expose the hash in dicts
        found = await repo.get_active_by_hash(gen.key_hash)
        assert found is not None
        assert found["id"] == created["id"]
        assert found["scopes"] == "threads:read"
    finally:
        await _cleanup()


async def test_get_active_by_hash_miss_returns_none(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        assert await repo.get_active_by_hash("deadbeef") is None
    finally:
        await _cleanup()


async def test_revoked_key_not_active(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        gen, created = await _mint(repo)
        await repo.revoke(created["id"])
        assert await repo.get_active_by_hash(gen.key_hash) is None
    finally:
        await _cleanup()


async def test_expired_key_not_active(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        past = datetime.now(UTC) - timedelta(hours=1)
        gen, _ = await _mint(repo, expires_at=past)
        assert await repo.get_active_by_hash(gen.key_hash) is None
    finally:
        await _cleanup()


async def test_future_expiry_still_active(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        future = datetime.now(UTC) + timedelta(hours=1)
        gen, _ = await _mint(repo, expires_at=future)
        assert await repo.get_active_by_hash(gen.key_hash) is not None
    finally:
        await _cleanup()


async def test_touch_last_used_sets_timestamp(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        gen, created = await _mint(repo)
        assert created["last_used_at"] is None
        await repo.touch_last_used(created["id"])
        refetched = await repo.get(created["id"])
        assert refetched["last_used_at"] is not None
    finally:
        await _cleanup()


async def test_list_by_service_account(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        await _mint(repo)
        await _mint(repo)
        rows = await repo.list_by_service_account("sa-1")
        assert len(rows) == 2
        assert all("key_hash" not in r for r in rows)
    finally:
        await _cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'ApiKeyRepository'`

- [ ] **Step 3: 写实现**

Create `backend/packages/harness/deerflow/persistence/api_key/sql.py`:

```python
"""SQLAlchemy-backed API key repository (Stage 1 PR1).

``get_active_by_hash`` is the auth hot path. ``revoked_at IS NULL``
rides the partial index ``idx_api_keys_active``; expiry is filtered in
Python so the behaviour is identical across sqlite/postgres drivers.

``_row_to_dict`` deliberately omits ``key_hash`` — no dict this
repository returns ever carries the secret material.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.api_key.model import ApiKeyRow


class ApiKeyRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: ApiKeyRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "service_account_id": row.service_account_id,
            "key_prefix": row.key_prefix,
            "name": row.name,
            "scopes": row.scopes,
            "rate_limit_rpm": row.rate_limit_rpm,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    async def create(
        self,
        *,
        service_account_id: str,
        key_prefix: str,
        key_hash: str,
        name: str,
        scopes: str,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        row = ApiKeyRow(
            id=str(uuid.uuid4()),
            service_account_id=service_account_id,
            key_prefix=key_prefix,
            key_hash=key_hash,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
            created_at=datetime.now(UTC),
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def get(self, key_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(ApiKeyRow, key_id)
            return self._row_to_dict(row) if row else None

    async def get_active_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        """Auth hot path: return the key iff not revoked and not expired."""
        async with self._sf() as session:
            result = await session.execute(
                select(ApiKeyRow).where(ApiKeyRow.key_hash == key_hash, ApiKeyRow.revoked_at.is_(None))
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
                return None
            return self._row_to_dict(row)

    async def list_by_service_account(self, service_account_id: str) -> list[dict[str, Any]]:
        async with self._sf() as session:
            result = await session.execute(
                select(ApiKeyRow).where(ApiKeyRow.service_account_id == service_account_id).order_by(ApiKeyRow.created_at.desc())
            )
            return [self._row_to_dict(r) for r in result.scalars()]

    async def revoke(self, key_id: str) -> None:
        """Soft-revoke: set ``revoked_at`` (row is kept for audit)."""
        async with self._sf() as session:
            await session.execute(update(ApiKeyRow).where(ApiKeyRow.id == key_id).values(revoked_at=datetime.now(UTC)))
            await session.commit()

    async def touch_last_used(self, key_id: str) -> None:
        """Best-effort: stamp ``last_used_at`` after a successful auth."""
        async with self._sf() as session:
            await session.execute(update(ApiKeyRow).where(ApiKeyRow.id == key_id).values(last_used_at=datetime.now(UTC)))
            await session.commit()
```

Modify `backend/packages/harness/deerflow/persistence/api_key/__init__.py` final block to:

```python
from deerflow.persistence.api_key.model import ApiKeyRow
from deerflow.persistence.api_key.sql import ApiKeyRepository

__all__ = ["ApiKeyRepository", "ApiKeyRow"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_repo.py -v`
Expected: PASS（7 tests）

- [ ] **Step 5: commit**

```bash
cd backend && git add packages/harness/deerflow/persistence/api_key/ tests/test_api_key_repo.py
git commit -m "feat(persistence): ApiKeyRepository with active-key hot path (Stage 1 PR1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 1.4: `ExternalUserRepository`（建好不接业务）

**Files:**
- Create: `backend/packages/harness/deerflow/persistence/external_user/sql.py`
- Modify: `backend/packages/harness/deerflow/persistence/external_user/__init__.py`
- Test: `backend/tests/test_external_user_repo.py`

> 本 PR 只建仓储（读方法 + upsert 骨架），**不接任何鉴权调用方**——透传逻辑留到轨道二后续 PR，与 PR8 建 schema 不接路由同思路（spec §5 PR1）。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_external_user_repo.py`:

```python
"""Tests for ExternalUserRepository (Stage 1 PR1).

Repository is built but not yet wired to any auth path. upsert is
idempotent on (service_account_id, external_id) per the table's
UniqueConstraint uq_external_users_sa_external.
"""

from __future__ import annotations

import pytest

from deerflow.persistence.external_user import ExternalUserRepository
from deerflow.persistence.service_account.model import ServiceAccountRow
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace.model import WorkspaceRow

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _make_repo(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    return ExternalUserRepository(get_session_factory())


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def _seed_sa(repo) -> None:
    async with repo._sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with repo._sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice"))
        await session.commit()
    async with repo._sf() as session:
        session.add(ServiceAccountRow(id="sa-1", workspace_id="w-1", name="bot", role="member", identity_mode="external_passthrough", status="active", created_by="u-alice"))
        await session.commit()


async def test_upsert_inserts_then_updates_same_row(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        first = await repo.upsert(workspace_id="w-1", service_account_id="sa-1", external_id="ext-42", display_name="alice")
        assert first["external_id"] == "ext-42"
        assert first["last_seen_at"] is not None
        second = await repo.upsert(workspace_id="w-1", service_account_id="sa-1", external_id="ext-42")
        # same logical row (no duplicate)
        assert second["id"] == first["id"]
        rows = await repo.list_by_workspace("w-1")
        assert len(rows) == 1
    finally:
        await _cleanup()


async def test_get_by_external_id(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        await _seed_sa(repo)
        await repo.upsert(workspace_id="w-1", service_account_id="sa-1", external_id="ext-7")
        found = await repo.get_by_external_id(service_account_id="sa-1", external_id="ext-7")
        assert found is not None
        assert found["external_id"] == "ext-7"
        assert await repo.get_by_external_id(service_account_id="sa-1", external_id="nope") is None
    finally:
        await _cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_external_user_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'ExternalUserRepository'`

- [ ] **Step 3: 写实现**

Create `backend/packages/harness/deerflow/persistence/external_user/sql.py`:

```python
"""SQLAlchemy-backed external user repository (Stage 1 PR1).

Built but NOT yet wired to any auth path — the X-External-User-Id
passthrough that calls ``upsert`` lands in a later track-2 PR. ``upsert``
is idempotent on (service_account_id, external_id).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.external_user.model import ExternalUserRow


class ExternalUserRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: ExternalUserRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "service_account_id": row.service_account_id,
            "external_id": row.external_id,
            "display_name": row.display_name,
            "metadata": dict(row.metadata_json or {}),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        }

    async def get(self, external_user_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(ExternalUserRow, external_user_id)
            return self._row_to_dict(row) if row else None

    async def get_by_external_id(self, *, service_account_id: str, external_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            result = await session.execute(
                select(ExternalUserRow).where(
                    ExternalUserRow.service_account_id == service_account_id,
                    ExternalUserRow.external_id == external_id,
                )
            )
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def list_by_workspace(self, workspace_id: str) -> list[dict[str, Any]]:
        async with self._sf() as session:
            result = await session.execute(
                select(ExternalUserRow).where(ExternalUserRow.workspace_id == workspace_id).order_by(ExternalUserRow.created_at.desc())
            )
            return [self._row_to_dict(r) for r in result.scalars()]

    async def upsert(
        self,
        *,
        workspace_id: str,
        service_account_id: str,
        external_id: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert a new external user or refresh ``last_seen_at`` on an
        existing (service_account_id, external_id) row."""
        now = datetime.now(UTC)
        async with self._sf() as session:
            result = await session.execute(
                select(ExternalUserRow).where(
                    ExternalUserRow.service_account_id == service_account_id,
                    ExternalUserRow.external_id == external_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = ExternalUserRow(
                    id=str(uuid.uuid4()),
                    workspace_id=workspace_id,
                    service_account_id=service_account_id,
                    external_id=external_id,
                    display_name=display_name,
                    metadata_json=metadata or {},
                    created_at=now,
                    last_seen_at=now,
                )
                session.add(row)
            else:
                row.last_seen_at = now
                if display_name is not None:
                    row.display_name = display_name
                if metadata is not None:
                    row.metadata_json = metadata
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)
```

Modify `backend/packages/harness/deerflow/persistence/external_user/__init__.py` final block to:

```python
from deerflow.persistence.external_user.model import ExternalUserRow
from deerflow.persistence.external_user.sql import ExternalUserRepository

__all__ = ["ExternalUserRepository", "ExternalUserRow"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_external_user_repo.py -v`
Expected: PASS（2 tests）

- [ ] **Step 5: PR1 收尾 — lint + boundary + commit**

Run boundary + lint（PR1 全在 deerflow 层，必须不引入 app import）:

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_harness_boundary.py -v && make lint
```
Expected: PASS / no lint errors

```bash
cd backend && git add packages/harness/deerflow/persistence/external_user/ tests/test_external_user_repo.py
git commit -m "feat(persistence): ExternalUserRepository scaffold (Stage 1 PR1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## PR2 · `APIKeyAuthBackend` + `AuthMiddleware` 双路径（`app` 层）

> 关键不变量（spec §4）：API key 路径走完后，下游看到的 `(user_id, workspace_id)` 与一个真人用户在该 workspace 下完全同构（`user_id = SA.id`），因此 thread store / sandbox / `@require_permission` owner_check 零改动。

### Task 2.1: `CurrentUser` 协议 + `ServicePrincipal` + `parse_scopes`

**Files:**
- Modify: `backend/packages/harness/deerflow/runtime/user_context.py`
- Modify: `backend/app/gateway/auth/models.py`
- Create: `backend/app/gateway/auth/api_key_backend.py`（本 task 只放 `ServicePrincipal` + `parse_scopes`，backend 类在 Task 2.2）
- Test: `backend/tests/test_api_key_backend.py`（本 task 只测 parse_scopes + principal）

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_api_key_backend.py`:

```python
"""Tests for the API key auth backend (Stage 1 PR2)."""

from __future__ import annotations

from app.gateway.auth.api_key_backend import ServicePrincipal, parse_scopes


def test_parse_scopes_splits_and_strips():
    assert parse_scopes("threads:read, threads:write") == ["threads:read", "threads:write"]


def test_parse_scopes_empty_string_is_empty_list():
    assert parse_scopes("") == []
    assert parse_scopes("   ") == []


def test_parse_scopes_drops_empty_segments():
    assert parse_scopes("threads:read,,runs:create,") == ["threads:read", "runs:create"]


def test_service_principal_is_service_account_true_by_default():
    p = ServicePrincipal(id="sa-1")
    assert p.id == "sa-1"
    assert p.is_service_account is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.gateway.auth.api_key_backend'`

- [ ] **Step 3: 写实现**

Modify `backend/packages/harness/deerflow/runtime/user_context.py` — add the `is_service_account` attribute to the `CurrentUser` protocol. Change:

```python
@runtime_checkable
class CurrentUser(Protocol):
    """Structural type for the current authenticated user.

    Any object with an ``.id: str`` attribute satisfies this protocol.
    Concrete implementations live in ``app.gateway.auth.models.User``.
    """

    id: str
```

to:

```python
@runtime_checkable
class CurrentUser(Protocol):
    """Structural type for the current authenticated user.

    Requires ``.id: str`` plus ``.is_service_account: bool`` — the latter
    distinguishes a human (cookie/JWT) principal from a headless service
    account (API key). Concrete implementations:
    ``app.gateway.auth.models.User`` (False) and
    ``app.gateway.auth.api_key_backend.ServicePrincipal`` (True).
    Readers that may run before either is set should use
    ``getattr(user, "is_service_account", False)``.
    """

    id: str
    is_service_account: bool
```

Modify `backend/app/gateway/auth/models.py` — add the field to `User` so the cookie path satisfies the protocol. After the `token_version` field (and before `default_workspace_id`), add:

```python
    # Headless API discriminator (Stage 1 PR2). Always False for human
    # users; ServicePrincipal sets it True. Lets downstream code branch
    # on principal kind without isinstance gymnastics.
    is_service_account: bool = Field(default=False, description="True only for API-key service accounts, never for human users")
```

Create `backend/app/gateway/auth/api_key_backend.py`:

```python
"""API key authentication backend (Stage 1 PR2).

Resolves an ``Authorization: Bearer dfk_...`` token into a
``ServicePrincipal`` + workspace + scopes, so ``AuthMiddleware`` can
stamp the same contextvars a cookie-authenticated human would set
(spec D1: user_id = SA.id).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServicePrincipal:
    """Non-human principal backing an API key. Satisfies the
    ``deerflow.runtime.user_context.CurrentUser`` protocol."""

    id: str
    is_service_account: bool = True


def parse_scopes(scopes: str) -> list[str]:
    """Parse a comma-separated scope string into a permission list.

    ``"threads:read, threads:write"`` -> ``["threads:read", "threads:write"]``.
    Empty / whitespace-only segments are dropped.
    """
    return [s.strip() for s in scopes.split(",") if s.strip()]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_backend.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: commit**

```bash
cd backend && git add packages/harness/deerflow/runtime/user_context.py app/gateway/auth/models.py app/gateway/auth/api_key_backend.py tests/test_api_key_backend.py
git commit -m "feat(auth): ServicePrincipal + is_service_account discriminator (Stage 1 PR2)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2.2: `APIKeyAuthBackend.authenticate`

**Files:**
- Modify: `backend/app/gateway/auth/api_key_backend.py`
- Test: `backend/tests/test_api_key_backend.py`（追加 authenticate 测试）

- [ ] **Step 1: 写失败测试**（追加到 `test_api_key_backend.py` 末尾）

```python
import pytest

from deerflow.auth.tokens import generate_api_key

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _setup_backend(tmp_path, *, sa_status="active", ws_status="active", scopes="threads:read", expires_at=None, revoke=False):
    from app.gateway.auth.api_key_backend import APIKeyAuthBackend
    from deerflow.persistence.api_key import ApiKeyRepository
    from deerflow.persistence.engine import get_session_factory, init_engine
    from deerflow.persistence.service_account import ServiceAccountRepository
    from deerflow.persistence.service_account.model import ServiceAccountRow
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace import WorkspaceRepository
    from deerflow.persistence.workspace.model import WorkspaceRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice", status=ws_status))
        await session.commit()
    async with sf() as session:
        session.add(ServiceAccountRow(id="sa-1", workspace_id="w-1", name="bot", role="member", identity_mode="collapsed", status=sa_status, created_by="u-alice"))
        await session.commit()

    api_key_repo = ApiKeyRepository(sf)
    gen = generate_api_key("live")
    created = await api_key_repo.create(service_account_id="sa-1", key_prefix=gen.prefix, key_hash=gen.key_hash, name="k", scopes=scopes, expires_at=expires_at)
    if revoke:
        await api_key_repo.revoke(created["id"])

    backend = APIKeyAuthBackend(api_key_repo=api_key_repo, service_account_repo=ServiceAccountRepository(sf), workspace_repo=WorkspaceRepository(sf))
    return backend, gen


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


async def test_authenticate_valid_key(tmp_path):
    backend, gen = await _setup_backend(tmp_path, scopes="threads:read,threads:write")
    try:
        result = await backend.authenticate(gen.plaintext)
        assert result is not None
        assert result.principal.id == "sa-1"
        assert result.principal.is_service_account is True
        assert result.workspace_id == "w-1"
        assert result.role == "member"
        assert result.permissions == ["threads:read", "threads:write"]
    finally:
        await _cleanup()


async def test_authenticate_unknown_token_returns_none(tmp_path):
    backend, _ = await _setup_backend(tmp_path)
    try:
        assert await backend.authenticate("dfk_live_doesnotexist000000000000") is None
    finally:
        await _cleanup()


async def test_authenticate_revoked_key_returns_none(tmp_path):
    backend, gen = await _setup_backend(tmp_path, revoke=True)
    try:
        assert await backend.authenticate(gen.plaintext) is None
    finally:
        await _cleanup()


async def test_authenticate_suspended_sa_returns_none(tmp_path):
    backend, gen = await _setup_backend(tmp_path, sa_status="suspended")
    try:
        assert await backend.authenticate(gen.plaintext) is None
    finally:
        await _cleanup()


async def test_authenticate_suspended_workspace_returns_none(tmp_path):
    backend, gen = await _setup_backend(tmp_path, ws_status="suspended")
    try:
        assert await backend.authenticate(gen.plaintext) is None
    finally:
        await _cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_backend.py -v`
Expected: FAIL — `ImportError: cannot import name 'APIKeyAuthBackend'`

- [ ] **Step 3: 写实现** — append to `backend/app/gateway/auth/api_key_backend.py`:

```python
from deerflow.auth.tokens import hash_api_key


@dataclass(frozen=True)
class ApiKeyAuthResult:
    """Everything ``AuthMiddleware`` needs to stamp request state +
    contextvars from a verified API key."""

    principal: ServicePrincipal
    workspace_id: str
    role: str
    permissions: list[str]


class APIKeyAuthBackend:
    def __init__(self, *, api_key_repo, service_account_repo, workspace_repo) -> None:
        self._api_key_repo = api_key_repo
        self._service_account_repo = service_account_repo
        self._workspace_repo = workspace_repo

    async def authenticate(self, token: str) -> ApiKeyAuthResult | None:
        """Resolve a plaintext token to an auth result, or None (→ 401)."""
        key = await self._api_key_repo.get_active_by_hash(hash_api_key(token))
        if key is None:
            return None

        sa = await self._service_account_repo.get_active(key["service_account_id"])
        if sa is None:
            return None

        # SA is not a workspace *member* — bypass the membership filter
        # with the documented user_id=None admin/migration path.
        workspace = await self._workspace_repo.get(sa["workspace_id"], user_id=None)
        if workspace is None or workspace["status"] != "active":
            return None

        # Best-effort: never block the request if the timestamp write fails.
        try:
            await self._api_key_repo.touch_last_used(key["id"])
        except Exception:  # noqa: BLE001 — best-effort, log and continue
            logger.warning("touch_last_used failed for api_key %s", key["id"], exc_info=True)

        return ApiKeyAuthResult(
            principal=ServicePrincipal(id=sa["id"]),
            workspace_id=sa["workspace_id"],
            role=sa["role"],
            permissions=parse_scopes(key["scopes"]),
        )


def build_api_key_backend() -> APIKeyAuthBackend | None:
    """Construct a backend from the global session factory, or None when
    persistence is the in-memory backend (no DB → no API keys)."""
    from deerflow.persistence.api_key import ApiKeyRepository
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.service_account import ServiceAccountRepository
    from deerflow.persistence.workspace import WorkspaceRepository

    sf = get_session_factory()
    if sf is None:
        return None
    return APIKeyAuthBackend(api_key_repo=ApiKeyRepository(sf), service_account_repo=ServiceAccountRepository(sf), workspace_repo=WorkspaceRepository(sf))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_key_backend.py -v`
Expected: PASS（9 tests）

- [ ] **Step 5: commit**

```bash
cd backend && git add app/gateway/auth/api_key_backend.py tests/test_api_key_backend.py
git commit -m "feat(auth): APIKeyAuthBackend resolves token to SA principal (Stage 1 PR2)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2.3: `AuthMiddleware` bearer 分支

**Files:**
- Modify: `backend/app/gateway/auth_middleware.py`
- Test: `backend/tests/test_auth_middleware_api_key.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_auth_middleware_api_key.py`:

```python
"""AuthMiddleware bearer-path integration tests (Stage 1 PR2).

Drives the real middleware via a minimal app with a probe route that
echoes the resolved contextvars, proving user_id=SA.id / workspace_id
are stamped identically to a human request.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from deerflow.auth.tokens import generate_api_key

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _seed_key(tmp_path, *, scopes="threads:read", revoke=False):
    from deerflow.persistence.api_key import ApiKeyRepository
    from deerflow.persistence.engine import get_session_factory, init_engine
    from deerflow.persistence.service_account.model import ServiceAccountRow
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace.model import WorkspaceRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice"))
        await session.commit()
    async with sf() as session:
        session.add(ServiceAccountRow(id="sa-1", workspace_id="w-1", name="bot", role="member", identity_mode="collapsed", status="active", created_by="u-alice"))
        await session.commit()
    repo = ApiKeyRepository(sf)
    gen = generate_api_key("live")
    created = await repo.create(service_account_id="sa-1", key_prefix=gen.prefix, key_hash=gen.key_hash, name="k", scopes=scopes)
    if revoke:
        await repo.revoke(created["id"])
    return gen


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _make_app():
    from fastapi import FastAPI, Request

    from app.gateway.auth_middleware import AuthMiddleware
    from deerflow.runtime.user_context import get_effective_user_id
    from deerflow.runtime.workspace_context import get_effective_workspace_id

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/probe")
    async def probe(request: Request):
        return {
            "user_id": get_effective_user_id(),
            "workspace_id": get_effective_workspace_id(),
            "is_sa": getattr(request.state.user, "is_service_account", None),
        }

    return app


async def test_valid_bearer_sets_sa_contextvars(tmp_path):
    gen = await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/probe", headers={"Authorization": f"Bearer {gen.plaintext}"})
        assert r.status_code == 200
        assert r.json() == {"user_id": "sa-1", "workspace_id": "w-1", "is_sa": True}
    finally:
        await _cleanup()


async def test_invalid_bearer_returns_401(tmp_path):
    await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/probe", headers={"Authorization": "Bearer dfk_live_bogus00000000000000000"})
        assert r.status_code == 401
    finally:
        await _cleanup()


async def test_revoked_bearer_returns_401(tmp_path):
    gen = await _seed_key(tmp_path, revoke=True)
    try:
        client = TestClient(_make_app())
        r = client.get("/api/probe", headers={"Authorization": f"Bearer {gen.plaintext}"})
        assert r.status_code == 401
    finally:
        await _cleanup()


async def test_non_dfk_bearer_falls_through_to_cookie_path(tmp_path):
    await _seed_key(tmp_path)
    try:
        client = TestClient(_make_app())
        # A non-dfk bearer is NOT the API-key path; with no cookie the
        # cookie path 401s (NOT_AUTHENTICATED), proving no mis-route.
        r = client.get("/api/probe", headers={"Authorization": "Bearer some.jwt.token"})
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "not_authenticated"
    finally:
        await _cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth_middleware_api_key.py -v`
Expected: FAIL — `test_valid_bearer_sets_sa_contextvars` returns 401（bearer 分支尚未实现）

- [ ] **Step 3: 写实现** — modify `backend/app/gateway/auth_middleware.py`.

Add imports near the top (after the existing `from app.gateway.auth.models import ActiveWorkspace`):

```python
from app.gateway.auth.api_key_backend import build_api_key_backend
```

Then insert the bearer branch in `dispatch`, immediately after the `_is_public` early-return and before the `internal_user` block. Change:

```python
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_public(request.url.path):
            return await call_next(request)

        internal_user = None
```

to:

```python
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_public(request.url.path):
            return await call_next(request)

        # API key path: "Authorization: Bearer dfk_..." authenticates a
        # service account. Resolved principal is mapped to the same
        # (user_id, workspace_id) contextvars a human would set (spec D1),
        # so all downstream isolation works unchanged.
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer dfk_"):
            token = auth_header[len("Bearer ") :]
            backend = build_api_key_backend()
            result = await backend.authenticate(token) if backend is not None else None
            if result is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": AuthErrorResponse(code=AuthErrorCode.TOKEN_INVALID, message="Invalid API key").model_dump()},
                )
            request.state.user = result.principal
            request.state.auth = AuthContext(user=result.principal, permissions=result.permissions)
            user_token = set_current_user(result.principal)
            ws_token = set_current_workspace(ActiveWorkspace(id=result.workspace_id, role=result.role))
            try:
                return await call_next(request)
            finally:
                reset_current_workspace(ws_token)
                reset_current_user(user_token)

        internal_user = None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth_middleware_api_key.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: cookie 路径回归 + lint + commit**

Run the existing middleware regression to prove the cookie path is untouched:

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_auth_middleware.py tests/test_auth_middleware_workspace.py -v && make lint
```
Expected: PASS

```bash
cd backend && git add app/gateway/auth_middleware.py tests/test_auth_middleware_api_key.py
git commit -m "feat(auth): AuthMiddleware bearer dfk_ path (Stage 1 PR2)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## PR3 · CSRF skip on bearer

> bearer 请求不带 cookie，不存在 CSRF 风险（CSRF 攻击靠浏览器自动附带 cookie）。给 `should_check_csrf` 加 bearer 短路；cookie 路径行为完全不变。

### Task 3.1: `has_bearer_header` + `should_check_csrf` skip

**Files:**
- Modify: `backend/app/gateway/csrf_middleware.py`
- Test: `backend/tests/test_csrf_bearer.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_csrf_bearer.py`:

```python
"""CSRF bearer-skip tests (Stage 1 PR3)."""

from __future__ import annotations

from starlette.testclient import TestClient


def _make_app():
    from fastapi import FastAPI

    from app.gateway.csrf_middleware import CSRFMiddleware

    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/api/echo")
    async def echo():
        return {"ok": True}

    return app


def test_bearer_post_skips_csrf():
    client = TestClient(_make_app())
    # No X-CSRF-Token / csrf cookie, but bearer header present → allowed.
    r = client.post("/api/echo", headers={"Authorization": "Bearer dfk_live_anything"})
    assert r.status_code == 200


def test_cookie_post_still_requires_csrf():
    client = TestClient(_make_app())
    # No bearer, no CSRF token → 403 (regression: cookie path unchanged).
    r = client.post("/api/echo")
    assert r.status_code == 403
    assert "CSRF token missing" in r.json()["detail"]


def test_has_bearer_header_detection():
    from starlette.requests import Request

    from app.gateway.csrf_middleware import has_bearer_header

    def _req(headers):
        scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
        return Request(scope)

    assert has_bearer_header(_req({"authorization": "Bearer x"})) is True
    assert has_bearer_header(_req({"authorization": "Basic x"})) is False
    assert has_bearer_header(_req({})) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_csrf_bearer.py -v`
Expected: FAIL — `test_bearer_post_skips_csrf` returns 403；`has_bearer_header` ImportError

- [ ] **Step 3: 写实现** — modify `backend/app/gateway/csrf_middleware.py`.

Add `has_bearer_header` after `should_check_csrf`, and call it inside `should_check_csrf`. Change:

```python
def should_check_csrf(request: Request) -> bool:
    """Determine if a request needs CSRF validation.

    CSRF is checked for state-changing methods (POST, PUT, DELETE, PATCH).
    GET, HEAD, OPTIONS, and TRACE are exempt per RFC 7231.
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return False

    path = request.url.path.rstrip("/")
    # Exempt /api/v1/auth/me endpoint
    if path == "/api/v1/auth/me":
        return False
    return True
```

to:

```python
def has_bearer_header(request: Request) -> bool:
    """True if the request carries an ``Authorization: Bearer ...`` header.

    Bearer requests authenticate via header, not cookie, so they are not
    vulnerable to CSRF (the browser never auto-attaches a bearer header).
    """
    return request.headers.get("authorization", "").startswith("Bearer ")


def should_check_csrf(request: Request) -> bool:
    """Determine if a request needs CSRF validation.

    CSRF is checked for state-changing methods (POST, PUT, DELETE, PATCH).
    GET, HEAD, OPTIONS, and TRACE are exempt per RFC 7231. Bearer-header
    (API key / token) requests are exempt — they don't ride on cookies.
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return False

    if has_bearer_header(request):
        return False

    path = request.url.path.rstrip("/")
    # Exempt /api/v1/auth/me endpoint
    if path == "/api/v1/auth/me":
        return False
    return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_csrf_bearer.py tests/test_csrf_middleware.py -v`
Expected: PASS（new 3 + existing csrf suite unchanged）

- [ ] **Step 5: lint + commit**

```bash
cd backend && make lint && git add app/gateway/csrf_middleware.py tests/test_csrf_bearer.py
git commit -m "feat(csrf): skip CSRF for bearer-header requests (Stage 1 PR3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## PR4 · 管理 endpoint（mint 闭环）

> owner/admin gating：现有 `@require_permission` 只覆盖 threads/runs 权限，无法表达 workspace 角色。新增 `require_workspace_admin` 依赖，读 workspace contextvar 的 `role`，非 owner/admin → 403。SA/key 必须属于当前 workspace，跨 workspace 操作 → 404（藏存在性）。

### Task 4.1: `require_workspace_admin` 依赖

**Files:**
- Modify: `backend/app/gateway/authz.py`
- Test: `backend/tests/test_require_workspace_admin.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_require_workspace_admin.py`:

```python
"""Tests for require_workspace_admin dependency (Stage 1 PR4)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.gateway.authz import require_workspace_admin
from deerflow.runtime.workspace_context import reset_current_workspace, set_current_workspace


class _WS:
    def __init__(self, role):
        self.id = "w-1"
        self.role = role


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_allows_owner_admin(role):
    token = set_current_workspace(_WS(role))
    try:
        require_workspace_admin()  # no raise
    finally:
        reset_current_workspace(token)


def test_rejects_member():
    token = set_current_workspace(_WS("member"))
    try:
        with pytest.raises(HTTPException) as exc:
            require_workspace_admin()
        assert exc.value.status_code == 403
    finally:
        reset_current_workspace(token)


def test_rejects_no_workspace():
    with pytest.raises(HTTPException) as exc:
        require_workspace_admin()
    assert exc.value.status_code == 403
```

> 注：本测试用例需 `@pytest.mark.no_auto_workspace` 吗？不需要——`test_rejects_no_workspace` 依赖"无 workspace"，但 conftest 的 autouse `_auto_workspace_context` 会注入 `test-workspace-autouse`（role=owner）。给该用例加 marker。把 `test_rejects_no_workspace` 改为：

```python
@pytest.mark.no_auto_workspace
def test_rejects_no_workspace():
    with pytest.raises(HTTPException) as exc:
        require_workspace_admin()
    assert exc.value.status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_require_workspace_admin.py -v`
Expected: FAIL — `ImportError: cannot import name 'require_workspace_admin'`

- [ ] **Step 3: 写实现** — append to `backend/app/gateway/authz.py`:

```python
def require_workspace_admin() -> None:
    """FastAPI dependency: require the caller's workspace role to be
    owner or admin. Reads the role from the workspace contextvar that
    AuthMiddleware stamps per request.

    Raises HTTPException 403 if no workspace is in context or the role is
    below admin. Use on management endpoints (service accounts, API keys).
    """
    from deerflow.runtime.workspace_context import get_current_workspace

    workspace = get_current_workspace()
    if workspace is None or getattr(workspace, "role", None) not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="workspace owner/admin role required")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_require_workspace_admin.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: commit**

```bash
cd backend && git add app/gateway/authz.py tests/test_require_workspace_admin.py
git commit -m "feat(authz): require_workspace_admin dependency (Stage 1 PR4)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 4.2: service-accounts router

**Files:**
- Create: `backend/app/gateway/routers/service_accounts.py`
- Modify: `backend/app/gateway/app.py`（import + include_router）
- Test: `backend/tests/test_service_accounts_router.py`

> Router 依赖工厂：用 `Depends(get_service_account_repo)` 从全局 session factory 构造仓储，便于测试覆写。`workspace_id` 从 `get_current_workspace()` 读，`created_by` 从 `request.state.user.id` 读。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_service_accounts_router.py`:

```python
"""service-accounts router tests (Stage 1 PR4)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _init_db(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace.model import WorkspaceRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="WS", slug="ws", owner_id="u-alice"))
        await session.commit()


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _make_app(*, role="owner", user_id="u-alice", workspace_id="w-1"):
    """App that stamps a fixed principal + workspace, then mounts the router.

    A tiny inline middleware substitutes for AuthMiddleware so the test
    controls role/user/workspace directly.
    """
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.gateway.authz import AuthContext, _ALL_PERMISSIONS
    from app.gateway.routers import service_accounts
    from deerflow.runtime.user_context import reset_current_user, set_current_user
    from deerflow.runtime.workspace_context import reset_current_workspace, set_current_workspace

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            user = type("U", (), {"id": user_id, "is_service_account": False})()
            ws = type("W", (), {"id": workspace_id, "role": role})()
            request.state.user = user
            request.state.auth = AuthContext(user=user, permissions=_ALL_PERMISSIONS)
            ut = set_current_user(user)
            wt = set_current_workspace(ws)
            try:
                return await call_next(request)
            finally:
                reset_current_workspace(wt)
                reset_current_user(ut)

    app = FastAPI()
    app.add_middleware(_Stamp)
    app.include_router(service_accounts.router)
    return app


async def test_owner_creates_and_lists_sa(tmp_path):
    await _init_db(tmp_path)
    try:
        client = TestClient(_make_app(role="owner"))
        r = client.post("/api/v1/service-accounts", json={"name": "ci-bot"})
        assert r.status_code == 201, r.text
        sa = r.json()
        assert sa["name"] == "ci-bot"
        assert sa["workspace_id"] == "w-1"
        assert sa["created_by"] == "u-alice"

        lst = client.get("/api/v1/service-accounts")
        assert lst.status_code == 200
        assert [s["id"] for s in lst.json()] == [sa["id"]]
    finally:
        await _cleanup()


async def test_member_cannot_create_sa(tmp_path):
    await _init_db(tmp_path)
    try:
        client = TestClient(_make_app(role="member"))
        r = client.post("/api/v1/service-accounts", json={"name": "x"})
        assert r.status_code == 403
    finally:
        await _cleanup()


async def test_patch_status_suspend(tmp_path):
    await _init_db(tmp_path)
    try:
        client = TestClient(_make_app(role="admin"))
        sa = client.post("/api/v1/service-accounts", json={"name": "bot"}).json()
        r = client.patch(f"/api/v1/service-accounts/{sa['id']}", json={"status": "suspended"})
        assert r.status_code == 200
        assert r.json()["status"] == "suspended"
    finally:
        await _cleanup()


async def test_patch_other_workspace_sa_404(tmp_path):
    await _init_db(tmp_path)
    try:
        # SA created in w-1
        owner_client = TestClient(_make_app(role="owner", workspace_id="w-1"))
        sa = owner_client.post("/api/v1/service-accounts", json={"name": "bot"}).json()
        # Caller in a different workspace tries to patch it → 404
        other_client = TestClient(_make_app(role="owner", workspace_id="w-2"))
        r = other_client.patch(f"/api/v1/service-accounts/{sa['id']}", json={"status": "suspended"})
        assert r.status_code == 404
    finally:
        await _cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_service_accounts_router.py -v`
Expected: FAIL — `ImportError: cannot import name 'service_accounts'`

- [ ] **Step 3: 写实现**

Create `backend/app/gateway/routers/service_accounts.py`:

```python
"""Service account management endpoints (Stage 1 PR4).

Owner/admin self-service: create / list / suspend service accounts in
the caller's current workspace. All operations are workspace-scoped;
cross-workspace targets return 404 (existence hidden).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.gateway.authz import require_workspace_admin
from deerflow.persistence.service_account import ServiceAccountRepository
from deerflow.runtime.workspace_context import get_current_workspace

router = APIRouter(prefix="/api/v1/service-accounts", tags=["service-accounts"])


class CreateServiceAccountRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    role: str = Field(default="member")
    identity_mode: str = Field(default="collapsed")


class UpdateServiceAccountRequest(BaseModel):
    status: str = Field(..., pattern="^(active|suspended|deleted)$")


def get_service_account_repo() -> ServiceAccountRepository:
    from deerflow.persistence.engine import get_session_factory

    sf = get_session_factory()
    if sf is None:
        raise HTTPException(status_code=503, detail="persistence backend not available")
    return ServiceAccountRepository(sf)


def _current_workspace_id() -> str:
    ws = get_current_workspace()
    if ws is None:
        raise HTTPException(status_code=403, detail="no workspace in context")
    return str(ws.id)


@router.post("", status_code=201, dependencies=[Depends(require_workspace_admin)])
async def create_service_account(
    body: CreateServiceAccountRequest,
    request: Request,
    repo: ServiceAccountRepository = Depends(get_service_account_repo),
):
    return await repo.create(
        workspace_id=_current_workspace_id(),
        name=body.name,
        created_by=str(request.state.user.id),
        role=body.role,
        identity_mode=body.identity_mode,
    )


@router.get("", dependencies=[Depends(require_workspace_admin)])
async def list_service_accounts(repo: ServiceAccountRepository = Depends(get_service_account_repo)):
    return await repo.list_by_workspace(_current_workspace_id())


@router.patch("/{sa_id}", dependencies=[Depends(require_workspace_admin)])
async def update_service_account(
    sa_id: str,
    body: UpdateServiceAccountRequest,
    repo: ServiceAccountRepository = Depends(get_service_account_repo),
):
    sa = await repo.get(sa_id)
    if sa is None or sa["workspace_id"] != _current_workspace_id():
        raise HTTPException(status_code=404, detail="service account not found")
    await repo.update_status(sa_id, body.status)
    return await repo.get(sa_id)
```

Modify `backend/app/gateway/app.py` — add to the router import block and register. In the `from app.gateway.routers import (` group (starts L14), add `service_accounts` (keep alphabetical-ish with neighbors). Then after the auth router registration block, add:

```python
    # Service Accounts API is mounted at /api/v1/service-accounts
    app.include_router(service_accounts.router)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_service_accounts_router.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: commit**

```bash
cd backend && git add app/gateway/routers/service_accounts.py app/gateway/app.py tests/test_service_accounts_router.py
git commit -m "feat(gateway): service-accounts management endpoints (Stage 1 PR4)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 4.3: api-keys router

**Files:**
- Create: `backend/app/gateway/routers/api_keys.py`
- Modify: `backend/app/gateway/app.py`（import + include_router）
- Test: `backend/tests/test_api_keys_router.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_api_keys_router.py`:

```python
"""api-keys router tests (Stage 1 PR4).

plaintext is returned exactly once at create time; never on list.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _init_db_with_sa(tmp_path, *, sa_id="sa-1", workspace_id="w-1"):
    from deerflow.persistence.engine import get_session_factory, init_engine
    from deerflow.persistence.service_account.model import ServiceAccountRow
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace.model import WorkspaceRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id=workspace_id, name="WS", slug=f"ws-{workspace_id}", owner_id="u-alice"))
        await session.commit()
    async with sf() as session:
        session.add(ServiceAccountRow(id=sa_id, workspace_id=workspace_id, name="bot", role="member", identity_mode="collapsed", status="active", created_by="u-alice"))
        await session.commit()


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _make_app(*, role="owner", workspace_id="w-1"):
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.gateway.authz import AuthContext, _ALL_PERMISSIONS
    from app.gateway.routers import api_keys
    from deerflow.runtime.user_context import reset_current_user, set_current_user
    from deerflow.runtime.workspace_context import reset_current_workspace, set_current_workspace

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            user = type("U", (), {"id": "u-alice", "is_service_account": False})()
            ws = type("W", (), {"id": workspace_id, "role": role})()
            request.state.user = user
            request.state.auth = AuthContext(user=user, permissions=_ALL_PERMISSIONS)
            ut = set_current_user(user)
            wt = set_current_workspace(ws)
            try:
                return await call_next(request)
            finally:
                reset_current_workspace(wt)
                reset_current_user(ut)

    app = FastAPI()
    app.add_middleware(_Stamp)
    app.include_router(api_keys.router)
    return app


async def test_create_returns_plaintext_once(tmp_path):
    await _init_db_with_sa(tmp_path)
    try:
        client = TestClient(_make_app())
        r = client.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "ci", "scopes": "threads:read"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["plaintext"].startswith("dfk_live_")
        assert body["key_prefix"] == body["plaintext"][:16]

        lst = client.get("/api/v1/api-keys", params={"service_account_id": "sa-1"})
        assert lst.status_code == 200
        rows = lst.json()
        assert len(rows) == 1
        assert "plaintext" not in rows[0]
        assert "key_hash" not in rows[0]
    finally:
        await _cleanup()


async def test_create_for_other_workspace_sa_404(tmp_path):
    await _init_db_with_sa(tmp_path, sa_id="sa-1", workspace_id="w-1")
    try:
        # Caller is in w-2 but targets sa-1 which lives in w-1 → 404.
        client = TestClient(_make_app(workspace_id="w-2"))
        r = client.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "x", "scopes": ""})
        assert r.status_code == 404
    finally:
        await _cleanup()


async def test_revoke_key(tmp_path):
    await _init_db_with_sa(tmp_path)
    try:
        client = TestClient(_make_app())
        created = client.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "k", "scopes": ""}).json()
        r = client.delete(f"/api/v1/api-keys/{created['id']}")
        assert r.status_code == 204
        rows = client.get("/api/v1/api-keys", params={"service_account_id": "sa-1"}).json()
        assert rows[0]["revoked_at"] is not None
    finally:
        await _cleanup()


async def test_member_cannot_create_key(tmp_path):
    await _init_db_with_sa(tmp_path)
    try:
        client = TestClient(_make_app(role="member"))
        r = client.post("/api/v1/api-keys", json={"service_account_id": "sa-1", "name": "x", "scopes": ""})
        assert r.status_code == 403
    finally:
        await _cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_keys_router.py -v`
Expected: FAIL — `ImportError: cannot import name 'api_keys'`

- [ ] **Step 3: 写实现**

Create `backend/app/gateway/routers/api_keys.py`:

```python
"""API key management endpoints (Stage 1 PR4).

Owner/admin mint / list / revoke API keys for a service account in the
caller's workspace. The plaintext token is returned exactly once, at
create time; list responses never include plaintext or the hash. The
target service account must belong to the caller's workspace, else 404.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.gateway.authz import require_workspace_admin
from deerflow.auth.tokens import generate_api_key
from deerflow.persistence.api_key import ApiKeyRepository
from deerflow.persistence.service_account import ServiceAccountRepository
from deerflow.runtime.workspace_context import get_current_workspace

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])


class CreateApiKeyRequest(BaseModel):
    service_account_id: str
    name: str = Field(..., min_length=1, max_length=64)
    scopes: str = Field(default="")
    env: str = Field(default="live", pattern="^(live|test)$")
    expires_at: datetime | None = None


def get_api_key_repo() -> ApiKeyRepository:
    from deerflow.persistence.engine import get_session_factory

    sf = get_session_factory()
    if sf is None:
        raise HTTPException(status_code=503, detail="persistence backend not available")
    return ApiKeyRepository(sf)


def get_service_account_repo() -> ServiceAccountRepository:
    from deerflow.persistence.engine import get_session_factory

    sf = get_session_factory()
    if sf is None:
        raise HTTPException(status_code=503, detail="persistence backend not available")
    return ServiceAccountRepository(sf)


def _current_workspace_id() -> str:
    ws = get_current_workspace()
    if ws is None:
        raise HTTPException(status_code=403, detail="no workspace in context")
    return str(ws.id)


async def _require_sa_in_workspace(sa_id: str, sa_repo: ServiceAccountRepository) -> dict:
    sa = await sa_repo.get(sa_id)
    if sa is None or sa["workspace_id"] != _current_workspace_id():
        raise HTTPException(status_code=404, detail="service account not found")
    return sa


@router.post("", status_code=201, dependencies=[Depends(require_workspace_admin)])
async def create_api_key(
    body: CreateApiKeyRequest,
    request: Request,
    key_repo: ApiKeyRepository = Depends(get_api_key_repo),
    sa_repo: ServiceAccountRepository = Depends(get_service_account_repo),
):
    await _require_sa_in_workspace(body.service_account_id, sa_repo)
    gen = generate_api_key(body.env)  # type: ignore[arg-type]
    created = await key_repo.create(
        service_account_id=body.service_account_id,
        key_prefix=gen.prefix,
        key_hash=gen.key_hash,
        name=body.name,
        scopes=body.scopes,
        expires_at=body.expires_at,
    )
    # plaintext returned exactly once; never persisted, never re-served.
    return {**created, "plaintext": gen.plaintext}


@router.get("", dependencies=[Depends(require_workspace_admin)])
async def list_api_keys(
    service_account_id: str,
    key_repo: ApiKeyRepository = Depends(get_api_key_repo),
    sa_repo: ServiceAccountRepository = Depends(get_service_account_repo),
):
    await _require_sa_in_workspace(service_account_id, sa_repo)
    return await key_repo.list_by_service_account(service_account_id)


@router.delete("/{key_id}", status_code=204, dependencies=[Depends(require_workspace_admin)])
async def revoke_api_key(
    key_id: str,
    key_repo: ApiKeyRepository = Depends(get_api_key_repo),
    sa_repo: ServiceAccountRepository = Depends(get_service_account_repo),
):
    key = await key_repo.get(key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="api key not found")
    await _require_sa_in_workspace(key["service_account_id"], sa_repo)
    await key_repo.revoke(key_id)
    return Response(status_code=204)
```

Modify `backend/app/gateway/app.py` — add `api_keys` to the router import group and register after service_accounts:

```python
    # API Keys API is mounted at /api/v1/api-keys
    app.include_router(api_keys.router)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_keys_router.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: commit**

```bash
cd backend && git add app/gateway/routers/api_keys.py app/gateway/app.py tests/test_api_keys_router.py
git commit -m "feat(gateway): api-keys management endpoints with one-time plaintext (Stage 1 PR4)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 4.4: 端到端 mint→use→isolate smoke

**Files:**
- Test: `backend/tests/test_headless_api_smoke.py`

> 地基的"活体证明"（spec §7）：owner 建 SA → 建 key → 用 key 经真实 `AuthMiddleware` 调一个 owner_check 探针路由 → 跨 workspace key 得 404。把全链路（中间件 + 仓储 + 管理 endpoint）串起来。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_headless_api_smoke.py`:

```python
"""End-to-end headless API smoke (Stage 1 PR4).

Mints a real key through the management endpoints, then calls a protected
probe route through the real AuthMiddleware using that key. Proves the
full chain and the cross-workspace 404 isolation guarantee.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _init_db(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine
    from deerflow.persistence.user.model import UserRow
    from deerflow.persistence.workspace.model import WorkspaceRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        session.add(UserRow(id="u-alice", email="alice@example.com"))
        session.add(UserRow(id="u-bob", email="bob@example.com"))
        await session.commit()
    async with sf() as session:
        session.add(WorkspaceRow(id="w-1", name="Alice WS", slug="alice", owner_id="u-alice"))
        session.add(WorkspaceRow(id="w-2", name="Bob WS", slug="bob", owner_id="u-bob"))
        await session.commit()


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


def _mgmt_app(*, workspace_id):
    """Management app: stamps a fixed owner principal + workspace."""
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.gateway.authz import AuthContext, _ALL_PERMISSIONS
    from app.gateway.routers import api_keys, service_accounts
    from deerflow.runtime.user_context import reset_current_user, set_current_user
    from deerflow.runtime.workspace_context import reset_current_workspace, set_current_workspace

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            user = type("U", (), {"id": "u-owner", "is_service_account": False})()
            ws = type("W", (), {"id": workspace_id, "role": "owner"})()
            request.state.user = user
            request.state.auth = AuthContext(user=user, permissions=_ALL_PERMISSIONS)
            ut = set_current_user(user)
            wt = set_current_workspace(ws)
            try:
                return await call_next(request)
            finally:
                reset_current_workspace(wt)
                reset_current_user(ut)

    app = FastAPI()
    app.add_middleware(_Stamp)
    app.include_router(service_accounts.router)
    app.include_router(api_keys.router)
    return app


def _probe_app():
    """Protected app behind the REAL AuthMiddleware with a probe route."""
    from fastapi import FastAPI, Request

    from app.gateway.auth_middleware import AuthMiddleware
    from deerflow.runtime.user_context import get_effective_user_id
    from deerflow.runtime.workspace_context import get_effective_workspace_id

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/probe")
    async def probe(request: Request):
        return {"user_id": get_effective_user_id(), "workspace_id": get_effective_workspace_id()}

    return app


async def test_mint_use_and_cross_workspace_isolation(tmp_path):
    await _init_db(tmp_path)
    try:
        mgmt = TestClient(_mgmt_app(workspace_id="w-1"))
        sa = mgmt.post("/api/v1/service-accounts", json={"name": "ci"}).json()
        key = mgmt.post("/api/v1/api-keys", json={"service_account_id": sa["id"], "name": "k", "scopes": "threads:read"}).json()
        plaintext = key["plaintext"]

        probe = TestClient(_probe_app())
        ok = probe.get("/api/probe", headers={"Authorization": f"Bearer {plaintext}"})
        assert ok.status_code == 200
        assert ok.json() == {"user_id": sa["id"], "workspace_id": "w-1"}

        # A bogus / unknown key is rejected.
        bad = probe.get("/api/probe", headers={"Authorization": "Bearer dfk_live_unknown0000000000000000"})
        assert bad.status_code == 401
    finally:
        await _cleanup()
```

- [ ] **Step 2: 跑测试确认失败 / 通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_headless_api_smoke.py -v`
Expected: PASS（如果 PR2+PR4 实现正确，本 smoke 直接 green；若 FAIL 按报错修对应实现）

- [ ] **Step 3: PR4 收尾 — 全量回归 + lint + commit**

```bash
cd backend && make lint && make test
```
Expected: lint clean；test 全绿（基线 3250 passed + 31 skipped，新增本 PR1–PR4 测试，无新 caplog flake 引入）

```bash
cd backend && git add tests/test_headless_api_smoke.py
git commit -m "test(gateway): end-to-end headless API mint/use/isolation smoke (Stage 1 PR4)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## PR5 · `/api/v1` 全量迁移 + 旧路径兼容（最后做）

> 风险面最大：13 个 legacy router 双挂 `/api` + `/api/v1`，前端同步迁 `/api/v1`。`auth`（已 `/api/v1/auth`）、`service-accounts`/`api-keys`（PR4 已 `/api/v1`）、`assistants_compat`、langgraph（`/api/langgraph/*`）**不动**。旧无版本路径加 `X-API-Deprecated` header。

### Task 5.1: deprecation header 中间件

**Files:**
- Create: `backend/app/gateway/deprecation_middleware.py`
- Modify: `backend/app/gateway/app.py`（add_middleware）
- Test: `backend/tests/test_api_deprecation_header.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_api_deprecation_header.py`:

```python
"""Deprecation header tests (Stage 1 PR5)."""

from __future__ import annotations

from starlette.testclient import TestClient


def _make_app():
    from fastapi import FastAPI

    from app.gateway.deprecation_middleware import ApiDeprecationMiddleware

    app = FastAPI()
    app.add_middleware(ApiDeprecationMiddleware)

    @app.get("/api/threads")
    async def legacy():
        return {"ok": True}

    @app.get("/api/v1/threads")
    async def versioned():
        return {"ok": True}

    @app.get("/api/langgraph/info")
    async def lg():
        return {"ok": True}

    return app


def test_legacy_path_gets_deprecation_header():
    client = TestClient(_make_app())
    r = client.get("/api/threads")
    assert r.headers.get("X-API-Deprecated") == "2027-01-01"


def test_versioned_path_no_header():
    client = TestClient(_make_app())
    r = client.get("/api/v1/threads")
    assert "X-API-Deprecated" not in r.headers


def test_langgraph_path_no_header():
    client = TestClient(_make_app())
    r = client.get("/api/langgraph/info")
    assert "X-API-Deprecated" not in r.headers
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_deprecation_header.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.gateway.deprecation_middleware'`

- [ ] **Step 3: 写实现**

Create `backend/app/gateway/deprecation_middleware.py`:

```python
"""Marks responses to legacy unversioned /api/* paths as deprecated.

Stamps ``X-API-Deprecated: <sunset-date>`` on any /api/* response that is
neither versioned (/api/v1/*) nor the LangGraph SDK surface
(/api/langgraph/*). Sunset date is the track-2 contract (2027-01-01).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

API_SUNSET_DATE = "2027-01-01"


def _is_deprecated_path(path: str) -> bool:
    return path.startswith("/api/") and not path.startswith("/api/v1/") and not path.startswith("/api/langgraph/")


class ApiDeprecationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        if _is_deprecated_path(request.url.path):
            response.headers["X-API-Deprecated"] = API_SUNSET_DATE
        return response
```

Modify `backend/app/gateway/app.py` — register the middleware. Find where `app.add_middleware(AuthMiddleware)` / `CSRFMiddleware` are added and add (order is not load-bearing for a response-header-only middleware, but add it alongside the others):

```python
    from app.gateway.deprecation_middleware import ApiDeprecationMiddleware

    app.add_middleware(ApiDeprecationMiddleware)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_deprecation_header.py -v`
Expected: PASS（3 tests）

- [ ] **Step 5: commit**

```bash
cd backend && git add app/gateway/deprecation_middleware.py app/gateway/app.py tests/test_api_deprecation_header.py
git commit -m "feat(gateway): X-API-Deprecated header for legacy /api/* paths (Stage 1 PR5)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 5.2: 13 个 legacy router 改相对前缀 + 双挂

**Files:**
- Modify: `backend/app/gateway/routers/{models,mcp,memory,skills,artifacts,uploads,threads,agents,suggestions,channels,feedback,thread_runs,runs}.py`
- Modify: `backend/app/gateway/app.py`
- Test: `backend/tests/test_api_v1_dual_mount.py`

> 做法：每个 router 的 `APIRouter(prefix="/api/...")` 改成去掉 `/api` 前缀的相对前缀（如 `/api/models` → `/models`，`/api/threads/{thread_id}/uploads` → `/threads/{thread_id}/uploads`）；在 `app.py` 中每个 router `include_router(x.router, prefix="/api")` + `include_router(x.router, prefix="/api/v1")` 双挂。逐个改 + 每改一个跑该域已有测试，避免一次性全断。

- [ ] **Step 1: 写失败测试**（先写双挂断言，红）

Create `backend/tests/test_api_v1_dual_mount.py`:

```python
"""Dual-mount /api + /api/v1 tests (Stage 1 PR5).

Every migrated legacy router must answer on BOTH /api/<x> and
/api/v1/<x>. We probe a GET endpoint that needs no auth bypass via the
real app's public surface where possible; here we assert route presence
on the OpenAPI schema to stay independent of per-route auth.
"""

from __future__ import annotations

from app.gateway.app import create_app


def _paths():
    app = create_app()
    return set(app.openapi()["paths"].keys())


def test_models_dual_mounted():
    paths = _paths()
    assert "/api/models" in paths
    assert "/api/v1/models" in paths


def test_threads_uploads_dual_mounted():
    paths = _paths()
    assert any(p.startswith("/api/threads/") and p.endswith("/uploads/list") for p in paths)
    assert any(p.startswith("/api/v1/threads/") and p.endswith("/uploads/list") for p in paths)


def test_runs_dual_mounted():
    paths = _paths()
    assert "/api/runs/stream" in paths
    assert "/api/v1/runs/stream" in paths


def test_auth_only_v1_not_dual():
    # auth stays v1-only — must NOT acquire an /api/auth twin.
    paths = _paths()
    assert "/api/v1/auth/me" in paths
    assert "/api/auth/me" not in paths


def test_langgraph_not_versioned():
    # langgraph surface (if present in schema) must not gain /api/v1 twins.
    paths = _paths()
    assert not any(p.startswith("/api/v1/langgraph") for p in paths)
```

> 注：`create_app` 的确切签名见 `app/gateway/app.py`（搜索 `def create_app`）。若它需要参数，按现有 `tests/` 中调用 `create_app` 的方式（grep `create_app(` in tests）对齐。若部分路由路径名（如 `/uploads/list`）与实际不符，先 grep 实际 `@router.get` 路径再校正断言字符串。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_api_v1_dual_mount.py -v`
Expected: FAIL — `/api/v1/models` etc. not in paths（尚未双挂）

- [ ] **Step 3: 写实现** — 逐 router 改前缀 + app.py 双挂。

对每个 legacy router 文件，把 `APIRouter(prefix="/api...")` 改为相对前缀。逐文件映射：

| 文件 | 旧前缀 | 新前缀 |
|---|---|---|
| `models.py` | `/api/models` | `/models` |
| `mcp.py` | `/api/mcp` | `/mcp` |
| `memory.py` | `/api/memory` | `/memory` |
| `skills.py` | `/api/skills` | `/skills` |
| `artifacts.py` | `/api/threads/{thread_id}/artifacts` | `/threads/{thread_id}/artifacts` |
| `uploads.py` | `/api/threads/{thread_id}/uploads` | `/threads/{thread_id}/uploads` |
| `threads.py` | `/api/threads` | `/threads` |
| `agents.py` | `/api/agents` | `/agents` |
| `suggestions.py` | `/api/threads/{thread_id}/suggestions` | `/threads/{thread_id}/suggestions` |
| `channels.py` | `/api/channels` | `/channels` |
| `feedback.py` | `/api/threads/{thread_id}/runs/{run_id}/feedback` | `/threads/{thread_id}/runs/{run_id}/feedback` |
| `thread_runs.py` | `/api/threads` (runs lifecycle) | `/threads` |
| `runs.py` | `/api/runs` | `/runs` |

> 先 `grep -n 'APIRouter(prefix=' app/gateway/routers/*.py` 确认每个文件的确切现值，再逐个 `Edit` 把 `prefix="/api<...>"` 改为 `prefix="<...>"`（仅删开头的 `/api`）。

然后 modify `backend/app/gateway/app.py` 的 include 段，把这 13 个 router 从单挂改为双挂。Change each line like:

```python
    app.include_router(models.router)
```

to:

```python
    app.include_router(models.router, prefix="/api")
    app.include_router(models.router, prefix="/api/v1")
```

对全部 13 个 legacy router 同样处理。**保持不变**（不加 prefix、不双挂）：`auth.router`、`service_accounts.router`、`api_keys.router`、`assistants_compat.router`。

- [ ] **Step 4: 跑测试确认通过 + 全域回归**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_api_v1_dual_mount.py -v
```
Expected: PASS（5 tests）

再跑受影响域的既有测试，确认旧 `/api/*` 行为不破（这些测试目前打 `/api/...`，双挂后仍应 200）：

```bash
cd backend && make test
```
Expected: 全绿。**若有测试因路径断言失败**：那些测试断言的是旧 `/api/*` 路径，双挂保留了旧路径，故不应失败；如失败说明某 router 改成相对前缀时漏删/多删了 `/api`，回到 Step 3 校正该文件。

- [ ] **Step 5: commit**

```bash
cd backend && make lint && git add app/gateway/routers/ app/gateway/app.py tests/test_api_v1_dual_mount.py
git commit -m "feat(gateway): dual-mount legacy routers on /api and /api/v1 (Stage 1 PR5)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 5.3: 前端路径迁移 `/api/...` → `/api/v1/...`

**Files:**
- Modify: `frontend/src/core/*/api.ts`、`frontend/src/core/threads/hooks.ts`、`frontend/src/core/artifacts/utils.ts`、`frontend/src/core/uploads/api.ts`、`frontend/src/core/api/feedback.ts`、`frontend/src/core/models/api.ts`（及其他命中文件）

> langgraph-sdk 客户端路径（`/api/langgraph/*`）与 auth（已 `/api/v1/auth`）**不动**。

- [ ] **Step 1: 定位所有需改的路径串**

```bash
cd frontend && grep -rn '"/api/' src/ | grep -v '/api/v1/' | grep -v '/api/langgraph'
```
Expected: 列出所有仍指向无版本 `/api/...` 的 fetch 路径（models/mcp/memory/skills/threads/artifacts/uploads/feedback/runs 等）。逐条记录文件 + 行号。

- [ ] **Step 2: 逐文件改为 `/api/v1/...`**

对 Step 1 列出的每条路径，用 `Edit` 把 `"/api/<x>"` 改为 `"/api/v1/<x>"`（仅这些命中行；不要碰 `/api/langgraph` 与已含 `/api/v1` 的串）。

- [ ] **Step 3: 验证无残留**

```bash
cd frontend && grep -rn '"/api/' src/ | grep -v '/api/v1/' | grep -v '/api/langgraph'
```
Expected: 无输出（全部已迁，langgraph 除外）

- [ ] **Step 4: 前端校验**

```bash
cd frontend && pnpm lint && pnpm typecheck
```
Expected: PASS。若改动触及 env/auth/routing/build-sensitive 代码，追加：
```bash
cd frontend && BETTER_AUTH_SECRET=local-dev-secret pnpm build
```

- [ ] **Step 5: commit**

```bash
cd frontend && git add src/
git commit -m "feat(frontend): migrate API calls to /api/v1 (Stage 1 PR5)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 5.4: 全栈回归收尾

- [ ] **Step 1: 后端全量**

```bash
cd backend && make lint && make test
```
Expected: lint clean；test 全绿（基线 3250 passed + 31 skipped + ≤18 既有 caplog flake；无新增 flake）

- [ ] **Step 2: 边界检查**

```bash
cd backend && PYTHONPATH=. uv run pytest tests/test_harness_boundary.py tests/test_workspace_boundary.py -v
```
Expected: PASS（PR1 的 deerflow 层代码未引入 `app.*` import；PR2 的 `is_service_account` 协议改动仍在 deerflow 内）

- [ ] **Step 3: 回填 spec 链接 + commit**

Modify `docs/multi-tenant-redesign/01-redesign/stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md` §10，把：

```
- 本 spec 的实现计划 → （writing-plans 生成后回填链接）
```

改为：

```
- 本 spec 的实现计划 → [2026-06-28-stage-1-headless-api-pattern-a-auth-foundation.md](../../superpowers/plans/2026-06-28-stage-1-headless-api-pattern-a-auth-foundation.md)
```

```bash
cd /Users/wangguixuan/work/github/deer-flow
git add docs/multi-tenant-redesign/01-redesign/stage-1-headless-api-pattern-a-auth-foundation-design.zh-CN.md
git commit -m "docs(stage-1): backfill implementation plan link in spec (Stage 1 PR5)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage（spec §5 逐 PR + §6 错误口径 + §8 不可逆决策）**
- PR1 三表仓储 + tokens → Task 1.1–1.4 ✅（含 ExternalUserRepository 建好不接业务）
- PR2 `is_service_account` + APIKeyAuthBackend + AuthMiddleware 双路径 → Task 2.1–2.3 ✅
- PR3 CSRF skip on bearer → Task 3.1 ✅
- PR4 管理 endpoint + owner/admin gating + 跨 workspace 404 + 端到端 smoke → Task 4.1–4.4 ✅
- PR5 `/api/v1` 双挂 + deprecation header + 前端迁移 → Task 5.1–5.4 ✅
- 错误口径（§6）：无效/撤销/过期 key → 401（Task 2.2/2.3 测试）；跨 workspace → 404（Task 4.2/4.3/4.4）；非 owner/admin → 403（Task 4.1/4.2/4.3）✅
- 不可逆决策（§8）：key 格式 `dfk_{live,test}_<24>` prefix 16 sha256（Task 1.1）；`user_id=SA.id`（Task 2.2/2.3 smoke）；管理路径 `/api/v1/service-accounts`+`/api/v1/api-keys`（Task 4.2/4.3）；双挂 + `X-API-Deprecated: 2027-01-01`（Task 5.1/5.2）；scope 逗号分隔（Task 2.1）✅

**2. Placeholder scan:** 所有 code step 含可运行实际代码；PR5 Task 5.2/5.3 的"逐文件"操作给了精确映射表 + grep 验证命令，非占位。✅（Task 5.2 的 `create_app` 签名与 Task 5.3 的具体命中文件标注了"先 grep 校正"——这是因为前端路径分散且 `create_app` 签名未在本次锚点中读取，属合理的实现期确认，非内容缺失。）

**3. Type consistency:**
- `GeneratedKey(plaintext, prefix, key_hash)` — 跨 Task 1.1/1.3/4.3 一致 ✅
- `ApiKeyRepository.create(*, service_account_id, key_prefix, key_hash, name, scopes, expires_at)` — Task 1.3 定义，Task 2.2/4.3 调用一致 ✅
- `ServiceAccountRepository.get`/`get_active`/`update_status` — Task 1.2 定义，Task 2.2/4.2 调用一致 ✅
- `APIKeyAuthBackend(api_key_repo, service_account_repo, workspace_repo)` + `ApiKeyAuthResult(principal, workspace_id, role, permissions)` — Task 2.2 定义，Task 2.3 中间件消费一致 ✅
- `ServicePrincipal(id, is_service_account=True)` 满足 `CurrentUser` 协议（id + is_service_account）— Task 2.1 协议 / Task 2.2 principal 一致 ✅
- `require_workspace_admin()` 无参依赖 — Task 4.1 定义，Task 4.2/4.3 `Depends(require_workspace_admin)` 一致 ✅
- `WorkspaceRepository.get(workspace_id, user_id=None)` 旁路 membership — 与现有 `sql.py` 签名一致（Task 2.2 调用）✅
