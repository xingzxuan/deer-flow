"""Abstract interface for thread metadata storage.

Implementations:
- ThreadMetaRepository: SQL-backed (sqlite / postgres via SQLAlchemy)
- MemoryThreadMetaStore: wraps LangGraph BaseStore (memory mode)

All mutating and querying methods accept both a ``user_id`` parameter
(member-scoped owner check) and a ``workspace_id`` parameter (tenant
scope). Both follow three-state semantics:

- ``AUTO`` (default): resolve from the request-scoped contextvar.
- Explicit ``str``: use the provided value verbatim.
- Explicit ``None``: bypass that filter (migration / CLI only).

The workspace scope is the **outer** boundary: a row in workspace A is
unreachable from any user_id under workspace B. ``check_access`` returns
False on cross-workspace mismatch so the route layer can convert it into
a 404 instead of leaking thread existence across tenants.
"""

from __future__ import annotations

import abc

from deerflow.runtime.user_context import AUTO, _AutoSentinel
from deerflow.runtime.workspace_context import AUTO as WORKSPACE_AUTO
from deerflow.runtime.workspace_context import _AutoSentinel as _WorkspaceAutoSentinel


class ThreadMetaStore(abc.ABC):
    @abc.abstractmethod
    async def create(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        user_id: str | None | _AutoSentinel = AUTO,
        workspace_id: str | None | _WorkspaceAutoSentinel = WORKSPACE_AUTO,
        display_name: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        pass

    @abc.abstractmethod
    async def get(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        workspace_id: str | None | _WorkspaceAutoSentinel = WORKSPACE_AUTO,
    ) -> dict | None:
        pass

    @abc.abstractmethod
    async def search(
        self,
        *,
        metadata: dict | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None | _AutoSentinel = AUTO,
        workspace_id: str | None | _WorkspaceAutoSentinel = WORKSPACE_AUTO,
    ) -> list[dict]:
        pass

    @abc.abstractmethod
    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        workspace_id: str | None | _WorkspaceAutoSentinel = WORKSPACE_AUTO,
    ) -> None:
        pass

    @abc.abstractmethod
    async def update_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        workspace_id: str | None | _WorkspaceAutoSentinel = WORKSPACE_AUTO,
    ) -> None:
        pass

    @abc.abstractmethod
    async def update_metadata(
        self,
        thread_id: str,
        metadata: dict,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        workspace_id: str | None | _WorkspaceAutoSentinel = WORKSPACE_AUTO,
    ) -> None:
        """Merge ``metadata`` into the thread's metadata field.

        Existing keys are overwritten by the new values; keys absent from
        ``metadata`` are preserved. No-op if the thread does not exist
        or the user/workspace check fails.
        """
        pass

    @abc.abstractmethod
    async def check_access(
        self,
        thread_id: str,
        user_id: str,
        workspace_id: str,
        *,
        require_existing: bool = False,
    ) -> bool:
        """Check whether ``user_id`` (in ``workspace_id``) can access ``thread_id``.

        Cross-workspace access returns ``False`` unconditionally so the
        decorator layer can convert it into a 404 — never leak the
        existence of a thread that belongs to a different tenant.
        """
        pass

    @abc.abstractmethod
    async def delete(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        workspace_id: str | None | _WorkspaceAutoSentinel = WORKSPACE_AUTO,
    ) -> None:
        pass
