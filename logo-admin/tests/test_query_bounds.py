"""Collection reads are cardinality- and byte-bounded inside PostgreSQL."""

import queries


class ScriptedCursor:
    def __init__(self, events):
        self.events = list(events)
        self.current = None
        self.executed = []

    def execute(self, sql, params=None):
        assert self.events, f"unexpected query: {sql}"
        self.current = self.events.pop(0)
        self.executed.append((sql, params))

    def fetchone(self):
        return self.current.get("one")


def bounded(
    rows,
    *,
    row_count=None,
    max_row_bytes=1,
    result_bytes=2,
):
    """Model the single safe/sentinel row returned by ``_bounded_query``."""

    return {
        "one": {
            "rows": rows,
            "row_count": len(rows) if row_count is None else row_count,
            "max_row_bytes": max_row_bytes,
            "result_bytes": result_bytes,
        }
    }


def test_store_and_search_results_use_cap_plus_one(monkeypatch):
    monkeypatch.setattr(queries, "STORE_RESULT_LIMIT", 2)
    store_cursor = ScriptedCursor([
        bounded(
            [
                {"fdm4_store": f"S{i}", "catalog_id": f"S{i}_catalog"}
                for i in range(3)
            ]
        )
    ])
    stores = queries.list_stores(store_cursor)
    assert len(stores["stores"]) == 2
    assert stores["truncated"] is True
    assert stores["truncation"] == {"rows": True, "bytes": False}
    assert store_cursor.executed[0][1] == (3,)
    assert "WITH bounded_result AS MATERIALIZED" in store_cursor.executed[0][0]
    assert "LIMIT %s" in store_cursor.executed[0][0]

    monkeypatch.setattr(queries, "STYLE_SEARCH_RESULT_LIMIT", 2)
    style_cursor = ScriptedCursor([
        {"one": {"catalog_id": "catalog"}},
        bounded([{"product_style": f"STYLE-{i}"} for i in range(3)]),
    ])
    styles = queries.list_styles(style_cursor, store="S1")
    assert len(styles["styles"]) == 2
    assert styles["truncated"] is True
    assert style_cursor.executed[-1][1][-1] == 3

    monkeypatch.setattr(queries, "DESIGN_SEARCH_RESULT_LIMIT", 2)
    design_cursor = ScriptedCursor([
        bounded([{"design_id": f"D{i}"} for i in range(3)])
    ])
    designs = queries.search_designs(design_cursor)
    assert len(designs["designs"]) == 2
    assert designs["truncated"] is True
    assert design_cursor.executed[0][1]["limit"] == 3


def test_style_detail_caps_each_collection_and_preserves_full_style_state(monkeypatch):
    monkeypatch.setattr(queries, "STYLE_COLOR_RESULT_LIMIT", 2)
    monkeypatch.setattr(queries, "STYLE_ASSIGNMENT_RESULT_LIMIT", 3)
    cursor = ScriptedCursor([
        {"one": {"catalog_id": "catalog"}},
        {"one": {"exists": 1}},
        bounded([{"code": f"C{i}"} for i in range(3)]),
        bounded(
            [
                {"active": True, "_style_active": False, "row": i}
                for i in range(4)
            ]
        ),
        {"one": None},
    ])

    result = queries.get_style(cursor, store="S1", style="STYLE-1")

    assert len(result["colors"]) == 2
    assert len(result["assignments"]) == 3
    assert all("_style_active" not in row for row in result["assignments"])
    assert result["style_active"] is False
    assert result["truncated"] is True
    assert result["truncation"] == {
        "rows": True,
        "bytes": False,
        "colors": True,
        "assignments": True,
        "colors_bytes": False,
        "assignments_bytes": False,
    }
    assert cursor.executed[2][1][-1] == 3
    assert cursor.executed[3][1][-1] == 4


def test_vocab_caps_placements_and_backgrounds_independently(monkeypatch):
    monkeypatch.setattr(queries, "ASSIGNMENT_PLACEMENT_RESULT_LIMIT", 2)
    monkeypatch.setattr(queries, "ASSIGNMENT_BACKGROUND_RESULT_LIMIT", 1)
    cursor = ScriptedCursor([
        bounded([{"location": str(i)} for i in range(3)]),
        bounded([{"background": str(i)} for i in range(2)]),
    ])

    result = queries.get_assignment_vocab(cursor)

    assert len(result["placements"]) == 2
    assert len(result["backgrounds"]) == 1
    assert result["truncated"] is True
    assert result["truncation"] == {
        "rows": True,
        "bytes": False,
        "placements": True,
        "backgrounds": True,
        "placements_bytes": False,
        "backgrounds_bytes": False,
    }
    assert [params for _, params in cursor.executed] == [(3,), (2,)]


def test_design_caps_assets_and_placements_independently(monkeypatch):
    monkeypatch.setattr(queries, "DESIGN_ASSET_RESULT_LIMIT", 2)
    monkeypatch.setattr(queries, "DESIGN_PLACEMENT_RESULT_LIMIT", 1)
    cursor = ScriptedCursor([
        {"one": {"design_id": "D1"}},
        bounded(
            [
                {
                    "color_scheme_id": "A",
                    "resource_type": "PREVIEW",
                    "target_web_path": "",
                    "target_filename": f"asset-{i}.png",
                    "assignment_image_url": "",
                }
                for i in range(3)
            ]
        ),
        bounded([{"location": str(i)} for i in range(2)]),
    ])

    result = queries.get_design(
        cursor,
        design_id="D1",
        fdm4_art_base="https://media.example.test/",
    )

    assert len(result["schemes"]) == 1
    assert len(result["schemes"][0]["assets"]) == 2
    assert len(result["placements"]) == 1
    assert result["truncated"] is True
    assert result["truncation"] == {
        "rows": True,
        "bytes": False,
        "assets": True,
        "placements": True,
        "assets_bytes": False,
        "placements_bytes": False,
    }
    assert cursor.executed[1][1][-1] == 3
    assert cursor.executed[2][1][-1] == 2


def test_report_and_audit_pages_are_sql_bounded(monkeypatch):
    report_cursor = ScriptedCursor([
        {"one": {"total": 4}},
        bounded([{"id": 4}, {"id": 3}, {"id": 2}], row_count=3),
    ])
    report = queries.get_import_report(report_cursor, limit=2, offset=1)
    assert report["reports"] == [{"id": 4}, {"id": 3}]
    assert report["truncation"] == {"rows": True, "bytes": False}
    assert report_cursor.executed[1][1] == (3, 1)

    audit_cursor = ScriptedCursor([
        bounded([{"id": 9}, {"id": 8}, {"id": 7}], row_count=3)
    ])
    audit = queries.get_audit_log(audit_cursor, limit=2)
    assert audit["entries"] == [{"id": 9}, {"id": 8}]
    assert audit["next_before_id"] == 8
    assert audit["truncation"] == {"rows": True, "bytes": False}
    assert audit_cursor.executed[0][1] == (3,)


def test_pricing_lists_have_independent_hard_caps(monkeypatch):
    monkeypatch.setattr(queries, "PRICING_TIER_RESULT_LIMIT", 2)
    tier_cursor = ScriptedCursor([
        bounded([{"tier_name": str(i)} for i in range(3)])
    ])
    tiers = queries.list_pricing_tiers(tier_cursor)
    assert len(tiers["tiers"]) == 2
    assert tiers["truncated"] is True
    assert tiers["truncation"] == {"rows": True, "bytes": False}
    assert tier_cursor.executed[0][1] == (3,)

    monkeypatch.setattr(queries, "STORE_PRICING_TIER_RESULT_LIMIT", 1)
    assignment_cursor = ScriptedCursor([
        bounded(
            [
                {"fdm4_store": "S1", "catalog_id": "S1_catalog"},
                {"fdm4_store": "S2", "catalog_id": "S2_catalog"},
            ]
        )
    ])
    assignments = queries.list_store_pricing_tiers(assignment_cursor)
    assert len(assignments["assignments"]) == 1
    assert assignments["truncated"] is True
    assert assignments["truncation"] == {"rows": True, "bytes": False}
    assert assignment_cursor.executed[0][1] == (2,)


def test_oversized_row_sentinel_never_materializes_rows_in_python(monkeypatch):
    monkeypatch.setattr(queries, "STORE_RESULT_LIMIT", 2)
    cursor = ScriptedCursor([
        bounded(
            [],
            row_count=1,
            max_row_bytes=queries.READ_MAX_ROW_BYTES + 1,
            result_bytes=queries.READ_MAX_ROW_BYTES + 3,
        )
    ])

    result = queries.list_stores(cursor)

    assert result["stores"] == []
    assert result["truncated"] is True
    assert result["truncation"] == {"rows": False, "bytes": True}
    sql = cursor.executed[0][0]
    assert "octet_length(row_json::text)" in sql
    assert "octet_length(" in sql
    assert "ELSE '[]'::jsonb" in sql
    assert str(queries.READ_MAX_ROW_BYTES) in sql
    assert str(queries.READ_MAX_RESULT_BYTES) in sql


def test_oversized_aggregate_sentinel_is_independent_of_row_limit(monkeypatch):
    monkeypatch.setattr(queries, "STORE_RESULT_LIMIT", 100)
    cursor = ScriptedCursor([
        bounded(
            [],
            row_count=100,
            max_row_bytes=queries.READ_MAX_ROW_BYTES - 1,
            result_bytes=queries.READ_MAX_RESULT_BYTES + 1,
        )
    ])

    result = queries.list_stores(cursor)

    assert result["stores"] == []
    assert result["truncated"] is True
    assert result["truncation"] == {"rows": False, "bytes": True}
