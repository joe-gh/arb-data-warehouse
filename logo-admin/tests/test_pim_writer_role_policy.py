"""Canonical PIM role SQL is rerunnable and keeps the mirror's grants.

Requires a provisioned test database (TEST_DATABASE_DSN +
TEST_DATABASE_ADMIN_DSN).

The file revokes everything in schema pim and then re-grants a hand-written
list, so a table added by a later migration loses its privileges the next time
the policy is applied - which is how the WordPress placement mirror's CRUD on
pim.media_object / product_placement / media_rendition would disappear. The
manifest check makes an unlisted table stop the run instead.

The database name is substituted because the file names the production
database in its GRANT CONNECT / REVOKE ... ON DATABASE statements and the
harness database is disposable and differently named.
"""

import os
from pathlib import Path
import re

import psycopg2
import pytest


ROLE_SQL = (
    Path(__file__).resolve().parents[2] / "sql" / "pim_writer_role.sql"
)
MIRROR_TABLES = (
    "pim.media_object",
    "pim.product_placement",
    "pim.media_rendition",
)


def _policy_sql(database_name):
    # The harness database name carries capitals, so the substituted
    # identifier must be quoted or the server folds it to lower case.
    quoted = '"' + database_name.replace('"', '""') + '"'
    return ROLE_SQL.read_text().replace("DATABASE arb_warehouse", "DATABASE " + quoted)


@pytest.fixture
def admin():
    connection = psycopg2.connect(os.environ["TEST_DATABASE_ADMIN_DSN"])
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 'CREATE ROLE pim_writer LOGIN NOINHERIT NOSUPERUSER '
                       'NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
                 WHERE NOT EXISTS (
                     SELECT 1 FROM pg_roles WHERE rolname = 'pim_writer')
                """
            )
            statement = cursor.fetchone()
            if statement:
                cursor.execute(statement[0])
            # A previous aborted run can leave the probe table behind, and
            # its mere presence makes the policy refuse.
            cursor.execute("DROP TABLE IF EXISTS pim.zzz_probe")
            cursor.execute("SELECT current_database()")
            database_name = cursor.fetchone()[0]
        yield connection, database_name
    finally:
        _end_transaction(connection)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS pim.zzz_probe")
        connection.close()


def _apply(connection, database_name):
    with connection.cursor() as cursor:
        cursor.execute(_policy_sql(database_name))


def _end_transaction(connection):
    """The policy file runs its own BEGIN/COMMIT, so a refusal leaves the
    server inside a failed transaction that autocommit mode cannot see and
    connection.rollback() does not touch."""

    with connection.cursor() as cursor:
        cursor.execute("ROLLBACK")


def _privileges(connection, table):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT has_table_privilege('pim_writer', %s, 'SELECT'),
                   has_table_privilege('pim_writer', %s, 'INSERT'),
                   has_table_privilege('pim_writer', %s, 'UPDATE'),
                   has_table_privilege('pim_writer', %s, 'DELETE')
            """,
            (table, table, table, table),
        )
        return tuple(bool(value) for value in cursor.fetchone())


def test_manifest_check_runs_before_the_first_revoke():
    source = ROLE_SQL.read_text()
    first_revoke = re.search(r"(?m)^\s*(?:REVOKE\b|EXECUTE format\( 'REVOKE)",
                             source)
    assert first_revoke is not None
    assert source.index("$manifest$") < first_revoke.start()


def test_policy_keeps_the_placement_mirror_grants(admin):
    connection, database_name = admin
    _apply(connection, database_name)
    for table in MIRROR_TABLES:
        assert _privileges(connection, table) == (True, True, True, True), table
    # The original two are unchanged: append-only log, updatable state.
    assert _privileges(connection, "pim.ingest_event") == (
        True, True, False, False)
    assert _privileges(connection, "pim.product_state") == (
        True, True, True, False)


def test_reapplying_the_policy_is_idempotent(admin):
    connection, database_name = admin
    _apply(connection, database_name)
    _apply(connection, database_name)
    for table in MIRROR_TABLES:
        assert _privileges(connection, table) == (True, True, True, True), table


def test_unlisted_pim_table_stops_the_policy_before_it_revokes(admin):
    connection, database_name = admin
    _apply(connection, database_name)
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE pim.zzz_probe (id integer PRIMARY KEY)")
    try:
        with pytest.raises(psycopg2.Error) as raised:
            _apply(connection, database_name)
        assert "zzz_probe" in str(raised.value)
        # The file opens its own transaction, so the refusal leaves this
        # session in a failed one until it is rolled back.
        _end_transaction(connection)
        # Refusing must not have stripped the live grants on the way.
        for table in MIRROR_TABLES:
            assert _privileges(connection, table) == (
                True, True, True, True), table
    finally:
        _end_transaction(connection)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS pim.zzz_probe")
