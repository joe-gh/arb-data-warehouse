"""FastAPI entry point for the standalone Warehouse Logo Admin."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg2 import DatabaseError, InterfaceError, OperationalError
from psycopg2.pool import PoolError

from auth import read_session, verify_csrf
from authorization import agent_access_allowed
from config import get_settings
from database_contract import validate_write_database_contract
from db import database
from categories_api import router as categories_router
from routes_agent import router as agent_router
from routes_api import router as api_router
from routes_feed import router as feed_router
from routes_pages import router as pages_router
from tool_registry import validate_registry, validate_write_tool_allowlist


settings = get_settings()
base_dir = Path(__file__).parent

app = FastAPI(
    title="Arborwear Warehouse Operations",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory=base_dir / "static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(base_dir / "static" / "favicon.ico", media_type="image/x-icon")
app.mount(
    "/logo-media",
    StaticFiles(directory=settings.upload_dir, check_dir=False),
    name="logo-media",
)
app.include_router(api_router)
app.include_router(categories_router)
app.include_router(agent_router)
app.include_router(feed_router)
app.include_router(pages_router)


@app.middleware("http")
async def early_agent_access_gate(request: Request, call_next):
    """Reject signed-in disallowed users before JSON/multipart parsing."""

    if request.url.path == "/api/agent" or request.url.path.startswith(
        "/api/agent/"
    ):
        user = read_session(request)
        if user is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )
        active_settings = get_settings()
        if not agent_access_allowed(user, active_settings):
            return JSONResponse(
                status_code=404,
                content={"detail": "Not found"},
            )
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            try:
                verify_csrf(user, request.headers.get("X-CSRF-Token"))
            except Exception as exc:
                return JSONResponse(
                    status_code=getattr(exc, "status_code", 403),
                    content={
                        "detail": getattr(
                            exc,
                            "detail",
                            "Invalid CSRF token",
                        )
                    },
                )
        if (
            not active_settings.agent_writes_enabled
            and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path not in {
                "/api/agent/chat",
                "/api/agent/sessions",
            }
        ):
            return JSONResponse(
                status_code=404,
                content={"detail": "Not found"},
            )
    return await call_next(request)


@app.on_event("startup")
def startup() -> None:
    validate_registry(
        writes_enabled=settings.agent_writes_enabled,
    )
    # AGENT_WRITE_TOOLS may only name approved writes (fail closed at startup).
    validate_write_tool_allowlist(getattr(settings, "agent_write_tools", None))
    settings.upload_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    if settings.agent_writes_enabled:
        settings.agent_upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            settings.agent_upload_dir.is_symlink()
            or settings.agent_upload_dir.resolve(strict=True)
            != settings.agent_upload_dir
        ):
            raise RuntimeError("AGENT_UPLOAD_DIR must not be a symbolic link")
        settings.agent_upload_dir.chmod(0o700)
    database.open()
    if settings.agent_writes_enabled:
        try:
            with database.cursor() as cursor:
                validate_write_database_contract(
                    cursor,
                    expected_repull_sha256=(
                        settings.agent_repull_function_sha256
                    ),
                )
        except Exception:
            database.close()
            raise


@app.on_event("shutdown")
def shutdown() -> None:
    database.close()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'; script-src 'self'; "
        "style-src 'self'; connect-src 'self'; img-src 'self' data: https:; "
        "font-src 'self'; upgrade-insecure-requests"
    )
    if request.url.path == "/api/agent/chat":
        response.headers["Cache-Control"] = "no-cache, no-transform"
        response.headers["Pragma"] = "no-cache"
    elif request.url.path.startswith(("/static/", "/logo-media/")):
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
    else:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
@app.exception_handler(DatabaseError)
# PoolError is NOT a DatabaseError subclass - without this, pool exhaustion
# (e.g. several long previews at once) surfaces as a raw 500.
@app.exception_handler(PoolError)
def database_failure(request: Request, exc: Exception):
    del request, exc
    return JSONResponse(
        status_code=503,
        content={"detail": "The warehouse database is currently unavailable"},
    )
