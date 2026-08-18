"""Allowlisted full-row business snapshots for atomicity assertions."""

from psycopg2 import sql


BUSINESS_TABLES = (
    "logo.assignment",
    "logo.store_settings",
    "woo.store_pricing_tier",
)


def snapshot_table(cursor, qualified_name: str) -> list[dict]:
    if qualified_name not in BUSINESS_TABLES:
        raise ValueError("snapshot table is not allowlisted")
    schema, table = qualified_name.split(".", 1)
    cursor.execute(
        sql.SQL(
            "SELECT to_jsonb(t) AS row "
            "FROM {}.{} AS t ORDER BY to_jsonb(t)::text"
        ).format(sql.Identifier(schema), sql.Identifier(table))
    )
    return [dict(record["row"]) for record in cursor.fetchall()]


def snapshot_business_state(cursor) -> dict[str, list[dict]]:
    return {
        table: snapshot_table(cursor, table)
        for table in BUSINESS_TABLES
    }
