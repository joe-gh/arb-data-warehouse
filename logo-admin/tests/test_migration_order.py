"""sql/migrations/APPLY_ORDER is the replayable order, and it is complete.

Lexical order cannot rebuild the schema: '-' sorts before 's', so
2026-07-31-price-rule-hardening.sql would ALTER woo.price_rule before
2026-07-31-price-rules.sql creates it, and 2026-08-03-assignment-catalog-scope
would add logo.assignment.catalog_id before 2026-08-03-logo-feed-versioning
adds row_version - the reverse of the physical order database_contract.py
pins. These are file-only checks; they need no database.
"""

from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[2] / "sql" / "migrations"
MANIFEST = MIGRATIONS / "APPLY_ORDER"


def _manifest_entries():
    return [
        line.strip()
        for line in MANIFEST.read_text().splitlines()
        if line.strip()
    ]


def test_manifest_is_a_permutation_of_the_migration_directory():
    entries = _manifest_entries()
    assert len(entries) == len(set(entries)), "APPLY_ORDER lists a file twice"
    on_disk = sorted(
        path.name for path in MIGRATIONS.iterdir()
        if path.is_file() and path.suffix == ".sql"
    )
    assert sorted(entries) == on_disk


def test_price_rule_table_is_created_before_it_is_altered():
    entries = _manifest_entries()
    assert entries.index("2026-07-31-price-rules.sql") < entries.index(
        "2026-07-31-price-rule-hardening.sql")


def test_assignment_row_version_lands_before_catalog_id():
    entries = _manifest_entries()
    assert entries.index("2026-08-03-logo-feed-versioning.sql") < entries.index(
        "2026-08-03-assignment-catalog-scope.sql")
