"""Server-rendered login and dashboard routes."""

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import (
    WordPressRequestError,
    clear_session_cookie,
    create_session,
    read_session,
    revoke_session,
    set_session_cookie,
    validate_wordpress_login,
    verify_csrf,
)
from authorization import agent_access_allowed
from config import get_settings


router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Cache-busting version for static assets: the newest mtime of the bundled
# files, computed once per process. /static/ is served with a long max-age,
# so every deploy (which restarts the service) must change asset URLs.
_STATIC_DIR = Path(__file__).parent / "static"
ASSET_VERSION = str(int(max(
    (f.stat().st_mtime for f in _STATIC_DIR.glob("*.*")), default=0
)))
templates.env.globals["asset_version"] = ASSET_VERSION


def _require_same_origin(request: Request) -> None:
    """Prevent login CSRF without storing a pre-authentication credential."""

    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        raise WordPressRequestError("Your browser did not provide an origin header", 403)
    parsed = urlsplit(source)
    expected = urlsplit(str(request.base_url))
    if parsed.scheme.lower() != expected.scheme.lower() or parsed.netloc.lower() != expected.netloc.lower():
        raise WordPressRequestError("Cross-site login request rejected", 403)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if read_session(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None},
    )


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    try:
        _require_same_origin(request)
    except WordPressRequestError as exc:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": str(exc)},
            status_code=exc.status,
        )
    username = username.strip()
    if not username or len(username) > 100 or not password:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Enter a valid WordPress username and password."},
            status_code=400,
        )

    try:
        identity = validate_wordpress_login(username, password)
    except WordPressRequestError as exc:
        status = exc.status if exc.status in {401, 403, 502} else 502
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": str(exc)},
            status_code=status,
        )

    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, create_session(identity))
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    user = read_session(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    verify_csrf(user, csrf_token)
    revoke_session(user)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = read_session(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    settings = get_settings()
    assistant_allowed = agent_access_allowed(user, settings)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "csrf_token": user["csrf"],
            # The assistant must be absent from the rendered page unless both
            # the feature flag and the per-user allowlist permit this operator.
            # API routes independently enforce the same predicate.
            "agent_access_allowed": assistant_allowed,
            "agent_writes_enabled": (
                assistant_allowed and settings.agent_writes_enabled
            ),
            # Which WordPress environment Sync pushes to - surfaced in the UI
            # so operators always know what a sync will touch.
            "wp_target_host": urlsplit(settings.wp_sync_url).netloc,
        },
    )
