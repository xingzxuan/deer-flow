"""Dual-mount /api + /api/v1 tests (Stage 1 PR5).

Every migrated legacy router must answer on BOTH /api/<x> and /api/v1/<x>.
Asserts route presence on the OpenAPI schema (independent of per-route auth).
"""

from __future__ import annotations

from app.gateway.app import create_app


def _paths():
    return set(create_app().openapi()["paths"].keys())


def test_models_dual_mounted():
    paths = _paths()
    assert "/api/models" in paths
    assert "/api/v1/models" in paths


def test_runs_dual_mounted():
    paths = _paths()
    assert "/api/runs/stream" in paths
    assert "/api/v1/runs/stream" in paths


def test_threads_dual_mounted():
    paths = _paths()
    # at least one threads sub-path must exist on both surfaces
    assert any(p.startswith("/api/threads/") for p in paths)
    assert any(p.startswith("/api/v1/threads/") for p in paths)


def test_uploads_dual_mounted():
    paths = _paths()
    assert any(p.startswith("/api/threads/") and "/uploads" in p for p in paths)
    assert any(p.startswith("/api/v1/threads/") and "/uploads" in p for p in paths)


def test_auth_only_v1_not_dual():
    # auth stays v1-only — must NOT acquire an /api/auth twin.
    paths = _paths()
    assert "/api/v1/auth/me" in paths
    assert "/api/auth/me" not in paths


def test_no_v1_langgraph_twins():
    paths = _paths()
    assert not any(p.startswith("/api/v1/langgraph") for p in paths)
    assert not any(p.startswith("/api/v1/assistants") for p in paths)
