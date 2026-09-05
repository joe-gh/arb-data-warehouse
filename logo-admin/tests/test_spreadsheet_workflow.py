"""Spreadsheet changes require mapping confirmation and change-set confirmation."""

import csv
import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier, Event
import time
import tracemalloc
from types import SimpleNamespace
from uuid import uuid4

import pytest

from db import database
from domain import Conflict, InvalidCommand, NotFound
import spreadsheet
from spreadsheet import (
    ASSIGNMENT_COLUMNS,
    confirm_spreadsheet_mapping,
    create_spreadsheet_job,
    get_spreadsheet_job,
)
from staging import apply_change_set


def _settings(tmp_path):
    return SimpleNamespace(
        agent_upload_dir=tmp_path,
        agent_max_spreadsheet_bytes=5 * 1024 * 1024,
        agent_max_spreadsheet_rows=500,
        agent_max_spreadsheet_columns=40,
        agent_max_cell_chars=2000,
        agent_max_xlsx_entries=200,
        agent_max_xlsx_uncompressed_bytes=50 * 1024 * 1024,
        agent_daily_token_cap=100_000,
        agent_monthly_token_cap=2_000_000,
        agent_requests_per_minute=100,
        agent_turn_timeout_seconds=60,
    )


def _session_and_csv():
    session_id = uuid4()
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            "SELECT * FROM logo.assignment "
            "ORDER BY fdm4_store,product_style,garment_color_code,option_row,position LIMIT 1"
        )
        row = dict(cursor.fetchone())
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,now()+interval '1 hour')",
            (session_id, "admin-one", "spreadsheet workflow"),
        )
    values = {column: row[column] for column in ASSIGNMENT_COLUMNS}
    values["location"] = "SPREADSHEET CHANGE"
    values["image_url"] = "https://example.test/spreadsheet.png"
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=ASSIGNMENT_COLUMNS)
    writer.writeheader()
    writer.writerow(values)
    return session_id, row, output.getvalue().encode("utf-8")


def _location(row):
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT location FROM logo.assignment
             WHERE fdm4_store=%s AND product_style=%s
               AND garment_color_code=%s AND option_row=%s AND position=%s
            """,
            (
                row["fdm4_store"], row["product_style"], row["garment_color_code"],
                row["option_row"], row["position"],
            ),
        )
        return cursor.fetchone()["location"]


@pytest.mark.asyncio
async def test_known_csv_requires_mapping_then_change_set_confirmation(tmp_path):
    session_id, row, data = _session_and_csv()
    settings = _settings(tmp_path)
    job = await create_spreadsheet_job(
        session_id,
        "admin-one",
        data,
        "assignments.csv",
        "text/csv",
        "update placement",
        settings,
    )
    assert job["status"] == "mapping_pending"
    assert _location(row) == row["location"]
    staged = confirm_spreadsheet_mapping(
        job["id"],
        "admin-one",
        job["mapping_revision"],
        job["mapping_hash"],
        50,
        settings,
    )
    assert staged["status"] == "staged"
    assert staged["change_set"]["status"] == "pending"
    assert _location(row) == row["location"]
    change_set = staged["change_set"]
    apply_change_set(
        change_set["id"],
        "admin-one",
        revision=change_set["revision"],
        confirmed_hash=change_set["preview_hash"],
        acknowledge_hard_delete=False,
    )
    assert _location(row) == "SPREADSHEET CHANGE"
    assert not (tmp_path / f"{job['storage_key']}.upload").exists()


@pytest.mark.asyncio
async def test_mapping_owner_and_confirmation_hash_are_enforced(tmp_path):
    session_id, _row, data = _session_and_csv()
    settings = _settings(tmp_path)
    job = await create_spreadsheet_job(
        session_id, "admin-one", data, "assignments.csv", "text/csv", "", settings
    )
    with pytest.raises(NotFound):
        get_spreadsheet_job(job["id"], "admin-two")
    with pytest.raises(Conflict, match="stale"):
        confirm_spreadsheet_mapping(
            job["id"], "admin-one", job["mapping_revision"], "0" * 64, 50, settings
        )


@pytest.mark.asyncio
async def test_private_file_tamper_is_rejected_before_staging(tmp_path):
    session_id, row, data = _session_and_csv()
    settings = _settings(tmp_path)
    job = await create_spreadsheet_job(
        session_id, "admin-one", data, "assignments.csv", "text/csv", "", settings
    )
    (tmp_path / f"{job['storage_key']}.upload").write_bytes(b"tampered")
    with pytest.raises(Conflict, match="no longer matches"):
        confirm_spreadsheet_mapping(
            job["id"],
            "admin-one",
            job["mapping_revision"],
            job["mapping_hash"],
            50,
            settings,
        )
    assert _location(row) == row["location"]


def test_spreadsheet_mapping_schema_cannot_express_hard_delete():
    from spreadsheet_mapping import TARGET_FIELDS

    assert set(TARGET_FIELDS) == {"save_assignment", "set_store_pricing_tier"}


@pytest.mark.asyncio
async def test_staging_retry_after_process_failure_reuses_one_linked_change_set(
    tmp_path,
    monkeypatch,
):
    session_id, _row, one_row_csv = _session_and_csv()
    source = list(csv.DictReader(StringIO(one_row_csv.decode("utf-8"))))[0]
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=ASSIGNMENT_COLUMNS)
    writer.writeheader()
    for index in range(3):
        repeated = dict(source)
        repeated["location"] = f"RECOVERY {index}"
        writer.writerow(repeated)
    settings = _settings(tmp_path)
    job = await create_spreadsheet_job(
        session_id,
        "admin-one",
        output.getvalue().encode("utf-8"),
        "recover.csv",
        "text/csv",
        "",
        settings,
    )

    original_stage_batch = spreadsheet.staging.stage_write_batch

    def crash_after_atomic_batch(*args, **kwargs):
        original_stage_batch(*args, **kwargs)
        raise RuntimeError("simulated process loss")

    monkeypatch.setattr(
        spreadsheet.staging,
        "stage_write_batch",
        crash_after_atomic_batch,
    )
    with pytest.raises(RuntimeError, match="process loss"):
        confirm_spreadsheet_mapping(
            job["id"],
            "admin-one",
            job["mapping_revision"],
            job["mapping_hash"],
            50,
            settings,
        )

    interrupted = get_spreadsheet_job(job["id"], "admin-one")
    assert interrupted["status"] == "mapping_confirmed"
    assert interrupted["change_set_id"] is not None
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) AS count
              FROM logo.agent_change_set
             WHERE session_id = %s AND user_login = 'admin-one'
               AND origin = 'spreadsheet'
            """,
            (session_id,),
        )
        assert cursor.fetchone()["count"] == 1
        cursor.execute(
            """
            SELECT count(*) AS count
              FROM logo.agent_change_set_item
             WHERE change_set_id = %s AND user_login = 'admin-one'
            """,
            (interrupted["change_set_id"],),
        )
        assert cursor.fetchone()["count"] == 3

    monkeypatch.setattr(
        spreadsheet.staging,
        "stage_write_batch",
        original_stage_batch,
    )
    resumed = confirm_spreadsheet_mapping(
        job["id"],
        "admin-one",
        job["mapping_revision"],
        job["mapping_hash"],
        50,
        settings,
    )
    assert resumed["status"] == "staged"
    assert resumed["change_set_id"] == interrupted["change_set_id"]
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) AS change_sets
              FROM logo.agent_change_set
             WHERE session_id = %s AND user_login = 'admin-one'
               AND origin = 'spreadsheet'
            """,
            (session_id,),
        )
        assert cursor.fetchone()["change_sets"] == 1
        cursor.execute(
            """
            SELECT count(*) AS items,
                   count(DISTINCT call_id) AS distinct_calls
              FROM logo.agent_change_set_item
             WHERE change_set_id = %s AND user_login = 'admin-one'
            """,
            (interrupted["change_set_id"],),
        )
        counts = cursor.fetchone()
    assert counts["items"] == 3
    assert counts["distinct_calls"] == 3


@pytest.mark.asyncio
async def test_simultaneous_mapping_confirms_return_one_canonical_change_set(
    tmp_path,
    monkeypatch,
):
    session_id, _row, data = _session_and_csv()
    settings = _settings(tmp_path)
    job = await create_spreadsheet_job(
        session_id,
        "admin-one",
        data,
        "concurrent-confirm.csv",
        "text/csv",
        "",
        settings,
    )
    rendezvous = Barrier(2)
    original_stage_batch = spreadsheet.staging.stage_write_batch

    def synchronized_stage_batch(*args, **kwargs):
        rendezvous.wait(timeout=5)
        return original_stage_batch(*args, **kwargs)

    monkeypatch.setattr(
        spreadsheet.staging,
        "stage_write_batch",
        synchronized_stage_batch,
    )

    def confirm():
        return confirm_spreadsheet_mapping(
            job["id"],
            "admin-one",
            job["mapping_revision"],
            job["mapping_hash"],
            50,
            settings,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: confirm(), range(2)))

    assert [result["status"] for result in results] == ["staged", "staged"]
    change_set_ids = {result["change_set_id"] for result in results}
    assert len(change_set_ids) == 1
    change_set_id = change_set_ids.pop()
    assert all(result["change_set"]["id"] == change_set_id for result in results)
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) AS change_sets
              FROM logo.agent_change_set
             WHERE session_id = %s AND user_login = 'admin-one'
               AND origin = 'spreadsheet'
            """,
            (session_id,),
        )
        assert cursor.fetchone()["change_sets"] == 1
        cursor.execute(
            """
            SELECT count(*) AS items,
                   count(DISTINCT call_id) AS distinct_calls
              FROM logo.agent_change_set_item
             WHERE change_set_id = %s AND user_login = 'admin-one'
            """,
            (change_set_id,),
        )
        counts = cursor.fetchone()
    assert counts["items"] == 1
    assert counts["distinct_calls"] == 1
    assert not (tmp_path / f"{job['storage_key']}.upload").exists()


@pytest.mark.asyncio
async def test_maximum_spreadsheet_stages_with_one_cumulative_preview(
    tmp_path,
    monkeypatch,
):
    session_id, _row, one_row_csv = _session_and_csv()
    source = list(csv.DictReader(StringIO(one_row_csv.decode("utf-8"))))[0]
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=ASSIGNMENT_COLUMNS)
    writer.writeheader()
    for index in range(500):
        repeated = dict(source)
        repeated["location"] = f"BOUNDED BATCH {index}"
        writer.writerow(repeated)
    settings = _settings(tmp_path)
    settings.agent_max_spreadsheet_rows = 500
    job = await create_spreadsheet_job(
        session_id,
        "admin-one",
        output.getvalue().encode("utf-8"),
        "maximum.csv",
        "text/csv",
        "",
        settings,
    )
    original_preview = spreadsheet.staging._preview_batch_candidates
    preview_calls = 0

    def counted_preview(*args, **kwargs):
        nonlocal preview_calls
        preview_calls += 1
        return original_preview(*args, **kwargs)

    monkeypatch.setattr(
        spreadsheet.staging,
        "_preview_batch_candidates",
        counted_preview,
    )
    tracemalloc.start()
    started_at = time.monotonic()
    try:
        staged = confirm_spreadsheet_mapping(
            job["id"],
            "admin-one",
            job["mapping_revision"],
            job["mapping_hash"],
            500,
            settings,
        )
        elapsed = time.monotonic() - started_at
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert staged["status"] == "staged"
    assert len(staged["change_set"]["items"]) == 500
    assert preview_calls == 1
    assert elapsed < 60
    assert peak < 128 * 1024 * 1024


@pytest.mark.asyncio
async def test_retry_terminalizes_job_when_linked_change_set_was_discarded(tmp_path):
    session_id, _row, data = _session_and_csv()
    settings = _settings(tmp_path)
    job = await create_spreadsheet_job(
        session_id,
        "admin-one",
        data,
        "lifecycle.csv",
        "text/csv",
        "",
        settings,
    )
    linked_job, change_set = spreadsheet._ensure_linked_change_set(
        job["id"],
        "admin-one",
        job["mapping_revision"],
        job["mapping_hash"],
    )
    assert change_set is not None
    # Simulate a set discarded by an earlier process. The current public
    # discard operation correctly refuses a still-building spreadsheet set.
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute("UPDATE logo.agent_change_set SET status = 'discarded' WHERE id = %s", (change_set["id"],))

    with pytest.raises(Conflict, match="no longer pending"):
        confirm_spreadsheet_mapping(
            job["id"],
            "admin-one",
            job["mapping_revision"],
            job["mapping_hash"],
            50,
            settings,
        )
    rejected = get_spreadsheet_job(job["id"], "admin-one")
    assert rejected["status"] == "rejected"
    assert rejected["change_set_id"] == linked_job["change_set_id"]
    assert not (tmp_path / f"{job['storage_key']}.upload").exists()


@pytest.mark.asyncio
async def test_partially_building_spreadsheet_set_cannot_be_applied_or_discarded(
    tmp_path,
):
    session_id, _row, data = _session_and_csv()
    settings = _settings(tmp_path)
    job = await create_spreadsheet_job(
        session_id,
        "admin-one",
        data,
        "building.csv",
        "text/csv",
        "",
        settings,
    )
    _linked_job, change_set = spreadsheet._ensure_linked_change_set(
        job["id"],
        "admin-one",
        job["mapping_revision"],
        job["mapping_hash"],
    )
    assert change_set is not None
    detail = spreadsheet.staging.get_change_set(
        change_set["id"],
        "admin-one",
    )
    assert detail["review_blocked"] is True
    with pytest.raises(Conflict, match="still being staged"):
        apply_change_set(
            change_set["id"],
            "admin-one",
            revision=0,
            confirmed_hash="",
            acknowledge_hard_delete=False,
        )
    with pytest.raises(Conflict, match="still being staged"):
        spreadsheet.staging.discard_change_set(
            change_set["id"],
            "admin-one",
        )

    completed = confirm_spreadsheet_mapping(
        job["id"],
        "admin-one",
        job["mapping_revision"],
        job["mapping_hash"],
        50,
        settings,
    )
    assert completed["status"] == "staged"
    assert completed["change_set"]["review_blocked"] is False


def test_concurrent_upload_reservations_cannot_exceed_per_user_job_cap(tmp_path):
    session_id, _row, data = _session_and_csv()
    settings = _settings(tmp_path)
    def persist(index):
        try:
            return spreadsheet._reserve_job(
                session_id=session_id,
                user_login="admin-one",
                data=data,
                filename=f"concurrent-{index}.csv",
                media_type="text/csv",
                settings=settings,
            )
        except InvalidCommand:
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(persist, range(12)))

    accepted = [result for result in results if result is not None]
    assert len(accepted) == spreadsheet.MAX_ACTIVE_SPREADSHEET_JOBS_PER_USER
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) AS count
              FROM logo.agent_spreadsheet_job
             WHERE user_login = 'admin-one'
               AND status = 'mapping_processing'
            """
        )
        assert cursor.fetchone()["count"] == len(accepted)


def test_global_active_upload_cap_rejects_before_private_file_write(tmp_path):
    session_id, _row, data = _session_and_csv()
    settings = _settings(tmp_path)
    with database.cursor(write=True, actor="fixture") as cursor:
        for index in range(spreadsheet.MAX_ACTIVE_SPREADSHEET_JOBS_GLOBAL):
            other_session = uuid4()
            other_user = f"global-user-{index}"
            cursor.execute(
                """
                INSERT INTO logo.agent_chat_session (
                    id, user_login, title, expires_at
                ) VALUES (%s, %s, 'global capacity', now() + interval '1 hour')
                """,
                (other_session, other_user),
            )
            cursor.execute(
                """
                INSERT INTO logo.agent_spreadsheet_job (
                    id, session_id, user_login, storage_key,
                    original_name, media_type, byte_size, sha256,
                    format_name, status, mapping_hash, mapping, expires_at
                ) VALUES (
                    %s, %s, %s, %s,
                    'held.csv', 'text/csv', 1, %s,
                    'csv', 'mapping_pending', %s, '{}'::jsonb,
                    now() + interval '1 hour'
                )
                """,
                (
                    uuid4(),
                    other_session,
                    other_user,
                    uuid4(),
                    "a" * 64,
                    "b" * 64,
                ),
            )

    with pytest.raises(InvalidCommand, match="storage capacity"):
        spreadsheet._reserve_job(
            session_id=session_id,
            user_login="admin-one",
            data=data,
            filename="rejected.csv",
            media_type="text/csv",
            settings=settings,
        )
    assert list(tmp_path.glob("*.upload")) == []


@pytest.mark.asyncio
async def test_full_user_capacity_rejects_before_parser_or_mapping_provider(
    tmp_path,
    monkeypatch,
):
    session_id, _row, data = _session_and_csv()
    settings = _settings(tmp_path)
    with database.cursor(write=True, actor="fixture") as cursor:
        for index in range(spreadsheet.MAX_ACTIVE_SPREADSHEET_JOBS_PER_USER):
            cursor.execute(
                """
                INSERT INTO logo.agent_spreadsheet_job (
                    id, session_id, user_login, storage_key,
                    original_name, media_type, byte_size, sha256,
                    format_name, status, mapping_hash, mapping, expires_at
                ) VALUES (
                    %s, %s, 'admin-one', %s,
                    'held.csv', 'text/csv', 1, %s,
                    'csv', 'mapping_pending', %s, '{}'::jsonb,
                    now() + interval '1 hour'
                )
                """,
                (uuid4(), session_id, uuid4(), "a" * 64, "b" * 64),
            )

    def parser_must_not_run(*_args, **_kwargs):
        raise AssertionError("parser/provider work happened before admission")

    monkeypatch.setattr(spreadsheet, "parse_spreadsheet", parser_must_not_run)
    monkeypatch.setattr(spreadsheet, "propose_mapping", parser_must_not_run)
    with pytest.raises(InvalidCommand, match="Too many active"):
        await create_spreadsheet_job(
            session_id,
            "admin-one",
            data,
            "rejected.csv",
            "text/csv",
            "",
            settings,
        )


@pytest.mark.asyncio
async def test_cancel_during_upload_reservation_compensates_committed_job(
    tmp_path,
    monkeypatch,
):
    started = Event()
    release = Event()
    job = {"id": uuid4(), "storage_key": uuid4()}
    discarded = []

    def slow_reserve(**_kwargs):
        started.set()
        release.wait(timeout=2)
        return job

    def discard(value, user_login, settings):
        discarded.append((value, user_login, settings))

    monkeypatch.setattr(spreadsheet, "_reserve_job", slow_reserve)
    monkeypatch.setattr(spreadsheet, "_discard_reserved_job", discard)
    settings = _settings(tmp_path)
    task = asyncio.create_task(spreadsheet._reserve_job_cancellation_safe(
        session_id=uuid4(),
        user_login="admin-one",
        data=b"a,b\n1,2\n",
        filename="cancel.csv",
        media_type="text/csv",
        settings=settings,
    ))
    await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert discarded == [(job, "admin-one", settings)]
