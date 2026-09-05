"""Fail-closed integration fixtures for the Logo Admin service."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from tests.harness_guard import SAFE_LOGO_ADMIN_FLAGS, validate_test_target


from contextlib import contextmanager

TEST_DSN = os.environ.get("TEST_DATABASE_DSN", "").strip()
TEST_ADMIN_DSN = os.environ.get("TEST_DATABASE_ADMIN_DSN", "").strip()

# The harness grants logo_admin INSERT/UPDATE/DELETE on woo.store_product_state
# so fixtures can shape product state directly. Production revokes them (the
# role script), so contract checks look at the role WITHOUT them.
HARNESS_ONLY_GRANTS = (
    ("woo.store_product_state", ("INSERT", "UPDATE", "DELETE")),
)


@contextmanager
def harness_grants_suspended():
    """Lift the harness-only grants while a contract check runs, then put
    back exactly what was there."""

    if not TEST_ADMIN_DSN:
        yield
        return
    connection = psycopg2.connect(TEST_ADMIN_DSN)
    connection.autocommit = True
    restore = []
    try:
        with connection.cursor() as cursor:
            for table, privileges in HARNESS_ONLY_GRANTS:
                present = []
                for privilege in privileges:
                    cursor.execute(
                        "SELECT has_table_privilege('logo_admin', %s, %s)",
                        (table, privilege),
                    )
                    if cursor.fetchone()[0]:
                        present.append(privilege)
                if present:
                    cursor.execute(
                        f"REVOKE {', '.join(present)} ON {table} FROM logo_admin"
                    )
                    restore.append((table, present))
        yield
    finally:
        with connection.cursor() as cursor:
            for table, present in restore:
                cursor.execute(
                    f"GRANT {', '.join(present)} ON {table} TO logo_admin"
                )
        connection.close()


def repull_function_sha256():
    """The hash the write contract expects for logo.repull_display_name,
    taken from the function the harness installed. Production supplies
    AGENT_REPULL_FUNCTION_SHA256 by hand after review; the tests fall back
    to the installed definition when the variable is not set."""

    dsn = TEST_ADMIN_DSN or TEST_DSN
    if not dsn:
        return None
    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT encode(sha256(convert_to(
                           pg_get_functiondef(procedure.oid), 'UTF8'
                       )), 'hex')
                  FROM pg_proc AS procedure
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = procedure.pronamespace
                 WHERE namespace.nspname = 'logo'
                   AND procedure.proname = 'repull_display_name'
                """
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    return row[0] if row else None
if not TEST_DSN or not TEST_ADMIN_DSN:
    raise pytest.UsageError(
        "TEST_DATABASE_DSN and TEST_DATABASE_ADMIN_DSN are required"
    )


def inspect_database(dsn: str, *, inspect_app: bool = False) -> dict:
    with psycopg2.connect(dsn) as connection:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_user, session_user"
            )
            database_name, role_name, session_role_name = cursor.fetchone()
            result = {
                "database_name": str(database_name),
                "role_name": str(role_name),
                "session_role_name": str(session_role_name),
            }
            if inspect_app:
                cursor.execute(
                    """
                    SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb,
                           rolcreaterole, rolreplication, rolbypassrls
                      FROM pg_roles WHERE rolname = current_user
                    """
                )
                flags = cursor.fetchone()
                result["role_flags"] = dict(zip(
                    SAFE_LOGO_ADMIN_FLAGS,
                    map(bool, flags),
                ))
                cursor.execute(
                    """
                    SELECT count(*)
                      FROM pg_auth_members membership
                      JOIN pg_roles role ON role.oid = membership.member
                     WHERE role.rolname = current_user
                    """
                )
                result["membership_count"] = int(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT to_regclass('fdm4.codex_test_harness')"
                )
                if cursor.fetchone()[0] is None:
                    result["marker_database_name"] = ""
                    result["marker_nonce"] = ""
                else:
                    cursor.execute(
                        """
                        SELECT database_name, nonce
                          FROM fdm4.codex_test_harness
                         ORDER BY created_at DESC LIMIT 1
                        """
                    )
                    marker = cursor.fetchone()
                    result["marker_database_name"] = str(marker[0])
                    result["marker_nonce"] = str(marker[1])
    return result


app_target = inspect_database(TEST_DSN, inspect_app=True)
admin_target = inspect_database(TEST_ADMIN_DSN)
try:
    validate_test_target(
        app_target["database_name"],
        app_target["role_name"],
        app_target["session_role_name"],
        admin_target["database_name"],
        admin_target["role_name"],
        app_role_flags=app_target["role_flags"],
        app_membership_count=app_target["membership_count"],
        marker_database_name=app_target["marker_database_name"],
        marker_nonce=app_target["marker_nonce"],
    )
except RuntimeError as exc:
    raise pytest.UsageError(str(exc)) from exc

# Deliberately overwrite, never setdefault: inherited deployment values lose.
os.environ["DATABASE_DSN"] = TEST_DSN
os.environ["SESSION_SECRET"] = "test-session-secret-" + ("x" * 32)
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["UPLOAD_DIR"] = "/tmp/arb-logo-admin-test-uploads"
os.environ["AGENT_UPLOAD_DIR"] = "/tmp/arb-logo-admin-test-agent-uploads"
os.environ["WP_AUTH_URL"] = "https://example.test/wp-json/arb/v1/logo-admin/auth"
os.environ["WP_SYNC_URL"] = "https://example.test/wp-json/arb/v1/logo-admin/sync"
os.environ["WP_SYNC_USER"] = "test-service"
os.environ["WP_SYNC_APP_PASSWORD"] = "test-password"
os.environ["MEDIA_BASE"] = "https://media.example.test/logos/"
os.environ["FDM4_ART_BASE"] = "https://media.example.test/fdm4/"
os.environ["AGENT_ENABLED"] = "false"
os.environ["AGENT_WRITES_ENABLED"] = "false"
os.environ["AGENT_ALLOWED_USERS"] = "admin-one"
# Concurrency tests spin up ~12 workers; give the pool room so they exercise the
# per-user job cap rather than exhausting the default 8-connection pool.
os.environ["DATABASE_POOL_MAX"] = "16"
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("OPENAI_MODEL", None)

from fastapi.testclient import TestClient

import auth
from config import get_settings
from db import database
import main


@dataclass
class AuthenticatedClient:
    client: TestClient
    csrf: str

    def request(self, method: str, url: str, **kwargs: Any):
        headers = dict(kwargs.pop("headers", {}))
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers.setdefault("X-CSRF-Token", self.csrf)
        return self.client.request(method, url, headers=headers, **kwargs)

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any):
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any):
        return self.request("DELETE", url, **kwargs)


@pytest.fixture(autouse=True)
def clean_test_database():
    database.close()
    # Isolate each test from the MCP process's import-time dependency overrides:
    # importing mcp_server (for its own tests) mutates the shared app globally.
    # Web-process tests must always run with real auth, so reset before each test.
    main.app.dependency_overrides.clear()
    sql_dir = Path(__file__).parent / "sql"
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute((sql_dir / "reset.sql").read_text())
            cursor.execute((sql_dir / "seed.sql").read_text())
    yield
    database.close()
    get_settings.cache_clear()


@pytest.fixture
def client_as():
    clients: list[TestClient] = []

    def build(user_login: str = "admin-one") -> AuthenticatedClient:
        client = TestClient(main.app)
        token = auth.create_session({
            "user_login": user_login,
            "display_name": user_login,
        })
        payload = auth._serializer(get_settings()).loads(token)
        client.cookies.set(get_settings().session_cookie_name, token)
        clients.append(client)
        return AuthenticatedClient(client, str(payload["csrf"]))

    yield build

    for client in clients:
        client.close()


@pytest.fixture
def agent_enabled(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_WRITES_ENABLED", "false")
    monkeypatch.setenv("AGENT_ALLOWED_USERS", "ADMIN-ONE")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()
