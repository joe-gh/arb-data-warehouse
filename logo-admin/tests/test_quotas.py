import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from config import get_settings
from db import database
import quotas


def _settings(**changes):
    return replace(get_settings(), **changes)


def test_daily_reservations_cannot_oversubscribe():
    settings = _settings(
        agent_daily_token_cap=100,
        agent_monthly_token_cap=1_000,
        agent_requests_per_minute=10,
    )
    reservation = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=60,
        settings=settings,
    )
    with pytest.raises(quotas.QuotaExceeded, match="daily"):
        quotas.reserve(
            user_login="admin-one",
            reserved_tokens=50,
            settings=settings,
        )
    quotas.reconcile(reservation, input_tokens=20, output_tokens=10)


def test_monthly_cap_is_global_across_users():
    settings = _settings(
        agent_daily_token_cap=100,
        agent_monthly_token_cap=100,
        agent_requests_per_minute=10,
    )
    reservation = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=60,
        settings=settings,
    )
    with pytest.raises(quotas.QuotaExceeded, match="monthly"):
        quotas.reserve(
            user_login="admin-two",
            reserved_tokens=50,
            settings=settings,
        )
    quotas.reconcile(reservation)


def test_rate_limit_is_per_user():
    settings = _settings(
        agent_daily_token_cap=10_000,
        agent_monthly_token_cap=20_000,
        agent_requests_per_minute=1,
    )
    first = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=10,
        settings=settings,
    )
    with pytest.raises(quotas.QuotaExceeded, match="rate"):
        quotas.reserve(
            user_login="admin-one",
            reserved_tokens=10,
            settings=settings,
        )
    second = quotas.reserve(
        user_login="admin-two",
        reserved_tokens=10,
        settings=settings,
    )
    quotas.reconcile(first)
    quotas.reconcile(second)


def test_concurrent_reservations_do_not_oversubscribe():
    settings = _settings(
        agent_daily_token_cap=100,
        agent_monthly_token_cap=1_000,
        agent_requests_per_minute=10,
    )

    def attempt():
        try:
            return quotas.reserve(
                user_login="admin-one",
                reserved_tokens=60,
                settings=settings,
            )
        except quotas.QuotaExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    reservations = [result for result in results if result is not None]
    assert len(reservations) == 1
    quotas.reconcile(reservations[0])


def test_reconciliation_never_leaves_negative_reservation():
    settings = _settings(
        agent_daily_token_cap=1_000,
        agent_monthly_token_cap=10_000,
    )
    reservation = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=100,
        settings=settings,
    )
    quotas.reconcile(reservation, input_tokens=80, output_tokens=40)
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT reserved_tokens, input_tokens, output_tokens
              FROM logo.agent_usage_daily
             WHERE user_login = 'admin-one'
            """
        )
        row = cursor.fetchone()
    assert row["reserved_tokens"] == 0
    assert row["input_tokens"] == 80
    assert row["output_tokens"] == 40


def test_reconciliation_is_idempotent_and_cannot_release_another_reservation():
    settings = _settings(
        agent_daily_token_cap=1_000,
        agent_monthly_token_cap=10_000,
    )
    first = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=100,
        settings=settings,
    )
    second = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=200,
        settings=settings,
    )
    assert quotas.reconcile(first, input_tokens=10, output_tokens=5) is True
    assert quotas.reconcile(first, input_tokens=999, output_tokens=999) is False
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT reserved_tokens, input_tokens, output_tokens
              FROM logo.agent_usage_daily
             WHERE user_login = 'admin-one'
            """
        )
        aggregate = cursor.fetchone()
        cursor.execute(
            """
            SELECT status, input_tokens, output_tokens
              FROM logo.agent_quota_reservation WHERE id = %s
            """,
            (first.id,),
        )
        persisted = cursor.fetchone()
    assert aggregate["reserved_tokens"] == second.reserved_tokens
    assert aggregate["input_tokens"] == 10
    assert aggregate["output_tokens"] == 5
    assert dict(persisted) == {
        "status": "reconciled",
        "input_tokens": 10,
        "output_tokens": 5,
    }


def test_unknown_usage_is_finalized_as_retained_and_stays_charged():
    settings = _settings(
        agent_daily_token_cap=1_000,
        agent_monthly_token_cap=10_000,
    )
    reservation = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=100,
        settings=settings,
    )
    assert quotas.retain(reservation) is True
    assert quotas.retain(reservation) is False
    assert quotas.reconcile(reservation) is False
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT status FROM logo.agent_quota_reservation WHERE id = %s
            """,
            (reservation.id,),
        )
        assert cursor.fetchone()["status"] == "retained"
        cursor.execute(
            """
            SELECT reserved_tokens FROM logo.agent_usage_daily
             WHERE user_login = 'admin-one'
            """
        )
        assert cursor.fetchone()["reserved_tokens"] == 100


def test_process_loss_sweeper_releases_only_provably_unstarted_calls():
    settings = _settings(
        agent_daily_token_cap=1_000,
        agent_monthly_token_cap=10_000,
        agent_requests_per_minute=10,
    )
    unstarted = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=100,
        settings=settings,
    )
    started = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=200,
        settings=settings,
    )
    assert quotas.mark_provider_started(started) is True
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            """
            UPDATE logo.agent_quota_reservation
               SET expires_at = now() - interval '1 second'
             WHERE id IN (%s, %s)
            """,
            (unstarted.id, started.id),
        )
    assert quotas.sweep_stale_reservations() == {
        "released": 1,
        "retained": 1,
    }
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, status FROM logo.agent_quota_reservation
             WHERE id IN (%s, %s)
            """,
            (unstarted.id, started.id),
        )
        statuses = {row["id"]: row["status"] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT reserved_tokens FROM logo.agent_usage_daily
             WHERE user_login = 'admin-one'
            """
        )
        reserved = cursor.fetchone()["reserved_tokens"]
    assert statuses == {
        unstarted.id: "reconciled",
        started.id: "retained",
    }
    assert reserved == started.reserved_tokens


def test_concurrent_reconcile_finalizes_exact_reservation_once():
    reservation = quotas.reserve(
        user_login="admin-one",
        reserved_tokens=100,
        settings=_settings(
            agent_daily_token_cap=1_000,
            agent_monthly_token_cap=10_000,
            agent_requests_per_minute=10,
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _index: quotas.reconcile(
                reservation,
                input_tokens=10,
                output_tokens=5,
            ),
            range(2),
        ))
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_cancel_during_committing_reserve_reconciles_before_propagating(
    monkeypatch,
):
    started = Event()
    release = Event()
    reservation = SimpleNamespace(id="reserved")
    reconciled = []

    def slow_reserve(**_kwargs):
        started.set()
        release.wait(timeout=2)
        return reservation

    def record_reconcile(value, **usage):
        reconciled.append((value, usage))

    monkeypatch.setattr(quotas, "reserve", slow_reserve)
    monkeypatch.setattr(quotas, "reconcile", record_reconcile)
    task = asyncio.create_task(quotas.reserve_async(
        user_login="admin-one",
        reserved_tokens=10,
        settings=_settings(),
    ))
    await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reconciled == [(
        reservation,
        {"input_tokens": 0, "output_tokens": 0},
    )]
