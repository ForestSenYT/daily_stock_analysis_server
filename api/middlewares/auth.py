# -*- coding: utf-8 -*-
"""
Auth middleware: protect /api/v1/* when admin auth is enabled.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth import COOKIE_NAME, is_auth_enabled, verify_session

logger = logging.getLogger(__name__)

EXEMPT_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/status",
    "/api/health",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
})
AUTH_PREFIX = "/api/v1/auth/"
PUBLIC_AUTH_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/status",
})


def _path_exempt(path: str) -> bool:
    """Check if path is exempt from auth."""
    normalized = path.rstrip("/") or "/"
    return normalized in EXEMPT_PATHS


def _has_valid_admin_session(request: Request) -> bool:
    """Accept either Cloud Run admin session or legacy API auth session."""
    try:
        if request.session.get("is_admin") is True:
            return True
    except (AssertionError, RuntimeError):
        pass

    cookie_val = request.cookies.get(COOKIE_NAME)
    return bool(cookie_val and verify_session(cookie_val))


class AuthMiddleware(BaseHTTPMiddleware):
    """Require valid session for /api/v1/* when auth is enabled."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ):
        path = request.url.path
        if _path_exempt(path):
            return await call_next(request)

        if not path.startswith("/api/v1/"):
            return await call_next(request)

        normalized_path = path.rstrip("/") or "/"
        if normalized_path in PUBLIC_AUTH_PATHS:
            return await call_next(request)

        protected_auth_mutation = path.rstrip("/").startswith(AUTH_PREFIX)
        if not is_auth_enabled() and not protected_auth_mutation:
            return await call_next(request)

        if not _has_valid_admin_session(request):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Login required",
                },
            )

        return await call_next(request)


def add_auth_middleware(app):
    """Add auth middleware to protect API routes.

    The middleware is always registered; whether auth is enforced is determined
    at request time by is_auth_enabled() so the decision stays consistent across
    any runtime configuration reload.
    """
    app.add_middleware(AuthMiddleware)
