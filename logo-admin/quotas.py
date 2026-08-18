"""Persistent, transaction-safe cost and request reservations."""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
import uuid

from config import Settings, get_settings
from db import database


class QuotaExceeded(RuntimeError):
    """Raised before a provider call when a configured cap is exhausted."""


@dataclass(frozen=True)
class Reservation:
    id: uuid.UUID
    user_login: str
    usage_day: date
    usage_month: date
    window_start: datetime
    reserved_tokens: int


def reserve(
    *,
    user_login: str,
    reserved_tokens: int,
    settings: Settings | None = None,
) -> Reservation:
    active_settings = settings or get_settings()
    reservation_id = uuid.uuid4()
    requested = int(reserved_tokens)
    if requested < 1:
        raise ValueError("reserved_tokens must be positive")
    if (
        requested > active_settings.agent_daily_token_cap
        or requested > active_settings.agent_monthly_token_cap
    ):
        raise QuotaExceeded("Agent token budget exceeded")

    try:
        with database.cursor(
            write=True,
            actor=f"agent-quota:{user_login}",
        ) as cursor:
            cursor.execute(
                """
                INSERT INTO logo.agent_rate_window (
                    user_login, window_start, requests
                ) VALUES (%s, date_trunc('minute', now()), 1)
                ON CONFLICT (user_login, window_start) DO UPDATE SET
                    requests = logo.agent_rate_window.requests + 1
                WHERE logo.agent_rate_window.requests < %s
                RETURNING window_start
                """,
                (user_login, active_settings.agent_requests_per_minute),
            )
            rate = cursor.fetchone()
            if rate is None:
                raise QuotaExceeded("Agent request-rate limit exceeded")

            cursor.execute(
                """
                INSERT INTO logo.agent_usage_daily (
                    user_login, usage_day, requests, reserved_tokens,
                    input_tokens, output_tokens, updated_at
                ) VALUES (%s, current_date, 1, %s, 0, 0, now())
                ON CONFLICT (user_login, usage_day) DO UPDATE SET
                    requests = logo.agent_usage_daily.requests + 1,
                    reserved_tokens = (
                        logo.agent_usage_daily.reserved_tokens
                        + EXCLUDED.reserved_tokens
                    ),
                    updated_at = now()
                WHERE (
                    logo.agent_usage_daily.input_tokens
                    + logo.agent_usage_daily.output_tokens
                    + logo.agent_usage_daily.reserved_tokens
                    + EXCLUDED.reserved_tokens
                ) <= %s
                RETURNING usage_day
                """,
                (
                    user_login,
                    requested,
                    active_settings.agent_daily_token_cap,
                ),
            )
            daily = cursor.fetchone()
            if daily is None:
                raise QuotaExceeded("Agent daily token budget exceeded")

            cursor.execute(
                """
                INSERT INTO logo.agent_usage_monthly (
                    usage_month, requests, reserved_tokens,
                    input_tokens, output_tokens, updated_at
                ) VALUES (
                    date_trunc('month', current_date)::date,
                    1, %s, 0, 0, now()
                )
                ON CONFLICT (usage_month) DO UPDATE SET
                    requests = logo.agent_usage_monthly.requests + 1,
                    reserved_tokens = (
                        logo.agent_usage_monthly.reserved_tokens
                        + EXCLUDED.reserved_tokens
                    ),
                    updated_at = now()
                WHERE (
                    logo.agent_usage_monthly.input_tokens
                    + logo.agent_usage_monthly.output_tokens
                    + logo.agent_usage_monthly.reserved_tokens
                    + EXCLUDED.reserved_tokens
                ) <= %s
                RETURNING usage_month
                """,
                (requested, active_settings.agent_monthly_token_cap),
            )
            monthly = cursor.fetchone()
            if monthly is None:
                raise QuotaExceeded("Agent monthly token budget exceeded")

            cursor.execute(
                """
                INSERT INTO logo.agent_quota_reservation (
                    id, user_login, usage_day, usage_month,
                    window_start, reserved_tokens, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    now() + make_interval(secs => %s)
                )
                """,
                (
                    reservation_id,
                    user_login,
                    daily["usage_day"],
                    monthly["usage_month"],
                    rate["window_start"],
                    requested,
                    max(
                        300,
                        int(active_settings.agent_turn_timeout_seconds) + 120,
                    ),
                ),
            )

            return Reservation(
                id=reservation_id,
                user_login=user_login,
                usage_day=daily["usage_day"],
                usage_month=monthly["usage_month"],
                window_start=rate["window_start"],
                reserved_tokens=requested,
            )
    except QuotaExceeded:
        # Raising inside Database.cursor rolls back rate/daily/monthly together.
        raise


async def reserve_async(
    *,
    user_login: str,
    reserved_tokens: int,
    settings: Settings | None = None,
) -> Reservation:
    """Cancellation-safe async boundary for the committing reservation call."""

    task = asyncio.create_task(asyncio.to_thread(
        reserve,
        user_login=user_login,
        reserved_tokens=reserved_tokens,
        settings=settings,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            reservation = await asyncio.shield(task)
        except Exception:
            pass
        else:
            await asyncio.shield(asyncio.to_thread(
                reconcile,
                reservation,
                input_tokens=0,
                output_tokens=0,
            ))
        raise


def reconcile(
    reservation: Reservation,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> bool:
    used_input = max(0, int(input_tokens))
    used_output = max(0, int(output_tokens))
    with database.cursor(
        write=True,
        actor=f"agent-quota:{reservation.user_login}",
    ) as cursor:
        cursor.execute(
            """
            SELECT * FROM logo.agent_quota_reservation
             WHERE id = %s AND user_login = %s
             FOR UPDATE
            """,
            (reservation.id, reservation.user_login),
        )
        persisted = cursor.fetchone()
        if persisted is None:
            raise RuntimeError("agent reservation no longer exists")
        if persisted["status"] != "reserved":
            return False
        if int(persisted["reserved_tokens"]) != reservation.reserved_tokens:
            raise RuntimeError("agent reservation amount changed")
        cursor.execute(
            """
            UPDATE logo.agent_usage_daily
               SET reserved_tokens = reserved_tokens - %s,
                   input_tokens = input_tokens + %s,
                   output_tokens = output_tokens + %s,
                   updated_at = now()
             WHERE user_login = %s AND usage_day = %s
               AND reserved_tokens >= %s
            """,
            (
                reservation.reserved_tokens,
                used_input,
                used_output,
                reservation.user_login,
                reservation.usage_day,
                reservation.reserved_tokens,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("daily agent reservation no longer exists")
        cursor.execute(
            """
            UPDATE logo.agent_usage_monthly
               SET reserved_tokens = reserved_tokens - %s,
                   input_tokens = input_tokens + %s,
                   output_tokens = output_tokens + %s,
                   updated_at = now()
             WHERE usage_month = %s AND reserved_tokens >= %s
            """,
            (
                reservation.reserved_tokens,
                used_input,
                used_output,
                reservation.usage_month,
                reservation.reserved_tokens,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("monthly agent reservation no longer exists")
        cursor.execute(
            """
            UPDATE logo.agent_quota_reservation
               SET status = 'reconciled', input_tokens = %s,
                   output_tokens = %s, finalized_at = now()
             WHERE id = %s AND user_login = %s AND status = 'reserved'
            """,
            (
                used_input,
                used_output,
                reservation.id,
                reservation.user_login,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("agent reservation reconciliation raced")
        return True


def mark_provider_started(reservation: Reservation) -> bool:
    """Durably mark the conservative point after which usage may be billable."""

    with database.cursor(
        write=True,
        actor=f"agent-quota:{reservation.user_login}",
    ) as cursor:
        cursor.execute(
            """
            UPDATE logo.agent_quota_reservation
               SET provider_started_at = coalesce(provider_started_at, now())
             WHERE id = %s AND user_login = %s
               AND status = 'reserved' AND reserved_tokens = %s
            """,
            (
                reservation.id,
                reservation.user_login,
                reservation.reserved_tokens,
            ),
        )
        return cursor.rowcount == 1


def retain(reservation: Reservation) -> bool:
    """Finalize unknown provider usage while keeping its tokens charged."""

    with database.cursor(
        write=True,
        actor=f"agent-quota:{reservation.user_login}",
    ) as cursor:
        cursor.execute(
            """
            UPDATE logo.agent_quota_reservation
               SET status = 'retained', finalized_at = now()
             WHERE id = %s AND user_login = %s
               AND status = 'reserved'
               AND reserved_tokens = %s
            """,
            (
                reservation.id,
                reservation.user_login,
                reservation.reserved_tokens,
            ),
        )
        return cursor.rowcount == 1


def sweep_stale_reservations(*, limit: int = 1_000) -> dict[str, int]:
    """Recover crash leftovers without guessing about possibly billed calls."""

    safe_limit = max(1, min(10_000, int(limit)))
    released = 0
    retained = 0
    with database.cursor(write=True, actor="agent-maintenance") as cursor:
        cursor.execute(
            """
            SELECT * FROM logo.agent_quota_reservation
             WHERE status = 'reserved' AND expires_at < now()
             ORDER BY expires_at, id
             FOR UPDATE SKIP LOCKED
             LIMIT %s
            """,
            (safe_limit,),
        )
        for row in cursor.fetchall():
            if row["provider_started_at"] is not None:
                cursor.execute(
                    """
                    UPDATE logo.agent_quota_reservation
                       SET status = 'retained', finalized_at = now()
                     WHERE id = %s AND status = 'reserved'
                    """,
                    (row["id"],),
                )
                retained += cursor.rowcount
                continue
            amount = int(row["reserved_tokens"])
            cursor.execute(
                """
                UPDATE logo.agent_usage_daily
                   SET reserved_tokens = reserved_tokens - %s,
                       updated_at = now()
                 WHERE user_login = %s AND usage_day = %s
                   AND reserved_tokens >= %s
                """,
                (amount, row["user_login"], row["usage_day"], amount),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale daily reservation cannot be released")
            cursor.execute(
                """
                UPDATE logo.agent_usage_monthly
                   SET reserved_tokens = reserved_tokens - %s,
                       updated_at = now()
                 WHERE usage_month = %s AND reserved_tokens >= %s
                """,
                (amount, row["usage_month"], amount),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale monthly reservation cannot be released")
            cursor.execute(
                """
                UPDATE logo.agent_quota_reservation
                   SET status = 'reconciled', input_tokens = 0,
                       output_tokens = 0, finalized_at = now()
                 WHERE id = %s AND status = 'reserved'
                """,
                (row["id"],),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale reservation finalization raced")
            released += 1
    return {"released": released, "retained": retained}
