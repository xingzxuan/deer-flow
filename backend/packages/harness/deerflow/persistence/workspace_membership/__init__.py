"""Workspace membership persistence — ORM model + repository.

Tracks who is a member of which workspace and what their role is
within that workspace. Composite primary key ``(workspace_id, user_id)``;
``role`` follows the three-state ``owner`` / ``admin`` / ``member`` model
(Stage 0 only writes ``'owner'``; Stage 2 RBAC rollout opens admin/member).

A workspace MUST have exactly one ``owner`` — enforced by a partial
unique index. Owner transfer is a two-row transactional swap.
"""

from __future__ import annotations

from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow

__all__ = ["WorkspaceMembershipRow"]
