"""Workspace persistence — ORM model + repository.

A workspace is the multi-tenant scope unit. Every user has at least
one (auto-created on registration; their personal workspace where
they are sole owner). Team plans get multi-member workspaces.

Stage 0 PR3 introduces the schema + repository; PR4 wires it into
the registration / login flow; PR5+ ALTER existing business tables
to FK back to ``workspaces.id``.
"""

from __future__ import annotations

from deerflow.persistence.workspace.model import WorkspaceRow
from deerflow.persistence.workspace.sql import WorkspaceRepository, WorkspaceValidationError

__all__ = ["WorkspaceRepository", "WorkspaceRow", "WorkspaceValidationError"]
