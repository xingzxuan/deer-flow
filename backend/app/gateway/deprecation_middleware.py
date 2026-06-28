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
