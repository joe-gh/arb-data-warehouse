"""Sliding sessions: a cookie older than an hour is re-issued on the next
authenticated request and the registry expiry moves out; a fresh cookie is
left alone; nothing renews past the absolute lifetime."""
import time

import psycopg2

import auth
from config import get_settings
from tests.conftest import TEST_ADMIN_DSN


def _expires_at(session_hash):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT expires_at FROM logo.admin_session WHERE session_hash = %s", (session_hash,))
            return cursor.fetchone()[0]


def _session_hash(client):
    token = client.client.cookies.get(get_settings().session_cookie_name)
    payload = auth._serializer(get_settings()).loads(token)
    import hashlib
    return hashlib.sha256(payload["session_id"].encode("utf-8")).hexdigest()


def test_fresh_session_is_not_reissued(client_as):
    client = client_as("renew-fresh")
    response = client.client.get("/")
    assert response.status_code == 200
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


def test_session_older_than_an_hour_is_renewed_and_extended(client_as, monkeypatch):
    client = client_as("renew-due")
    session_hash = _session_hash(client)
    before = _expires_at(session_hash)
    monkeypatch.setattr(auth, "_now", lambda: time.time() + 2 * 60 * 60)
    response = client.client.get("/")
    assert response.status_code == 200
    cookie = response.headers.get("set-cookie", "")
    assert get_settings().session_cookie_name in cookie and "HttpOnly" in cookie
    after = _expires_at(session_hash)
    assert after > before
    # The renewed cookie is a valid session for the same user with the same CSRF token.
    token = cookie.split(";")[0].split("=", 1)[1]
    payload = auth._serializer(get_settings()).loads(token)
    assert payload["user_login"] == "renew-due" and payload["csrf"] == client.csrf


def test_no_renewal_past_the_absolute_lifetime(client_as, monkeypatch):
    client = client_as("renew-capped")
    monkeypatch.setattr(auth, "_now", lambda: time.time() + auth.SESSION_ABSOLUTE_MAX_SECONDS + 60)
    response = client.client.get("/")
    assert response.status_code == 200
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


def test_session_cap_is_twenty_four_hours():
    assert auth._session_ttl(get_settings()) == 24 * 60 * 60
