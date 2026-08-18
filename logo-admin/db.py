"""Small synchronous psycopg2 connection-pool wrapper."""

from contextlib import contextmanager
import threading
from typing import Iterator
import uuid

from psycopg2.extras import RealDictCursor, register_uuid
from psycopg2.pool import ThreadedConnectionPool

from config import get_settings

# psycopg2 has no native uuid.UUID handling, but the app passes UUID objects
# (agent session / change-set / item / journal ids) as query parameters and its
# code and tests expect uuid columns to read back as uuid.UUID. register_uuid()
# enables both directions process-wide (UUID params adapt to text; uuid columns
# cast to uuid.UUID on read). JSON paths already serialize UUID via default=str.
register_uuid()


EXPECTED_DATABASE_ROLE = "logo_admin"


class Database:
    """Lazily owns the process-wide threaded PostgreSQL connection pool."""

    def __init__(self) -> None:
        self._pool = None
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._pool is not None:
            return
        with self._lock:
            if self._pool is None:
                settings = get_settings()
                pool = ThreadedConnectionPool(
                    settings.db_pool_min,
                    settings.db_pool_max,
                    dsn=settings.database_dsn,
                    application_name="arb_logo_admin",
                )
                connection = pool.getconn()
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT current_user, session_user")
                        current_user, session_user = map(
                            str,
                            cursor.fetchone(),
                        )
                    connection.rollback()
                    if (
                        current_user != EXPECTED_DATABASE_ROLE
                        or session_user != EXPECTED_DATABASE_ROLE
                    ):
                        raise RuntimeError(
                            "DATABASE_DSN must authenticate directly as the "
                            "logo_admin role; SET ROLE is not allowed"
                        )
                except Exception:
                    connection.rollback()
                    pool.putconn(connection)
                    pool.closeall()
                    raise
                pool.putconn(connection)
                self._pool = pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None

    @contextmanager
    def cursor(
        self,
        *,
        write: bool = False,
        actor: str = "",
        commit_on_success: bool = True,
    ) -> Iterator[RealDictCursor]:
        """Yield a dictionary cursor inside a committed/rolled-back transaction.

        ``actor`` names the human operator for this transaction; the audit
        triggers on logo.* read it from the transaction-local ``logo.actor``
        setting so every row change is attributed in logo.audit_log.
        """

        self.open()
        pool = self._pool
        if pool is None:  # Defensive; open() either initializes or raises.
            raise RuntimeError("database pool is unavailable")

        connection = pool.getconn()
        discard = False
        try:
            connection.set_session(autocommit=False, readonly=not write)
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SET LOCAL statement_timeout = '30s'")
                if write and actor:
                    cursor.execute(
                        "SELECT set_config('logo.actor', %s, true)", (str(actor)[:100],)
                    )
                yield cursor
            if commit_on_success:
                connection.commit()
            else:
                connection.rollback()
        except Exception:
            try:
                connection.rollback()
            except Exception:
                discard = True
            raise
        finally:
            pool.putconn(connection, close=discard)

    @contextmanager
    def streaming_cursor(self, *, batch_size: int = 500) -> Iterator[RealDictCursor]:
        """Yield a read-only server-side cursor for bounded-memory exports."""

        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        self.open()
        pool = self._pool
        if pool is None:
            raise RuntimeError("database pool is unavailable")

        connection = pool.getconn()
        discard = False
        try:
            connection.set_session(autocommit=False, readonly=True)
            with connection.cursor() as control:
                control.execute("SET LOCAL statement_timeout = '30s'")
            cursor_name = f"logo_stream_{uuid.uuid4().hex}"
            with connection.cursor(
                name=cursor_name,
                cursor_factory=RealDictCursor,
            ) as cursor:
                cursor.itersize = batch_size
                yield cursor
            connection.rollback()
        except Exception:
            try:
                connection.rollback()
            except Exception:
                discard = True
            raise
        finally:
            pool.putconn(connection, close=discard)


database = Database()
