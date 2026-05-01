# -*- coding: utf-8 -*-
"""
===================================
Daily Stock Analysis - FastAPI 后端服务入口（Cloud Run 兼容）
===================================

职责：
1. 复用 ``api.app:app`` 已有的完整 FastAPI 应用
2. 在其上叠加 Cloud Run 友好的最小接口集合：
   - ``GET  /``            服务基本信息
   - ``GET  /health``      健康检查
   - ``POST /analyze``     同步分析（薄封装现有 AnalysisService）
   - ``POST /analyze/async`` 异步分析（内存任务表）
   - ``GET  /tasks/{id}``  查询异步任务状态
3. 通过环境变量 ``PORT``（默认 8080）+ ``0.0.0.0`` 监听，满足 Cloud Run 要求
4. 提供轻量 Bearer Token 鉴权（``API_TOKEN`` 为空时不强制）

启动方式：
    python server.py
    uvicorn server:app --host 0.0.0.0 --port 8080

环境变量：
    PORT                监听端口，默认 8080（Cloud Run 必读）
    HOST                监听地址，默认 0.0.0.0
    API_TOKEN           /analyze 系列 Bearer token，留空则不强制鉴权
    ADMIN_PASSWORD      Web UI 管理员密码；留空则前端开放（仅开发用）
    SESSION_SECRET_KEY  签名 cookie 用密钥；建议 ≥48 字节随机串
    ENV                 development / production；production 时启用 HTTPS-only cookie 等
    STOCK_LIST          /analyze 未传 stocks 时的默认股票列表
    MARKET              默认市场（仅响应回显）
    DRY_RUN             默认 dry_run（true/false）
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlsplit

from src.config import setup_env, get_config
from src.logging_config import setup_logging

# 初始化环境变量与日志（必须在导入 api.app 之前）
setup_env()

_config = get_config()
_level_name = (_config.log_level or "INFO").upper()
_level = getattr(logging, _level_name, logging.INFO)

setup_logging(
    log_prefix="api_server",
    console_level=_level,
    log_dir=_config.log_dir,
    extra_quiet_loggers=["uvicorn", "fastapi"],
)

logger = logging.getLogger(__name__)

from fastapi import Depends, HTTPException, Request, status  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from api.app import app  # noqa: E402  (FastAPI instance, reused as-is)


# ============================================================
# 环境变量辅助
# ============================================================

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_stock_list() -> List[str]:
    raw = os.environ.get("STOCK_LIST", "") or ""
    return [s.strip() for s in raw.split(",") if s.strip()]


# ============================================================
# Web UI 管理员登录（独立于 /api/v1/auth 内部 admin 流程）
#
# 设计目标：
# - Cloud Run 这类无状态环境下，仅靠 ADMIN_PASSWORD + SESSION_SECRET_KEY 两个
#   环境变量就能给前端页面加一道"管理员密码"门
# - 登录态保存在 Starlette SessionMiddleware 的签名 cookie 里，不落盘
# - 与 API_TOKEN（保护 /analyze 系列）和 src.auth（保护 /api/v1/*）互不影响
# ============================================================

_ENV_NAME = (os.environ.get("ENV") or "development").strip().lower()
_IS_PRODUCTION = _ENV_NAME == "production"

_ADMIN_PASSWORD = (os.environ.get("ADMIN_PASSWORD") or "").strip()

_SESSION_SECRET = (os.environ.get("SESSION_SECRET_KEY") or "").strip()
if not _SESSION_SECRET:
    if _IS_PRODUCTION:
        # 生产环境必须配置，否则每次实例重启都会让所有人掉登录态，
        # 多实例时不同实例的 cookie 也会互相不认。
        logger.error(
            "[security] SESSION_SECRET_KEY is empty in production. "
            "Sessions WILL break across restarts/instances. "
            "Set a long random value (e.g. `python -c \"import secrets; print(secrets.token_urlsafe(48))\"`)."
        )
    else:
        logger.warning(
            "[security] SESSION_SECRET_KEY not set; generating an ephemeral one. "
            "Restarting the process will invalidate existing sessions."
        )
    _SESSION_SECRET = secrets.token_urlsafe(48)

if not _ADMIN_PASSWORD:
    if _IS_PRODUCTION:
        logger.error(
            "[security] ADMIN_PASSWORD is NOT set in production. "
            "The Web UI is publicly accessible. Set ADMIN_PASSWORD to enable login."
        )
    else:
        logger.warning(
            "[security] ADMIN_PASSWORD is not set; Web UI login is disabled "
            "(development mode)."
        )

# Cookie 配置
_SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME") or "dsa_admin_session"
_SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE_SECONDS") or 60 * 60 * 12)  # 12h
_SESSION_HTTPS_ONLY = _IS_PRODUCTION

# Middleware 顺序（重要）：
#   Starlette `add_middleware` 把每次新增插入到 user_middleware[0]，
#   构建栈时再反向 wrap 一次，因此 *最后* 调用的 add_middleware 才是 *最外层*。
# 我们想让请求流是：Session → AdminLogin → 已有 Auth/CORS → routes，
# 所以必须先 add AdminLoginMiddleware，再 add SessionMiddleware（见下文）。

# ----- 路径分类 ----------------------------------------------------------

# 永远公开（不需要登录、不需要 API token）
_PUBLIC_EXACT = frozenset({
    "/health",
    "/api/health",
    "/info",
    "/favicon.ico",
    "/login",
    "/logout",
    "/robots.txt",
})

# /api/v1/auth 子路径必须保持公开，否则 WebUI 自身的登录流程会被前端拦截层挡住
_PUBLIC_PREFIX = (
    "/api/v1/auth/",
)

# Cloud Run /analyze 系列：走 API_TOKEN Bearer 校验（路由层 dependency 实现），
# 跟管理员浏览器 session 解耦——给程序化调用方用
_BEARER_API_PREFIX = (
    "/analyze",
    "/tasks/",
)

# /api/v1/*（除了 /api/v1/auth/*）：必须登录才能调
# 之前是直接放行的；现在改成需要 session（保护 WebUI 设置页等内部接口）
_SESSION_API_PREFIX = (
    "/api/v1/",
)


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    for prefix in _PUBLIC_PREFIX:
        if path.startswith(prefix):
            return True
    return False


def _is_bearer_api(path: str) -> bool:
    """走 API_TOKEN Bearer 校验的路径——AdminLoginMiddleware 不应拦截。"""
    if path == "/analyze":
        return True
    for prefix in _BEARER_API_PREFIX:
        if path.startswith(prefix):
            return True
    return False


def _is_session_api(path: str) -> bool:
    """需要管理员 session 的 API 路径，未登录时返回 401 JSON 而非跳 /login。"""
    # /api/v1/auth/* 在 _PUBLIC_PREFIX 已经早一步放行了，这里不会再命中
    for prefix in _SESSION_API_PREFIX:
        if path.startswith(prefix):
            return True
    return False


def _docs_path(path: str) -> bool:
    return path in {"/docs", "/redoc", "/openapi.json"} or path.startswith("/docs/") or path.startswith("/redoc/")


# ----- 登录页面 ----------------------------------------------------------

_LOGIN_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Stock Analysis Admin</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0a0e17;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  .card{width:min(420px,92vw);padding:2.25rem 2rem;border:1px solid #1e293b;border-radius:14px;
        background:#111827;box-shadow:0 24px 48px -24px rgba(0,0,0,.6)}
  h1{font-size:1.15rem;color:#38bdf8;margin-bottom:1.5rem;letter-spacing:.02em}
  .muted{font-size:.78rem;color:#64748b;margin-bottom:1.25rem}
  label{display:block;font-size:.78rem;color:#94a3b8;margin-bottom:.45rem}
  input[type=password]{width:100%;padding:.7rem .85rem;font-size:.95rem;color:#e2e8f0;
        background:#0b1220;border:1px solid #1e293b;border-radius:8px;outline:none}
  input[type=password]:focus{border-color:#38bdf8;box-shadow:0 0 0 3px rgba(56,189,248,.18)}
  button{margin-top:1.1rem;width:100%;padding:.75rem 1rem;border:none;border-radius:8px;
        background:#38bdf8;color:#0a0e17;font-weight:600;font-size:.95rem;cursor:pointer}
  button:hover{background:#0ea5e9}
  .err{margin-top:1rem;padding:.6rem .8rem;border-left:3px solid #ef4444;background:#1f1414;
       border-radius:0 6px 6px 0;color:#fca5a5;font-size:.82rem}
  .footer{margin-top:1.25rem;font-size:.7rem;color:#475569;text-align:center}
</style></head><body><div class="card">
<h1>Daily Stock Analysis Admin</h1>
<p class="muted">Sign in to access the Web UI.</p>
__ERROR_BLOCK__
<form method="post" action="/login__NEXT_QUERY__" autocomplete="off">
  <label for="password">Admin password</label>
  <input id="password" name="password" type="password" placeholder="Admin password" autofocus required>
  <button type="submit">Login</button>
</form>
<p class="footer">Health: <a href="/health" style="color:#38bdf8;text-decoration:none">/health</a></p>
</div></body></html>
"""


def _render_login_page(error: str | None = None, next_path: str | None = None) -> HTMLResponse:
    error_block = (
        f'<div class="err">{html.escape(error)}</div>' if error else ""
    )
    next_query = ""
    if next_path:
        next_query = "?next=" + quote(next_path, safe="/")
    body = (
        _LOGIN_PAGE_TEMPLATE
        .replace("__ERROR_BLOCK__", error_block)
        .replace("__NEXT_QUERY__", next_query)
    )
    return HTMLResponse(content=body, status_code=200 if not error else 401)


def _safe_next_path(raw: str | None) -> str:
    """Reject absolute URLs / scheme-relative URLs / oddities — only allow
    in-app paths beginning with a single '/'."""
    if not raw:
        return "/"
    candidate = unquote(raw).strip()
    if not candidate:
        return "/"
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return "/"
    if not candidate.startswith("/"):
        return "/"
    if candidate.startswith("//"):
        return "/"
    if candidate.startswith(("/login", "/logout")):
        return "/"
    return candidate


# ----- 中间件：处理 /login, /logout，并保护前端路由 ----------------------------

class AdminLoginMiddleware(BaseHTTPMiddleware):
    """
    入站请求依次：
      1. /login (GET/POST) / /logout → 直接处理
      2. 永远公开路径 → 放行
      3. Bearer-API（/analyze、/tasks/）→ 放行（在路由层做 API_TOKEN 校验）
      4. ADMIN_PASSWORD 未配置 → 放行（开发模式）
      5. Session-API（/api/v1/*，除 /api/v1/auth/*）：
           - 已登录 → 放行
           - 未登录 → 401 JSON（不跳 /login，避免前端 fetch 报错）
      6. /docs 系列：开发环境放行；生产环境视为前端，按下一条处理
      7. 其余前端路径：
           - 已登录 → 放行
           - 未登录 → 303 跳 /login
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        # ----- /login ---------------------------------------------------
        if path == "/login":
            if method == "GET":
                if _ADMIN_PASSWORD and request.session.get("is_admin") is True:
                    target = _safe_next_path(request.query_params.get("next"))
                    return RedirectResponse(target, status_code=303)
                return _render_login_page(
                    next_path=_safe_next_path(request.query_params.get("next")),
                )
            if method == "POST":
                form = await request.form()
                submitted = (form.get("password") or "").strip()
                if not _ADMIN_PASSWORD:
                    return _render_login_page(
                        error="ADMIN_PASSWORD is not configured on the server.",
                    )
                if submitted and hmac.compare_digest(submitted, _ADMIN_PASSWORD):
                    request.session["is_admin"] = True
                    request.session["login_at"] = datetime.utcnow().isoformat()
                    target = _safe_next_path(
                        form.get("next") or request.query_params.get("next")
                    )
                    return RedirectResponse(target, status_code=303)
                logger.info("admin login failed from %s", request.client.host if request.client else "?")
                return _render_login_page(error="Invalid admin password")
            return Response(status_code=405, headers={"Allow": "GET, POST"})

        # ----- /logout --------------------------------------------------
        if path == "/logout":
            request.session.pop("is_admin", None)
            request.session.pop("login_at", None)
            return RedirectResponse("/login", status_code=303)

        # ----- 永远公开 -------------------------------------------------
        if _is_public_path(path):
            return await call_next(request)

        # ----- Bearer 校验的 API（/analyze、/tasks/）-------------------
        if _is_bearer_api(path):
            return await call_next(request)

        # ----- 没设置 ADMIN_PASSWORD：开发模式直接放行 ------------------
        if not _ADMIN_PASSWORD:
            return await call_next(request)

        # ----- /api/v1/* (除 /api/v1/auth/*)：要求 session，未登录给 401 JSON -----
        if _is_session_api(path):
            if request.session.get("is_admin") is True:
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Admin session required. Login at /login.",
                },
            )

        # ----- /docs 系列：开发模式放行；生产模式视为前端，需要登录 ----
        if _docs_path(path) and not _IS_PRODUCTION:
            return await call_next(request)

        # ----- 前端 HTML 页面：未登录跳 /login -------------------------
        if request.session.get("is_admin") is True:
            return await call_next(request)

        login_url = "/login?next=" + quote(path, safe="/")
        return RedirectResponse(login_url, status_code=303)


# 顺序：先 AdminLoginMiddleware（内层），再 SessionMiddleware（外层）。
# SessionMiddleware 解析 / 签发签名 cookie，必须在 AdminLoginMiddleware 之前
# 处理请求，AdminLoginMiddleware 才能读到 request.session。
app.add_middleware(AdminLoginMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    session_cookie=_SESSION_COOKIE_NAME,
    max_age=_SESSION_MAX_AGE,
    same_site="lax",
    https_only=_SESSION_HTTPS_ONLY,
)


# ============================================================
# 鉴权依赖：API_TOKEN 静态串 + Google OIDC ID-token 双通道
# ============================================================
#
# 这个 dependency 同时接受两种 ``Authorization: Bearer <...>`` 凭据：
# 1. 静态 ``API_TOKEN`` 环境变量字面值（人手工 curl / 客户端用）。
# 2. Google 签发的 OIDC ID token，且 token 的 ``email`` 在允许调用者列表
#    （默认就是本服务的运行时 SA），``aud`` 必须是本服务的 Cloud Run URL。
#    这条通道是给 GCP Cloud Scheduler 走的：scheduler 的 HTTP 目标可以
#    挂 ``OidcToken``，Google 自动签发并轮换，省掉手动维护静态 token 的
#    管理负担。
#
# 行为：
# - 两种通道任一通过即放行；都失败时返回 401。
# - 若 ``API_TOKEN`` 未配置且 ``SCHEDULER_INVOKER_SA`` 也没设：完全
#   跳过校验（本地 / dev 模式），保持向后兼容。

def _allowed_oidc_invokers() -> list[str]:
    """OIDC token 的 email 必须在这个列表里。
    默认包含本服务的运行时 SA（从 metadata server 自动拿）。
    可以通过 ``SCHEDULER_INVOKER_SA``（逗号分隔）追加额外允许的 SA。
    """
    allowed: list[str] = []
    extra = (os.environ.get("SCHEDULER_INVOKER_SA") or "").strip()
    if extra:
        allowed.extend(s.strip() for s in extra.split(",") if s.strip())
    # 自动加上当前 Cloud Run 进程的 runtime SA
    try:
        from src.services.cloud_scheduler_service import _detect_runtime_sa  # noqa: WPS433
        runtime_sa = _detect_runtime_sa()
        if runtime_sa and runtime_sa not in allowed:
            allowed.append(runtime_sa)
    except Exception:
        pass
    return allowed


def _try_verify_oidc_token(token: str) -> Optional[str]:
    """Validate a Google-signed OIDC ID token. Returns the verified email
    (caller identity) on success, or None on any failure."""
    try:
        from google.auth.transport import requests as g_requests  # type: ignore
        from google.oauth2 import id_token as g_id_token  # type: ignore
    except ImportError:
        return None

    try:
        # audience can be the canonical Cloud Run service URL or any value
        # Cloud Scheduler put in the OidcToken.audience field.
        from src.services.cloud_scheduler_service import _detect_cloud_run_url
        expected_audience = (
            os.environ.get("OIDC_EXPECTED_AUDIENCE")
            or _detect_cloud_run_url()
        )
        if not expected_audience:
            return None
        info = g_id_token.verify_oauth2_token(
            token,
            g_requests.Request(),
            audience=expected_audience,
        )
        email = (info.get("email") or "").strip()
        if not email:
            return None
        if email not in _allowed_oidc_invokers():
            logger.warning("OIDC caller %s not in allowed invoker list", email)
            return None
        return email
    except Exception as exc:
        logger.debug("OIDC verify failed: %s", exc)
        return None


def _require_api_token(request: Request) -> None:
    static_token = (os.environ.get("API_TOKEN") or "").strip()
    invoker_allowlist = _allowed_oidc_invokers()

    # Wide-open dev mode: nothing configured at all.
    if not static_token and not invoker_allowlist:
        return

    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Missing Authorization header"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Invalid Authorization header"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = parts[1].strip()

    # 1) Static API_TOKEN match (manual / client-script callers)
    if static_token and presented == static_token:
        return

    # 2) OIDC ID token (Cloud Scheduler / cron callers)
    if invoker_allowlist:
        verified_email = _try_verify_oidc_token(presented)
        if verified_email is not None:
            request.state.oidc_invoker = verified_email
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "message": "Invalid bearer token"},
        headers={"WWW-Authenticate": "Bearer"},
    )


# ============================================================
# 请求 / 响应模型（Cloud Run 接口专用，独立于 /api/v1）
# ============================================================

class CloudRunAnalyzeRequest(BaseModel):
    stocks: Optional[List[str]] = Field(
        default=None,
        description="股票代码列表；省略时回退到环境变量 STOCK_LIST",
    )
    market: Optional[str] = Field(
        default=None,
        description="市场标识（cn/hk/us），仅用于响应回显，实际市场由代码自动识别",
    )
    notify: bool = Field(default=False, description="是否发送外部通知")
    dry_run: bool = Field(default=False, description="演练模式：跳过真实分析与通知")
    report_type: str = Field(default="simple", description="simple / detailed / full / brief")
    force_refresh: bool = Field(default=False, description="是否强制刷新缓存")


class CloudRunAnalyzeResponse(BaseModel):
    success: bool
    stocks: List[str]
    result: List[Dict[str, Any]] = Field(default_factory=list)
    report: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    dry_run: bool = False


class CloudRunAsyncAccepted(BaseModel):
    success: bool = True
    task_id: str
    status: str = "pending"
    stocks: List[str]


class CloudRunTaskState(BaseModel):
    task_id: str
    status: str  # pending / running / success / failed
    stocks: List[str]
    result: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str


# ============================================================
# 内存任务表（Cloud Run 单实例够用；多实例请用 Cloud Tasks/Pub-Sub）
# ============================================================

_TASKS: Dict[str, Dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()


def _new_task(stocks: List[str]) -> str:
    task_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    with _TASKS_LOCK:
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "stocks": list(stocks),
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
    return task_id


def _update_task(task_id: str, **fields: Any) -> None:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        task.update(fields)
        task["updated_at"] = datetime.utcnow().isoformat()


def _get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task else None


# ============================================================
# 实际分析调用（同步函数，需要在 threadpool 里调用以避免阻塞事件循环）
# ============================================================

def _analyze_one(stock_code: str, report_type: str, force_refresh: bool, notify: bool) -> Dict[str, Any]:
    """Wrap the existing AnalysisService for a single stock."""
    from src.services.analysis_service import AnalysisService

    query_id = uuid.uuid4().hex
    service = AnalysisService()
    result = service.analyze_stock(
        stock_code=stock_code,
        report_type=report_type,
        force_refresh=force_refresh,
        query_id=query_id,
        send_notification=notify,
    )
    if result is None:
        return {
            "stock_code": stock_code,
            "success": False,
            "error": service.last_error or "analysis_failed",
            "query_id": query_id,
        }
    return {
        "stock_code": result.get("stock_code", stock_code),
        "stock_name": result.get("stock_name"),
        "success": True,
        "query_id": query_id,
        "report": result.get("report"),
    }


def _run_analysis(stocks: List[str], request: CloudRunAnalyzeRequest) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for code in stocks:
        try:
            out.append(
                _analyze_one(
                    stock_code=code,
                    report_type=request.report_type,
                    force_refresh=request.force_refresh,
                    notify=request.notify,
                )
            )
        except Exception as exc:  # 单只股票失败不影响整体
            logger.exception("Cloud Run /analyze 失败: stock=%s", code)
            out.append({"stock_code": code, "success": False, "error": str(exc)})
    return out


# ============================================================
# Cloud Run 路由
# ============================================================

# NOTE: 根路径 ``GET /`` 由 ``api.app:app`` 自身管理（前端 SPA 或引导页）。
# Cloud Run 的纯 JSON 视图通过 ``GET /info`` 暴露，避免与前端冲突。

@app.get("/info", tags=["CloudRun"], summary="服务基本信息")
async def cloud_run_info() -> Dict[str, Any]:
    return {
        "name": "daily_stock_analysis_server",
        "status": "ok",
        "version": app.version,
        "env": _ENV_NAME,
        "docs": "/docs",
        "health": "/health",
        "login": "/login",
        "analyze": "/analyze",
        "analyze_async": "/analyze/async",
        "default_stock_list": _env_stock_list(),
        "api_token_required": bool((os.environ.get("API_TOKEN") or "").strip()),
        "admin_login_required": bool(_ADMIN_PASSWORD),
    }


@app.get("/health", tags=["CloudRun"], summary="健康检查")
async def cloud_run_health() -> Dict[str, str]:
    return {"status": "healthy"}


@app.post(
    "/analyze",
    tags=["CloudRun"],
    summary="同步分析（Cloud Run 友好封装）",
    response_model=CloudRunAnalyzeResponse,
    dependencies=[Depends(_require_api_token)],
)
async def cloud_run_analyze(payload: CloudRunAnalyzeRequest) -> CloudRunAnalyzeResponse:
    stocks = payload.stocks or _env_stock_list()
    if not stocks:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_error",
                "message": "未提供 stocks 参数，且环境变量 STOCK_LIST 为空",
            },
        )

    dry_run = payload.dry_run or _env_bool("DRY_RUN", False)
    if dry_run:
        return CloudRunAnalyzeResponse(
            success=True,
            stocks=stocks,
            result=[
                {"stock_code": code, "success": True, "dry_run": True}
                for code in stocks
            ],
            dry_run=True,
        )

    try:
        # 现有 AnalysisService 是同步实现，必须扔到 threadpool 里跑，
        # 否则会阻塞 uvicorn 的事件循环。
        results = await run_in_threadpool(_run_analysis, stocks, payload)
    except Exception as exc:  # pragma: no cover - 顶层兜底
        logger.exception("Cloud Run /analyze 整体失败")
        return CloudRunAnalyzeResponse(
            success=False,
            stocks=stocks,
            result=[],
            error=str(exc),
        )

    success = all(item.get("success") for item in results) if results else False
    first_report = next((item.get("report") for item in results if item.get("report")), None)
    return CloudRunAnalyzeResponse(
        success=success,
        stocks=stocks,
        result=results,
        report=first_report,
        error=None if success else "one_or_more_stocks_failed",
        dry_run=False,
    )


def _async_runner(task_id: str, stocks: List[str], payload: CloudRunAnalyzeRequest) -> None:
    _update_task(task_id, status="running")
    try:
        results = _run_analysis(stocks, payload)
        success = all(item.get("success") for item in results) if results else False
        _update_task(
            task_id,
            status="success" if success else "failed",
            result=results,
            error=None if success else "one_or_more_stocks_failed",
        )
    except Exception as exc:
        logger.exception("Cloud Run /analyze/async 任务失败 task=%s", task_id)
        _update_task(task_id, status="failed", error=str(exc))


@app.post(
    "/analyze/async",
    tags=["CloudRun"],
    summary="异步分析（立即返回 task_id）",
    response_model=CloudRunAsyncAccepted,
    status_code=202,
    dependencies=[Depends(_require_api_token)],
)
async def cloud_run_analyze_async(payload: CloudRunAnalyzeRequest) -> CloudRunAsyncAccepted:
    stocks = payload.stocks or _env_stock_list()
    if not stocks:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_error",
                "message": "未提供 stocks 参数，且环境变量 STOCK_LIST 为空",
            },
        )

    task_id = _new_task(stocks)
    thread = threading.Thread(
        target=_async_runner,
        args=(task_id, stocks, payload),
        daemon=True,
        name=f"cloudrun-analyze-{task_id[:8]}",
    )
    thread.start()
    return CloudRunAsyncAccepted(task_id=task_id, stocks=stocks)


@app.get(
    "/tasks/{task_id}",
    tags=["CloudRun"],
    summary="查询异步任务状态",
    response_model=CloudRunTaskState,
    dependencies=[Depends(_require_api_token)],
)
async def cloud_run_task_status(task_id: str) -> CloudRunTaskState:
    task = _get_task(task_id)
    if not task:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"task {task_id} not found"},
        )
    return CloudRunTaskState(**task)


# ============================================================
# 把 Cloud Run 路由提到 SPA 捕获路由之前，避免被 ``/{full_path:path}`` 吞掉
# ============================================================
#
# 现象：当 ``api.app:create_app`` 检测到打包好的前端（``static/index.html``
# 存在）时，会在末尾注册一条 ``GET /{full_path:path}`` SPA 兜底路由。
# 这条路由比我们用 ``@app.get`` 在 server.py 里加的 /health、/info、/analyze
# 等晚被定义，但 ``Starlette/FastAPI`` 路由匹配按定义顺序优先 → SPA 兜底
# 会先匹配，把 /health 这样的 JSON 接口当成前端路径返回 index.html。
#
# 这里在所有路由都注册完之后做一次重排：把 Cloud Run 接口移到列表最前面。
def _promote_cloudrun_routes() -> None:
    cloud_paths = {
        "/info",
        "/health",
        "/analyze",
        "/analyze/async",
        "/tasks/{task_id}",
    }
    promoted: list = []
    others: list = []
    for route in app.router.routes:
        path = getattr(route, "path", None)
        if path in cloud_paths:
            promoted.append(route)
        else:
            others.append(route)
    app.router.routes[:] = promoted + others


_promote_cloudrun_routes()


__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    reload = _env_bool("UVICORN_RELOAD", False)

    logger.info("Starting uvicorn on %s:%s (reload=%s)", host, port, reload)
    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=reload,
        log_level=_level_name.lower(),
    )
