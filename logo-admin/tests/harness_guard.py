"""Pure checks that keep the test suite away from production databases."""

import re
from typing import Mapping


SAFE_LOGO_ADMIN_FLAGS = {
    "can_login": True,
    "inherits": False,
    "superuser": False,
    "can_create_database": False,
    "can_create_role": False,
    "replication": False,
    "bypass_rls": False,
}


def validate_test_target(
    database_name: str,
    app_role: str,
    app_session_role: str,
    admin_database_name: str,
    admin_role: str,
    *,
    app_role_flags: Mapping[str, bool],
    app_membership_count: int,
    marker_database_name: str,
    marker_nonce: str,
) -> None:
    if not re.fullmatch(r"arb_warehouse_test_[A-Za-z0-9_]+", database_name):
        raise RuntimeError(f"refusing non-test database {database_name!r}")
    if app_role != "logo_admin":
        raise RuntimeError(
            f"TEST_DATABASE_DSN must use logo_admin, got {app_role!r}"
        )
    if app_session_role != "logo_admin":
        raise RuntimeError(
            "TEST_DATABASE_DSN session_user must be logo_admin; "
            f"got {app_session_role!r}"
        )
    if admin_database_name != database_name:
        raise RuntimeError("test DSNs refer to different databases")
    if admin_role == "logo_admin":
        raise RuntimeError("reset DSN must not use logo_admin")
    if dict(app_role_flags) != SAFE_LOGO_ADMIN_FLAGS:
        raise RuntimeError(
            "logo_admin role flags are unsafe: "
            f"expected={SAFE_LOGO_ADMIN_FLAGS!r}, actual={dict(app_role_flags)!r}"
        )
    if int(app_membership_count) != 0:
        raise RuntimeError("logo_admin must have no role memberships")
    if marker_database_name != database_name:
        raise RuntimeError("disposable database marker is missing or mismatched")
    if not re.fullmatch(r"[0-9a-f]{32}", marker_nonce):
        raise RuntimeError("disposable database marker nonce is invalid")
    if not database_name.endswith("_" + marker_nonce[:8]):
        raise RuntimeError("test database name is not tied to its random marker")
