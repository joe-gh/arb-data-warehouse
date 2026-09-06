"""A completed full PIM pull is the deletion reconciliation.

Needs a real database because the behaviour lives in SQL against the columns
sql/migrations/2026-09-06-pim-api-retire.sql adds. Uses the admin DSN directly
rather than the app fixtures: this is the puller's own connection, not the web
app's.
"""

import json
import os

import psycopg2
import pytest

from tests.infra.fakes import load_script

pull = load_script("pull_pim.py", "infra_pull_pim")

PREFIX = "TESTRET-"
FIELDS = ["prod_ref", "prod_stylenumber", "prod_modify"]


def scripted_api(pages, raise_on_page=None):
    """Stand in for api_get: hand back the prepared pages in order."""
    state = {"n": 0}

    def api_get(path, key):
        index = state["n"]
        state["n"] += 1
        if raise_on_page is not None and index == raise_on_page:
            raise RuntimeError("api2 returned garbage")
        return pages[index]

    return api_get


def paged(refs, per_page=2):
    """Refs split into @nextLink-chained pages, the way api2 answers."""
    chunks = [refs[i:i + per_page] for i in range(0, len(refs), per_page)] or [[]]
    pages = []
    for position, chunk in enumerate(chunks):
        page = {"value": [{"prod_ref": ref, "prod_stylenumber": ref, "prod_modify": None}
                          for ref in chunk]}
        if position < len(chunks) - 1:
            page["@nextLink"] = pull.API_BASE + f"/catalog/products?page={position + 2}"
        pages.append(page)
    return pages


@pytest.fixture
def connection():
    dsn = os.environ.get("TEST_DATABASE_ADMIN_DSN", "").strip()
    if not dsn:
        pytest.skip("TEST_DATABASE_ADMIN_DSN is not set")
    handle = psycopg2.connect(dsn)
    with handle.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_schema = 'pim' AND table_name = 'api_product'"
            "   AND column_name = 'retired_at'"
        )
        if cursor.fetchone() is None:
            handle.close()
            pytest.skip("apply sql/migrations/2026-09-06-pim-api-retire.sql first")
    handle.commit()
    try:
        yield handle
    finally:
        handle.rollback()
        with handle.cursor() as cursor:
            cursor.execute("DELETE FROM pim.api_product WHERE prod_ref LIKE %s", (PREFIX + "%",))
            cursor.execute("DELETE FROM pim.api_pull_state WHERE entity = 'products'")
        handle.commit()
        handle.close()


def seed(connection, refs):
    with connection.cursor() as cursor:
        for ref in refs:
            cursor.execute(
                "INSERT INTO pim.api_product (prod_ref, style_number, payload, retired_at)"
                " VALUES (%s, %s, %s, NULL)"
                " ON CONFLICT (prod_ref) DO UPDATE SET retired_at = NULL",
                (ref, ref, json.dumps({"prod_ref": ref})),
            )
    connection.commit()


def retired(connection, refs):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT prod_ref FROM pim.api_product"
            " WHERE prod_ref = ANY(%s) AND retired_at IS NOT NULL",
            (list(refs),),
        )
        return {row[0] for row in cursor.fetchall()}


def refs(count):
    return [f"{PREFIX}{index:02d}" for index in range(1, count + 1)]


def test_a_completed_full_pull_retires_what_it_did_not_see(connection, monkeypatch):
    everything = refs(5)
    seed(connection, everything)
    monkeypatch.setattr(pull, "api_get", scripted_api(paged(everything[:-1])))

    count = pull.pull_products(connection, "key", FIELDS, full=True)

    assert count == 4
    assert retired(connection, everything) == {everything[-1]}


def test_a_pull_that_failed_part_way_retires_nothing(connection, monkeypatch):
    everything = refs(5)
    seed(connection, everything)
    # Page one lands, page two blows up: the walk never completes, so the
    # missing refs are unknown and nothing may be retired.
    monkeypatch.setattr(pull, "api_get", scripted_api(paged(everything), raise_on_page=1))

    with pytest.raises(RuntimeError):
        pull.pull_products(connection, "key", FIELDS, full=True)
    connection.rollback()

    assert retired(connection, everything) == set()


def test_a_record_that_comes_back_is_live_again(connection, monkeypatch):
    everything = refs(5)
    seed(connection, everything)
    monkeypatch.setattr(pull, "api_get", scripted_api(paged(everything[:-1])))
    pull.pull_products(connection, "key", FIELDS, full=True)
    assert retired(connection, everything) == {everything[-1]}

    monkeypatch.setattr(pull, "api_get", scripted_api(paged(everything)))
    pull.pull_products(connection, "key", FIELDS, full=True)

    assert retired(connection, everything) == set()


def test_retirement_refuses_to_empty_the_mirror(connection, monkeypatch, capsys):
    everything = refs(5)
    seed(connection, everything)
    # Three of five gone in one run is far past the floor: treat it as a bad
    # API answer, keep every row and say so.
    monkeypatch.setattr(pull, "api_get", scripted_api(paged(everything[:2])))

    pull.pull_products(connection, "key", FIELDS, full=True)

    assert retired(connection, everything) == set()
    assert "retirement skipped" in capsys.readouterr().out


def test_an_incremental_pull_never_retires(connection, monkeypatch):
    everything = refs(5)
    seed(connection, everything)
    monkeypatch.setattr(pull, "api_get", scripted_api(paged(everything[:1])))

    pull.pull_products(connection, "key", FIELDS, full=False)

    assert retired(connection, everything) == set()


def test_seeing_a_record_again_clears_its_retirement(connection, monkeypatch):
    everything = refs(5)
    seed(connection, everything)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE pim.api_product SET retired_at = now() WHERE prod_ref = %s",
            (everything[0],),
        )
    connection.commit()

    monkeypatch.setattr(pull, "api_get", scripted_api(paged(everything[:1])))
    pull.pull_products(connection, "key", FIELDS, full=False)

    assert retired(connection, everything) == set()
