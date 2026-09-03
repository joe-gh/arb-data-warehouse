"""WordPress identity validation, signed sessions, and CSRF protection."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import Depends, Header, HTTPException, Request as FastAPIRequest, Response
from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer

from config import Settings, get_settings
from db import database


SESSION_SALT = "arb-logo-admin-session-v1"


class WordPressRequestError(RuntimeError):
    """A sanitized WordPress broker error safe to show to the operator."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never forward WordPress Basic credentials across a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def wordpress_json_request(
    url: str,
    username: Optional[str],
    application_password: Optional[str],
    *,
    method: str,
    timeout: int,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call a WordPress JSON route, with HTTP Basic credentials when given."""

    headers = {
        "Accept": "application/json",
        "User-Agent": "arb-logo-admin/1.0",
    }
    if username is not None and application_password is not None:
        credentials = f"{username}:{application_password}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=timeout) as response:
            raw = response.read(WORDPRESS_RESPONSE_CAP_BYTES)
            status = response.status
    except HTTPError as exc:
        raw = exc.read(64 * 1024)
        message = _wordpress_error_message(raw) or "WordPress rejected the request"
        if 400 <= exc.code < 500:
            raise WordPressRequestError(message, exc.code) from None
        raise WordPressRequestError(message, 502) from None
    except (URLError, TimeoutError, OSError):
        raise WordPressRequestError("WordPress is currently unreachable", 502) from None

    if status < 200 or status >= 300:
        raise WordPressRequestError("Unexpected response from WordPress", 502)
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WordPressRequestError("WordPress returned invalid JSON", 502) from None
    if not isinstance(result, dict):
        raise WordPressRequestError("WordPress returned an invalid response", 502)
    return result


# Category exports for a large blog run to several MB per page; the broker
# pages memberships at 5,000 rows so this stays a safety cap, not a limit.
WORDPRESS_RESPONSE_CAP_BYTES = 16 * 1024 * 1024


def _wordpress_error_message(raw: bytes) -> str:
    """The operator-facing text of a WordPress error body. Besides ``message``
    it carries the broker's structured drift/fence reports (``code`` plus a
    compact ``drift`` / ``report`` list) so a refused apply says WHICH terms
    moved instead of a generic rejection."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    parts = []
    if isinstance(payload.get("message"), str) and payload["message"]:
        parts.append(payload["message"])
    code = payload.get("code")
    if isinstance(code, str) and code and not parts:
        parts.append(code)
    for key in ("drift", "report"):
        rows = payload.get(key)
        if isinstance(rows, list) and rows:
            rendered = []
            for row in rows[:10]:
                if isinstance(row, dict):
                    rendered.append(", ".join(f"{k}={v}" for k, v in row.items() if v not in (None, "")))
                else:
                    rendered.append(str(row))
            more = f" (+{len(rows) - 10} more)" if len(rows) > 10 else ""
            parts.append(f"{key}: " + "; ".join(rendered) + more)
    return " | ".join(parts)[:1500]


def validate_wordpress_login(username: str, password: str) -> Dict[str, str]:
    """Validate an operator's WordPress username + ACCOUNT password.

    Posts the credentials to the logo-admin login broker, which runs them
    through wp_authenticate() - so Wordfence's brute-force protection and
    lockouts apply. Credentials are never stored; only identity comes back.
    """

    settings = get_settings()
    parsed = urlsplit(settings.wp_auth_url)
    auth_path = parsed.path.rstrip("/")
    if not auth_path.endswith("/arb/v1/logo-admin/auth"):
        raise WordPressRequestError("WP_AUTH_URL is not a Logo Admin auth route", 502)
    login_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            auth_path[: -len("/auth")] + "/login",
            "",
            "",
        )
    )
    result = wordpress_json_request(
        login_url,
        None,
        None,
        method="POST",
        timeout=settings.wp_http_timeout,
        payload={"username": username, "password": password},
    )
    user_login = str(result.get("user_login", "")).strip()
    if not user_login:
        raise WordPressRequestError("WordPress did not return a user identity", 502)
    return {
        "user_login": user_login,
        "display_name": str(result.get("display_name", user_login)).strip()
        or user_login,
        "email": str(result.get("email", "")).strip(),
    }


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        settings.session_secret,
        salt=SESSION_SALT,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def create_session(identity: Dict[str, str]) -> str:
    """Create an eight-hour-max signed session containing no credentials."""

    settings = get_settings()
    session_id = secrets.token_urlsafe(32)
    ttl = min(settings.session_max_age, 8 * 60 * 60)
    payload = {
        "session_id": session_id,
        "user_login": identity["user_login"],
        "display_name": identity.get("display_name") or identity["user_login"],
        "csrf": secrets.token_urlsafe(32),
    }
    with database.cursor(write=True, actor=identity["user_login"]) as cursor:
        cursor.execute(
            """
            INSERT INTO logo.admin_session (
                session_hash, user_login, expires_at
            ) VALUES (%s, %s, %s)
            """,
            (
                hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
                identity["user_login"],
                datetime.now(timezone.utc) + timedelta(seconds=ttl),
            ),
        )
    return _serializer(settings).dumps(payload)


def read_session(request: FastAPIRequest) -> Optional[Dict[str, str]]:
    settings = get_settings()
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        return None
    try:
        payload = _serializer(settings).loads(
            cookie, max_age=min(settings.session_max_age, 8 * 60 * 60)
        )
    except (SignatureExpired, BadData):
        return None
    if not isinstance(payload, dict):
        return None
    user_login = payload.get("user_login")
    csrf = payload.get("csrf")
    session_id = payload.get("session_id")
    if (
        not isinstance(user_login, str)
        or not user_login
        or not isinstance(csrf, str)
        or not isinstance(session_id, str)
        or not session_id
    ):
        return None
    session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
              FROM logo.admin_session
             WHERE session_hash = %s
               AND user_login = %s
               AND revoked_at IS NULL
               AND expires_at > now()
             LIMIT 1
            """,
            (session_hash, user_login),
        )
        if cursor.fetchone() is None:
            return None
    return {
        "user_login": user_login,
        "display_name": str(payload.get("display_name") or user_login),
        "csrf": csrf,
        "_session_hash": session_hash,
    }


def revoke_session(user: Dict[str, str]) -> None:
    session_hash = user.get("_session_hash")
    if not session_hash:
        return
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            """
            UPDATE logo.admin_session
               SET revoked_at = COALESCE(revoked_at, now())
             WHERE session_hash = %s
               AND user_login = %s
            """,
            (session_hash, user["user_login"]),
        )


def set_session_cookie(response: Response, value: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.session_cookie_name,
        value,
        max_age=min(settings.session_max_age, 8 * 60 * 60),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )


def require_user(request: FastAPIRequest) -> Dict[str, str]:
    user = read_session(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def verify_csrf(user: Dict[str, str], token: Optional[str]) -> None:
    if not token or not secrets.compare_digest(user["csrf"], token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def require_csrf(
    user: Dict[str, str] = Depends(require_user),
    csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
) -> Dict[str, str]:
    verify_csrf(user, csrf_token)
    return user
