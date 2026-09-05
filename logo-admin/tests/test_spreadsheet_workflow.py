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

    assert set(TARGET_FIELDS) == {"save_assignment", "set_store_pricing_tier", "deactivate_assignment",
                                  "remove_stock_override", "remove_sync_block", "delete_price_rule", "remove_mix_styles", "mixed"}
    assert not any(name.startswith("hard_delete") for name in TARGET_FIELDS)


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


def _mixed_csv(rows):
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream,fieldnames=list(dict.fromkeys(key for row in rows for key in row)))
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode()


@pytest.mark.asyncio
async def test_mixed_commands_and_delete_round_trip_with_counts(tmp_path):
    from mutations import MutationScope
    from snapshots import states_equal
    from staging import undo_change_set
    from tests.test_rule_agent_tools import _snapshot
    session_id,row,_data = _session_and_csv()
    rows = [
        {"command":"set_store_pricing_tier","fdm4_store":"S_TEST","tier_name":"MSRP"},
        {"command":"deactivate_assignment",**{k:row[k] for k in ("fdm4_store","product_style","garment_color_code","option_row","position")}},
        {"command":"remove_sync_block","store":"S_EMPTY","styles":"*"},
    ]
    scopes=(MutationScope("store_pricing_tier_row",{"fdm4_store":"S_TEST"}),MutationScope("assignment_store",{"fdm4_store":"S_TEST"}),MutationScope("sync_exclusion_row",{"fdm4_store":"S_EMPTY","style_code":""}))
    before=_snapshot(scopes)
    settings=_settings(tmp_path)
    job=await create_spreadsheet_job(session_id,"admin-one",_mixed_csv(rows),"mixed.csv","text/csv","",settings)
    assert job["mapping"]["_command_counts"]=={row["command"]:1 for row in rows}
    staged=confirm_spreadsheet_mapping(job["id"],"admin-one",job["mapping_revision"],job["mapping_hash"],50,settings)
    assert staged["status"]=="staged" and not staged["rejected_rows"]
    assert [r["tool_name"] for r in staged["change_set"]["items"]]==[r["command"] for r in rows]
    assert states_equal(before,_snapshot(scopes))
    change=staged["change_set"]
    apply_change_set(change["id"],"admin-one",revision=change["revision"],confirmed_hash=change["preview_hash"],acknowledge_hard_delete=False)
    assert not states_equal(before,_snapshot(scopes))
    undo_change_set(change["id"],"admin-one")
    assert states_equal(before,_snapshot(scopes))


@pytest.mark.asyncio
async def test_chunk_links_survive_failure_and_retry_then_apply_undo(tmp_path,monkeypatch):
    from tests.test_rule_agent_tools import _snapshot
    from mutations import MutationScope
    from snapshots import states_equal
    from staging import refresh_change_set,undo_change_set,get_change_set
    session_id,_row,_data=_session_and_csv()
    settings=_settings(tmp_path); settings.agent_max_change_set_items=2
    rows=[{"command":"set_store_pricing_tier","fdm4_store":"S_TEST","tier_name":"MSRP","note":str(i)} for i in range(5)]
    job=await create_spreadsheet_job(session_id,"admin-one",_mixed_csv(rows),"chunks.csv","text/csv","",settings)
    real=spreadsheet.staging.stage_write_batch
    calls=0
    def fail_second(*args,**kwargs):
        nonlocal calls
        result=real(*args,**kwargs); calls+=1
        if calls==2: raise RuntimeError("interrupted")
        return result
    monkeypatch.setattr(spreadsheet.staging,"stage_write_batch",fail_second)
    with pytest.raises(RuntimeError,match="interrupted"):
        confirm_spreadsheet_mapping(job["id"],"admin-one",job["mapping_revision"],job["mapping_hash"],2000,settings)
    links=get_spreadsheet_job(job["id"],"admin-one")["mapping"]["_change_set_ids"]
    assert len(links)==3 and len(set(links))==3
    monkeypatch.setattr(spreadsheet.staging,"stage_write_batch",real)
    staged=confirm_spreadsheet_mapping(job["id"],"admin-one",job["mapping_revision"],job["mapping_hash"],2000,settings)
    assert staged["change_set_ids"]==links
    assert [len(get_change_set(i,"admin-one")["items"]) for i in links]==[2,2,1]
    retry=confirm_spreadsheet_mapping(job["id"],"admin-one",job["mapping_revision"],job["mapping_hash"],2000,settings)
    assert retry["change_set_ids"]==links
    scopes=(MutationScope("store_pricing_tier_row",{"fdm4_store":"S_TEST"}),)
    before=_snapshot(scopes)
    for id in links:
        current=refresh_change_set(id,"admin-one")
        apply_change_set(id,"admin-one",revision=current["revision"],confirmed_hash=current["preview_hash"],acknowledge_hard_delete=False)
    for id in reversed(links): undo_change_set(id,"admin-one")
    assert states_equal(before,_snapshot(scopes))


@pytest.mark.asyncio
async def test_two_thousand_rows_respect_fifty_item_change_set_cap(tmp_path):
    session_id,_row,_data=_session_and_csv()
    settings=_settings(tmp_path); settings.agent_max_spreadsheet_rows=2000; settings.agent_max_change_set_items=50
    rows=[{"command":"set_store_pricing_tier","fdm4_store":"S_TEST","tier_name":"MSRP","note":str(i)} for i in range(2000)]
    job=await create_spreadsheet_job(session_id,"admin-one",_mixed_csv(rows),"2000.csv","text/csv","",settings)
    staged=confirm_spreadsheet_mapping(job["id"],"admin-one",job["mapping_revision"],job["mapping_hash"],2000,settings)
    assert len(staged["change_set_ids"])==40
    with database.cursor() as cursor:
        cursor.execute("SELECT change_set_id,count(*) AS n FROM logo.agent_change_set_item WHERE change_set_id=ANY(%s::uuid[]) GROUP BY change_set_id",(staged["change_set_ids"],))
        counts=[r["n"] for r in cursor.fetchall()]
    assert counts==[50]*40
    assert len(staged["change_set"]["items"])==50


@pytest.mark.asyncio
@pytest.mark.parametrize("command",["remove_stock_override","delete_price_rule","remove_mix_styles"])
async def test_sheet_remaining_deletes_stage_apply_exact_undo(tmp_path,command):
    from tests.test_rule_agent_tools import _admin,_snapshot
    from tests.test_warehouse_ops_tools import _rule
    from mutations import MutationScope
    from snapshots import states_equal
    from staging import undo_change_set
    session_id,_row,_data=_session_and_csv()
    if command=="remove_stock_override":
        _admin("INSERT INTO woo.stock_override(style_code,mode) VALUES ('STYLE-1','fake') ON CONFLICT(style_code) DO UPDATE SET mode='fake'")
        values={"style_code":"STYLE-1"}; scope=MutationScope("stock_override_row",values)
    elif command=="delete_price_rule":
        values={"rule_id":_rule()}; scope=MutationScope("price_rule_row",values)
    else:
        _admin("INSERT INTO woo.store_mix_item(fdm4_store,style_code,source) VALUES ('S_MIXED','MIX-2','manual') ON CONFLICT DO NOTHING")
        values={"store":"S_MIXED","styles":"MIX-2"}; scope=MutationScope("store_mix_items",{"fdm4_store":"S_MIXED"})
    before=_snapshot((scope,)); settings=_settings(tmp_path)
    job=await create_spreadsheet_job(session_id,"admin-one",_mixed_csv([{"command":command,**values}]),"delete.csv","text/csv","",settings)
    staged=confirm_spreadsheet_mapping(job["id"],"admin-one",job["mapping_revision"],job["mapping_hash"],50,settings)
    assert staged["status"]=="staged" and not staged["rejected_rows"],staged
    assert states_equal(before,_snapshot((scope,)))
    change=staged["change_set"]
    apply_change_set(change["id"],"admin-one",revision=change["revision"],confirmed_hash=change["preview_hash"],acknowledge_hard_delete=False)
    assert not states_equal(before,_snapshot((scope,)))
    undo_change_set(change["id"],"admin-one")
    assert states_equal(before,_snapshot((scope,)))


@pytest.mark.asyncio
async def test_mixed_sheet_blank_styles_cell_is_refused(tmp_path):
    session_id,_row,_data=_session_and_csv()
    settings=_settings(tmp_path)
    rows=[{"command":"set_store_pricing_tier","fdm4_store":"S_TEST","tier_name":"MSRP"},
          {"command":"remove_sync_block","store":"S_EMPTY","styles":""}]
    job=await create_spreadsheet_job(session_id,"admin-one",_mixed_csv(rows),"blank.csv","text/csv","",settings)
    staged=confirm_spreadsheet_mapping(job["id"],"admin-one",job["mapping_revision"],job["mapping_hash"],50,settings)
    assert [r["row"] for r in staged["rejected_rows"]]==[3] and "whole store" in staged["rejected_rows"][0]["detail"],staged["rejected_rows"]
    assert [i["tool_name"] for i in staged["change_set"]["items"]]==["set_store_pricing_tier"], "the rest of the sheet still stages"


@pytest.mark.asyncio
async def test_every_chunk_change_set_is_review_blocked_while_the_sheet_builds(tmp_path):
    from tests.test_rule_agent_tools import _admin
    import staging
    session_id,_row,_data=_session_and_csv()
    settings=_settings(tmp_path)
    rows=[{"command":"set_store_pricing_tier","fdm4_store":"S_TEST","tier_name":"MSRP","note":str(i)} for i in range(3)]
    job=await create_spreadsheet_job(session_id,"admin-one",_mixed_csv(rows),"chunks.csv","text/csv","",settings)
    with database.cursor(write=True,actor="admin-one") as cursor:
        first=str(staging.insert_change_set(cursor,session_id,"admin-one",24,origin="spreadsheet")["id"])
    links=spreadsheet._ensure_chunk_links(job["id"],"admin-one",first,3,1)
    assert len(links)==3
    _admin("UPDATE logo.agent_spreadsheet_job SET status='mapping_processing' WHERE id=%s",(job["id"],))
    with database.cursor() as cursor:
        assert all(staging._spreadsheet_build_in_progress(cursor,i,"admin-one") for i in links)
        assert not staging._spreadsheet_build_in_progress(cursor,links[1],"admin-two")
        for i in links[1:]:
            with pytest.raises(Conflict):
                staging._assert_review_ready(cursor,i,"admin-one")
    _admin("UPDATE logo.agent_spreadsheet_job SET status='staged' WHERE id=%s",(job["id"],))
    with database.cursor() as cursor:
        assert not any(staging._spreadsheet_build_in_progress(cursor,i,"admin-one") for i in links)
