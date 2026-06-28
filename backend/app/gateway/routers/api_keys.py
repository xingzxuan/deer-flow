"""API key management endpoints (Stage 1 PR4).

Owner/admin mint / list / revoke API keys for a service account in the
caller's workspace. The plaintext token is returned exactly once, at
create time; list responses never include plaintext or the hash. The
target service account must belong to the caller's workspace, else 404.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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
    env: Literal["live", "test"] = "live"
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
    gen = generate_api_key(body.env)
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
