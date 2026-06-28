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
