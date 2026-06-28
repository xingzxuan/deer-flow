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


@pytest.mark.no_auto_workspace
def test_rejects_no_workspace():
    with pytest.raises(HTTPException) as exc:
        require_workspace_admin()
    assert exc.value.status_code == 403
