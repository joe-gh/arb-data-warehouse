"""Agent-specific access policy and future authorization seams."""

from dataclasses import dataclass
import hashlib
import hmac
from typing import Literal, Mapping, Optional

from fastapi import Depends, Header, HTTPException, Request

from auth import require_user, verify_csrf
from config import Settings, get_settings


AdminTier = Literal["admin"]
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

HUMAN_ONLY_COMMANDS = frozenset({
    'cat_snapshot_import',
    'cat_draft_seed',
    'cat_run_create',
    'cat_run_start',
    'cat_run_pause',
    'cat_run_resume',
    'cat_run_cancel',
    'cat_job_retry',
    'cat_job_skip',
    'cat_restore_blog',
    'cat_job_restore',
    'cat_freeze_set',
    'cat_lock',
    'cat_unlock',
    'cat_drift_audit',

    "confirm_change_set",
    "apply_change_set",
    "discard_change_set",
    "undo_change_set",
    "confirm_spreadsheet_mapping",
})


@dataclass(frozen=True)
class AccessContext:
    user_login: str
    display_name: str

    @classmethod
    def from_session(cls, user: Mapping[str, str]) -> "AccessContext":
        login = str(user.get("user_login", "")).strip().lower()
        if not login:
            raise ValueError("authenticated user_login is required")
        return cls(
            user_login=login,
            display_name=str(user.get("display_name") or login),
        )


def required_tier(
    command: str,
) -> AdminTier:
    """Future authorization seam; the application is admin-only today."""

    del command
    return "admin"


def assert_agent_callable(command: str) -> None:
    if command in HUMAN_ONLY_COMMANDS:
        raise ValueError(f"{command} is human-only")


def agent_access_allowed(
    user: Mapping[str, str] | None,
    settings: Settings | None = None,
) -> bool:
    """Return true only for an enabled, explicitly allow-listed operator."""

    if user is None:
        return False
    login = str(user.get("user_login", "")).strip().lower()
    active_settings = settings or get_settings()
    return bool(
        active_settings.agent_enabled
        and login
        and login in active_settings.agent_allowed_users
    )


def provider_safety_identifier(user_login: str, settings: Settings) -> str:
    """Return a stable, non-identifying provider abuse-monitoring key."""

    login = str(user_login).strip().lower()
    if not login:
        raise ValueError("user_login is required")
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        login.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def require_agent_access(
    request: Request,
    user: dict[str, str] = Depends(require_user),
    csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
) -> dict[str, str]:
    """Single fail-closed dependency for every agent HTTP endpoint.

    Authenticated users outside the allowlist receive 404 so the feature is
    invisible. Unsafe methods additionally use the application's existing
    CSRF verifier.
    """

    if not agent_access_allowed(user):
        raise HTTPException(status_code=404, detail="Not found")
    if request.method.upper() not in SAFE_METHODS:
        verify_csrf(user, csrf_token)
    return user
