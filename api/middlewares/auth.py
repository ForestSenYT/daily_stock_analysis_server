# -*- coding: utf-8 -*-
"""
Auth middleware: protect /api/v1/* when admin auth is enabled.
"""

from __future__ import annotations

import ipaddress
import logging
import os
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
SENSITIVE_API_PREFIXES = (
    "/api/v1/system/config",
    "/api/v1/broker/firstrade",
    "/api/v1/trading",
    "/api/v1/ai-sandbox",
    "/api/v1/ai-training",
)


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


def _is_loopback_client(request: Request) -> bool:
    """Allow unauthenticated sensitive APIs only for local/dev clients."""
    if (
        os.getenv("K_SERVICE")
        or os.getenv("K_REVISION")
        or os.getenv("K_CONFIGURATION")
    ):
        return False
    host = request.client.host if request.client else ""
    if host == "testclient":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_sensitive_api_path(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return any(normalized == prefix or normalized.startswith(prefix + "/")
               for prefix in SENSITIVE_API_PREFIXES)


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
        auth_enabled = is_auth_enabled()
        if not auth_enabled and _is_sensitive_api_path(path) and not _is_loopback_client(request):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Admin authentication is required for this endpoint.",
                },
            )

        if not auth_enabled and not protected_auth_mutation:
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
