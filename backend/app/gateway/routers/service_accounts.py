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
