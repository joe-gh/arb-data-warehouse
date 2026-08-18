"""Retention cleanup for agent chat, quotas, and private spreadsheet files."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import uuid

from agent_logging import log_event
from config import get_settings
from db import database
import quotas


def _count(cursor, query: str, params: tuple = ()) -> int:
    cursor.execute(query, params)
    row = cursor.fetchone()
    return int(row["count"] if isinstance(row, dict) else row[0])


def cleanup(*, dry_run: bool = False) -> dict[str, int]:
    """Remove expired transient data while preserving append-only journals."""

    settings = get_settings()
    counts: dict[str, int] = {}
    if not dry_run:
        recovered = quotas.sweep_stale_reservations()
        counts["stale_quota_released"] = recovered["released"]
        counts["stale_quota_retained"] = recovered["retained"]
    with database.cursor(
        write=not dry_run,
        actor="agent-maintenance",
        commit_on_success=not dry_run,
    ) as cursor:
        cursor.execute(
            """
            SELECT storage_key FROM logo.agent_spreadsheet_job
             WHERE expires_at < now()
            """
        )
        storage_keys = [str(row["storage_key"]) for row in cursor.fetchall()]
        counts["spreadsheet_jobs"] = len(storage_keys)

        counts["expired_pending_change_sets"] = _count(
            cursor,
            """
            SELECT count(*) FROM logo.agent_change_set
             WHERE status = 'pending' AND expires_at < now()
            """,
        )
        counts["old_messages"] = _count(
            cursor,
            """
            SELECT count(*) FROM logo.agent_chat_message
             WHERE created_at < now() - (%s * interval '1 day')
            """,
            (settings.agent_chat_retention_days,),
        )
        counts["old_session_titles"] = _count(
            cursor,
            """
            SELECT count(*) FROM logo.agent_chat_session AS session
             WHERE session.title <> ''
               AND NOT EXISTS (
                   SELECT 1 FROM logo.agent_chat_message AS message
                    WHERE message.session_id = session.id
                      AND message.user_login = session.user_login
                      AND message.created_at >= now() - (%s * interval '1 day')
               )
            """,
            (settings.agent_chat_retention_days,),
        )
        counts["old_discarded_change_sets"] = _count(
            cursor,
            """
            SELECT count(*) FROM logo.agent_change_set
             WHERE status = 'discarded'
               AND updated_at < now() - interval '30 days'
            """,
        )
        counts["old_applied_change_sets"] = _count(
            cursor,
            """
            SELECT count(*) FROM logo.agent_change_set AS cs
             WHERE cs.status IN ('applied', 'undone')
               AND cs.updated_at < now() - interval '400 days'
               AND EXISTS (
                   SELECT 1 FROM logo.agent_action_journal AS journal
                    WHERE journal.change_set_id = cs.id
                      AND journal.user_login = cs.user_login
                      AND journal.event_type = 'apply'
               )
               AND (
                   cs.status = 'applied'
                   OR EXISTS (
                       SELECT 1 FROM logo.agent_action_journal AS journal
                        WHERE journal.change_set_id = cs.id
                          AND journal.user_login = cs.user_login
                          AND journal.event_type = 'undo'
                   )
               )
               AND NOT EXISTS (
                   SELECT 1 FROM logo.agent_action_journal AS journal
                    WHERE journal.change_set_id = cs.id
                      AND journal.user_login = cs.user_login
                      AND journal.created_at >= now() - interval '400 days'
               )
            """,
        )
        counts["empty_sessions"] = _count(
            cursor,
            """
            SELECT count(*) FROM logo.agent_chat_session AS session
             WHERE session.updated_at < now() - (%s * interval '1 day')
               AND NOT EXISTS (
                   SELECT 1 FROM logo.agent_chat_message AS message
                    WHERE message.session_id = session.id
                      AND message.user_login = session.user_login
                      AND message.created_at >= now() - (%s * interval '1 day')
               )
               AND NOT EXISTS (
                   SELECT 1 FROM logo.agent_change_set AS change_set
                    WHERE change_set.session_id = session.id
                      AND change_set.user_login = session.user_login
               )
               AND NOT EXISTS (
                   SELECT 1 FROM logo.agent_spreadsheet_job AS job
                    WHERE job.session_id = session.id
                      AND job.user_login = session.user_login
                      AND job.expires_at >= now()
               )
            """,
            (
                settings.agent_chat_retention_days,
                settings.agent_chat_retention_days,
            ),
        )
        counts["old_rate_windows"] = _count(
            cursor,
            """
            SELECT count(*) FROM logo.agent_rate_window
             WHERE window_start < now() - interval '2 days'
            """,
        )
        counts["old_quota_reservations"] = _count(
            cursor,
            "SELECT count(*) FROM logo.agent_quota_reservation "
            "WHERE status <> 'reserved' "
            "AND created_at < now() - interval '90 days'",
        )
        counts["old_daily_usage"] = _count(
            cursor,
            "SELECT count(*) FROM logo.agent_usage_daily WHERE usage_day < current_date - 90",
        )
        counts["old_monthly_usage"] = _count(
            cursor,
            "SELECT count(*) FROM logo.agent_usage_monthly "
            "WHERE usage_month < (date_trunc('month', current_date) - interval '24 months')::date",
        )

        if not dry_run:
            cursor.execute(
                """
                UPDATE logo.agent_change_set
                   SET status = 'discarded', updated_at = now()
                 WHERE status = 'pending' AND expires_at < now()
                """
            )
            cursor.execute(
                """
                DELETE FROM logo.agent_spreadsheet_job
                 WHERE expires_at < now()
                """
            )
            cursor.execute(
                """
                DELETE FROM logo.agent_chat_message
                 WHERE created_at < now() - (%s * interval '1 day')
                """,
                (settings.agent_chat_retention_days,),
            )
            cursor.execute(
                """
                UPDATE logo.agent_chat_session AS session
                   SET title = ''
                 WHERE session.title <> ''
                   AND NOT EXISTS (
                       SELECT 1 FROM logo.agent_chat_message AS message
                        WHERE message.session_id = session.id
                          AND message.user_login = session.user_login
                          AND message.created_at >= now() - (%s * interval '1 day')
                   )
                """,
                (settings.agent_chat_retention_days,),
            )
            cursor.execute(
                """
                DELETE FROM logo.agent_change_set
                 WHERE status = 'discarded'
                   AND updated_at < now() - interval '30 days'
                """
            )
            cursor.execute(
                """
                SELECT journals_deleted, change_sets_deleted
                  FROM logo.prune_agent_history()
                """
            )
            pruned = cursor.fetchone()
            if pruned is not None:
                counts["old_applied_journals"] = int(
                    pruned["journals_deleted"]
                )
                counts["old_applied_change_sets"] = int(
                    pruned["change_sets_deleted"]
                )
            cursor.execute(
                "DELETE FROM logo.agent_rate_window "
                "WHERE window_start < now() - interval '2 days'"
            )
            cursor.execute(
                "DELETE FROM logo.agent_quota_reservation "
                "WHERE status <> 'reserved' "
                "AND created_at < now() - interval '90 days'"
            )
            cursor.execute(
                "DELETE FROM logo.agent_usage_daily WHERE usage_day < current_date - 90"
            )
            cursor.execute(
                "DELETE FROM logo.agent_usage_monthly "
                "WHERE usage_month < "
                "(date_trunc('month', current_date) - interval '24 months')::date"
            )

            cursor.execute(
                """
                DELETE FROM logo.agent_chat_session AS session
                 WHERE session.updated_at < now() - (%s * interval '1 day')
                   AND NOT EXISTS (
                       SELECT 1 FROM logo.agent_chat_message AS message
                        WHERE message.session_id = session.id
                          AND message.user_login = session.user_login
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM logo.agent_change_set AS change_set
                        WHERE change_set.session_id = session.id
                          AND change_set.user_login = session.user_login
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM logo.agent_spreadsheet_job AS job
                        WHERE job.session_id = session.id
                          AND job.user_login = session.user_login
                   )
                """,
                (settings.agent_chat_retention_days,),
            )

        cursor.execute("SELECT storage_key FROM logo.agent_spreadsheet_job")
        active_storage_keys = {
            str(row["storage_key"]) for row in cursor.fetchall()
        }

    base = Path(settings.agent_upload_dir)
    removed_files = 0
    file_errors = 0
    if not dry_run:
        for storage_key in storage_keys:
            try:
                safe_key = uuid.UUID(storage_key)
                (base / f"{safe_key}.upload").unlink(missing_ok=True)
                removed_files += 1
            except (ValueError, OSError):
                file_errors += 1

    # Independently sweep UUID-named orphan files. This makes cleanup
    # crash-recoverable if metadata committed before a prior unlink. Files
    # younger than the job TTL or still referenced by a job are never touched.
    orphan_candidates = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    if base.is_dir():
        for path in base.glob("*.upload"):
            try:
                safe_key = str(uuid.UUID(path.stem))
                modified = datetime.fromtimestamp(
                    path.stat().st_mtime,
                    timezone.utc,
                )
            except (ValueError, OSError):
                continue
            if safe_key in active_storage_keys or modified >= cutoff:
                continue
            orphan_candidates += 1
            if not dry_run:
                try:
                    path.unlink(missing_ok=True)
                    removed_files += 1
                except OSError:
                    file_errors += 1

    counts["orphan_upload_files"] = orphan_candidates
    counts["removed_upload_files"] = removed_files
    counts["upload_file_errors"] = file_errors

    for name, count in counts.items():
        log_event("agent_cleanup", status="dry_run" if dry_run else "complete", count=count)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.command == "cleanup":
        cleanup(dry_run=arguments.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
