"""Retention cleans expired transient state without touching durable journals."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from db import database
import maintenance


def _expired_state(tmp_path):
    session_id = uuid4()
    change_set_id = uuid4()
    job_id = uuid4()
    storage_key = uuid4()
    with database.cursor(write=True, actor="retention-fixture") as cursor:
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,now()+interval '1 day')",
            (session_id, "admin-one", "retention"),
        )
        cursor.execute(
            """
            INSERT INTO logo.agent_chat_message
                (id,session_id,user_login,turn_id,role,status,content,created_at)
            VALUES (%s,%s,%s,%s,'user','complete','old',now()-interval '40 days')
            """,
            (uuid4(), session_id, "admin-one", uuid4()),
        )
        cursor.execute(
            """
            INSERT INTO logo.agent_change_set
                (id,session_id,user_login,expires_at)
            VALUES (%s,%s,%s,now()-interval '1 hour')
            """,
            (change_set_id, session_id, "admin-one"),
        )
        cursor.execute(
            """
            INSERT INTO logo.agent_spreadsheet_job (
                id,session_id,user_login,storage_key,original_name,media_type,
                byte_size,sha256,format_name,status,mapping_hash,mapping,expires_at
            ) VALUES (%s,%s,%s,%s,'old.csv','text/csv',1,%s,'csv',
                      'expired',%s,'{}'::jsonb,now()-interval '1 hour')
            """,
            (job_id, session_id, "admin-one", storage_key, "a" * 64, "b" * 64),
        )
        cursor.execute(
            "INSERT INTO logo.agent_rate_window(user_login,window_start,requests) "
            "VALUES ('admin-one',now()-interval '3 days',1)"
        )
        cursor.execute(
            "INSERT INTO logo.agent_usage_daily(user_login,usage_day,requests) "
            "VALUES ('admin-one',current_date-100,1)"
        )
        cursor.execute(
            "INSERT INTO logo.agent_usage_monthly(usage_month,requests) "
            "VALUES ((date_trunc('month',current_date)-interval '25 months')::date,1)"
        )
    path = tmp_path / f"{storage_key}.upload"
    path.write_bytes(b"x")
    return change_set_id, job_id, path


def _settings(tmp_path):
    return SimpleNamespace(
        agent_upload_dir=tmp_path,
        agent_chat_retention_days=30,
    )


def test_dry_run_reports_without_mutating_database_or_files(tmp_path, monkeypatch):
    change_set_id, job_id, path = _expired_state(tmp_path)
    monkeypatch.setattr(maintenance, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(maintenance, "log_event", lambda *_args, **_kwargs: None)
    counts = maintenance.cleanup(dry_run=True)
    assert counts["spreadsheet_jobs"] == 1
    assert counts["expired_pending_change_sets"] == 1
    assert counts["old_messages"] == 1
    assert counts["old_session_titles"] == 1
    assert path.exists()
    with database.cursor() as cursor:
        cursor.execute("SELECT status FROM logo.agent_change_set WHERE id=%s", (change_set_id,))
        assert cursor.fetchone()["status"] == "pending"
        cursor.execute("SELECT 1 FROM logo.agent_spreadsheet_job WHERE id=%s", (job_id,))
        assert cursor.fetchone() is not None


def test_cleanup_discards_expired_sets_and_removes_transient_rows_files(tmp_path, monkeypatch):
    change_set_id, job_id, path = _expired_state(tmp_path)
    events = []
    monkeypatch.setattr(maintenance, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(
        maintenance,
        "log_event",
        lambda event, **fields: events.append((event, fields)),
    )
    counts = maintenance.cleanup(dry_run=False)
    assert counts["spreadsheet_jobs"] == 1
    assert counts["expired_pending_change_sets"] == 1
    assert counts["old_messages"] == 1
    assert counts["old_session_titles"] == 1
    assert counts["old_rate_windows"] == 1
    assert counts["old_daily_usage"] == 1
    assert counts["old_monthly_usage"] == 1
    assert counts["upload_file_errors"] == 0
    assert not path.exists()
    with database.cursor() as cursor:
        cursor.execute("SELECT status FROM logo.agent_change_set WHERE id=%s", (change_set_id,))
        assert cursor.fetchone()["status"] == "discarded"
        cursor.execute("SELECT 1 FROM logo.agent_spreadsheet_job WHERE id=%s", (job_id,))
        assert cursor.fetchone() is None
        cursor.execute("SELECT count(*) AS count FROM logo.agent_rate_window")
        assert cursor.fetchone()["count"] == 0
        cursor.execute(
            "SELECT count(*) AS count FROM logo.agent_chat_session "
            "WHERE title <> ''"
        )
        assert cursor.fetchone()["count"] == 0
    assert events
    assert all(event == "agent_cleanup" for event, _fields in events)


def test_cleanup_source_never_deletes_append_only_journal():
    from pathlib import Path

    source = Path(maintenance.__file__).read_text().lower()
    assert "delete from logo.agent_action_journal" not in source
