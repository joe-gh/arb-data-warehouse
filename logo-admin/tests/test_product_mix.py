"""Product-mix override endpoints: registry, seeding, style configs, imports,
previews, and the empty-list safety guards.

Fixtures (tests/sql/seed.sql): S_TEST is un-enabled with two active styles;
S_MIXED is list-mode (MIX-1 kept, RED only, L excluded; MIX-2 removed; MIX-3 is
candidate-only drift); S_ALLMODE follows FDM4 in mode 'all'; S_EMPTY has a
catalog row but no products.
"""


def _enable(client, store, mode="list", expect=200):
    response = client.put(
        "/api/product-mix/stores", json={"fdm4_store": store, "mode": mode})
    assert response.status_code == expect, response.text
    return response.json() if expect == 200 else response


def test_enable_list_seeds_current_state(client_as):
    client = client_as()
    payload = _enable(client, "S_TEST")
    assert payload["mode"] == "list"
    assert payload["imported"] == 2
    listing = client.get("/api/product-mix", params={"store": "S_TEST"}).json()
    assert listing["mode"] == "list"
    assert listing["summary"]["in_mix"] == 2
    by_style = {row["style_code"]: row for row in listing["styles"]}
    assert set(by_style) == {"STYLE-1", "STYLE-2"}
    # Normal (non-virtual) stores seed their current color sets, not NULL.
    assert sorted(by_style["STYLE-1"]["colors"]) == ["BLU", "GRN", "RED"]
    assert by_style["STYLE-1"]["products_live"] == 3
    assert by_style["STYLE-2"]["colors"] == ["RED"]
    # Enabling twice is an explicit error, not an idempotent no-op.
    _enable(client, "S_TEST", expect=400)


def test_enable_rejects_unknown_store_and_empty_seed(client_as):
    client = client_as()
    _enable(client, "S_NOPE", expect=400)
    # S_EMPTY exists in store_catalog but has no products: list mode would
    # store an empty list and remove everything, so it must be refused...
    _enable(client, "S_EMPTY", expect=400)
    assert client.get("/api/product-mix", params={"store": "S_EMPTY"}).status_code == 404
    # ...while mode 'all' (follow FDM4) is fine.
    payload = _enable(client, "S_EMPTY", mode="all")
    assert payload["imported"] == 0
    listing = client.get("/api/product-mix", params={"store": "S_EMPTY"}).json()
    assert listing["mode"] == "all"
    assert listing["summary"]["in_mix"] is None


def test_stores_listing_reports_modes_and_counts(client_as):
    client = client_as()
    stores = {
        row["fdm4_store"]: row
        for row in client.get("/api/product-mix/stores").json()["stores"]
    }
    assert stores["S_MIXED"]["mode"] == "list"
    assert stores["S_MIXED"]["style_count"] == 1
    assert stores["S_ALLMODE"]["mode"] == "all"
    assert stores["S_ALLMODE"]["style_count"] == 0


def test_list_view_summary_and_drift(client_as):
    client = client_as()
    listing = client.get("/api/product-mix", params={"store": "S_MIXED"}).json()
    assert listing["summary"]["in_mix"] == 1
    # MIX-2 (removed) and MIX-3 (new in FDM4) are both drift.
    assert listing["summary"]["new_in_fdm4"] == 2
    assert listing["summary"]["products_live"] == 1
    row = listing["styles"][0]
    assert row["style_code"] == "MIX-1"
    assert row["colors"] == ["RED"]
    assert row["size_excludes"] == {"RED": ["L"]}


def test_style_detail_includes_removed_channels(client_as):
    client = client_as()
    detail = client.get(
        "/api/product-mix/style",
        params={"store": "S_MIXED", "style": "MIX-1"}).json()
    assert detail["in_mix"] is True
    red = next(a for a in detail["available"] if a["color"] == "RED")
    # One active variation (M); the excluded L row is inactive but its size
    # must stay visible so the exclusion remains editable.
    assert red["variations"] == 1
    assert sorted(s["code"] for s in red["sizes"]) == ["L", "M"]
    removed = client.get(
        "/api/product-mix/style",
        params={"store": "S_MIXED", "style": "MIX-2"}).json()
    assert removed["in_mix"] is False
    assert [a["color"] for a in removed["available"]] == ["BLU"]


def test_style_save_validations(client_as):
    client = client_as()
    base = {"store": "S_MIXED", "style_code": "MIX-1"}
    # Explicit empty color list is never storable.
    response = client.put("/api/product-mix/style", json={**base, "colors": []})
    assert response.status_code == 400
    assert "remove the style" in response.json()["detail"]
    assert client.put(
        "/api/product-mix/style",
        json={**base, "colors": ["PURPLE"]}).status_code == 400
    assert client.put(
        "/api/product-mix/style",
        json={**base, "colors": ["RED"], "size_excludes": {"RED": ["XXL"]}},
    ).status_code == 400
    # Excluding every known size of a channel = removing the channel; refuse.
    assert client.put(
        "/api/product-mix/style",
        json={**base, "colors": ["RED"], "size_excludes": {"RED": ["M", "L"]}},
    ).status_code == 400
    # A size exclude on a non-included channel is invalid too.
    assert client.put(
        "/api/product-mix/style",
        json={**base, "colors": ["RED"], "size_excludes": {"BLU": ["M"]}},
    ).status_code == 400
    saved = client.put(
        "/api/product-mix/style",
        json={**base, "colors": ["RED"], "size_excludes": {"RED": ["M"]}})
    assert saved.status_code == 200, saved.text
    detail = client.get(
        "/api/product-mix/style",
        params={"store": "S_MIXED", "style": "MIX-1"}).json()
    assert detail["size_excludes"] == {"RED": ["M"]}


def test_merge_import_never_resurrects_operator_removals(client_as):
    client = client_as()
    result = client.post(
        "/api/product-mix/import",
        json={"store": "S_MIXED", "mode": "merge"}).json()
    assert result["added"] == 2 and result["removed"] == 0
    listing = client.get("/api/product-mix", params={"store": "S_MIXED"}).json()
    by_style = {row["style_code"]: row for row in listing["styles"]}
    assert set(by_style) == {"MIX-1", "MIX-2", "MIX-3"}
    # The operator's channel/size choices on MIX-1 survive a merge untouched.
    assert by_style["MIX-1"]["colors"] == ["RED"]
    assert by_style["MIX-1"]["size_excludes"] == {"RED": ["L"]}


def test_reset_import_restores_fdm4_configuration(client_as):
    client = client_as()
    result = client.post(
        "/api/product-mix/import",
        json={"store": "S_MIXED", "mode": "reset"}).json()
    assert result["removed"] == 1 and result["added"] == 3
    listing = client.get("/api/product-mix", params={"store": "S_MIXED"}).json()
    by_style = {row["style_code"]: row for row in listing["styles"]}
    # Reset resurrects the operator's removals: size excludes are gone.
    assert by_style["MIX-1"]["size_excludes"] is None
    assert set(by_style) == {"MIX-1", "MIX-2", "MIX-3"}


def test_preview_counts(client_as):
    client = client_as()
    remove = client.post(
        "/api/product-mix/preview",
        json={"store": "S_MIXED", "action": "remove_styles", "styles": ["MIX-1"]},
    ).json()
    assert remove["products_retired"] == 1
    assert remove["styles_affected"] == 1
    assert remove["approximate"] is False
    widen = client.post(
        "/api/product-mix/preview",
        json={"store": "S_MIXED", "action": "set_style", "style_code": "MIX-1",
              "colors": ["RED"], "size_excludes": None}).json()
    # Dropping the L exclude restores the inactive RED/L row (approximately).
    assert widen["products_retired"] == 0
    assert widen["products_restored"] == 1
    assert widen["approximate"] is True
    disable = client.post(
        "/api/product-mix/preview",
        json={"store": "S_MIXED", "action": "disable"}).json()
    assert disable["styles_affected"] == 1
    assert disable["products_restored"] == 2
    reset = client.post(
        "/api/product-mix/preview",
        json={"store": "S_MIXED", "action": "reset"}).json()
    assert reset["styles_affected"] == 2  # MIX-2 + MIX-3 drift


def test_bulk_add_and_empty_list_guard(client_as):
    client = client_as()
    added = client.put(
        "/api/product-mix",
        json={"store": "S_MIXED", "styles": ["MIX-2", "NOPE-9"]}).json()
    assert added["saved"] == 2
    flagged = {row["style"]: row["products"] for row in added["per_style"]}
    assert flagged["NOPE-9"] == 0  # zero-match styles are flagged, not refused
    removed = client.request(
        "DELETE", "/api/product-mix",
        json={"store": "S_MIXED", "styles": ["MIX-2", "NOPE-9"]}).json()
    assert removed["removed"] == 2
    # Removing the last style would leave an active list-mode store empty -
    # the transform would then remove every product. Must be refused.
    response = client.request(
        "DELETE", "/api/product-mix",
        json={"store": "S_MIXED", "styles": ["MIX-1"]})
    assert response.status_code == 400
    assert "Disable the override" in response.json()["detail"]
    listing = client.get("/api/product-mix", params={"store": "S_MIXED"}).json()
    assert listing["summary"]["in_mix"] == 1  # the delete rolled back


def test_mode_switch_roundtrip(client_as):
    client = client_as()
    switched = client.put(
        "/api/product-mix/stores/mode",
        json={"fdm4_store": "S_ALLMODE", "mode": "list"}).json()
    # No candidates for S_ALLMODE: snapshot falls back to active state rows.
    assert switched["imported"] == 1
    listing = client.get("/api/product-mix", params={"store": "S_ALLMODE"}).json()
    assert listing["styles"][0]["style_code"] == "ALL-1"
    assert listing["styles"][0]["colors"] == ["GRN"]
    back = client.put(
        "/api/product-mix/stores/mode",
        json={"fdm4_store": "S_ALLMODE", "mode": "all"}).json()
    assert back["mode"] == "all"
    assert client.get(
        "/api/product-mix", params={"store": "S_ALLMODE"}).json()["mode"] == "all"


def test_all_mode_stores_reject_list_edits(client_as):
    client = client_as()
    assert client.put(
        "/api/product-mix/style",
        json={"store": "S_ALLMODE", "style_code": "ALL-1", "colors": ["GRN"]},
    ).status_code == 400
    assert client.put(
        "/api/product-mix",
        json={"store": "S_ALLMODE", "styles": ["ALL-1"]}).status_code == 400
    assert client.post(
        "/api/product-mix/import",
        json={"store": "S_ALLMODE", "mode": "merge"}).status_code == 400


def test_disable_and_reenable_keeps_configuration(client_as):
    client = client_as()
    assert client.delete(
        "/api/product-mix/stores", params={"store": "S_MIXED"}).json()["ok"]
    assert client.get(
        "/api/product-mix", params={"store": "S_MIXED"}).status_code == 404
    assert client.delete(
        "/api/product-mix/stores", params={"store": "S_MIXED"}).status_code == 404
    # Re-enabling merges (DO NOTHING) - the saved MIX-1 config survives.
    payload = _enable(client, "S_MIXED")
    assert payload["imported"] >= 0
    listing = client.get("/api/product-mix", params={"store": "S_MIXED"}).json()
    by_style = {row["style_code"]: row for row in listing["styles"]}
    assert by_style["MIX-1"]["colors"] == ["RED"]
    assert by_style["MIX-1"]["size_excludes"] == {"RED": ["L"]}


def test_external_enroll_and_unenroll(client_as):
    client = client_as()
    # Unknown store refused.
    assert client.put(
        "/api/product-mix/external",
        json={"fdm4_store": "S_NOPE"}).status_code == 400
    # A curated-list store must go back to 'all' before becoming external.
    assert client.put(
        "/api/product-mix/external",
        json={"fdm4_store": "S_MIXED"}).status_code == 400
    # Enroll: creates the virtual catalog + forces an active 'all' registry row.
    payload = client.put(
        "/api/product-mix/external", json={"fdm4_store": "S_TEST"}).json()
    assert payload["ok"] is True
    assert payload["catalog_id"] == "S_TEST_Woo_1"
    stores = {
        row["fdm4_store"]: row
        for row in client.get("/api/product-mix/stores").json()["stores"]
    }
    assert stores["S_TEST"]["external"] is True
    assert stores["S_TEST"]["external_catalog"] == "S_TEST_Woo_1"
    assert stores["S_TEST"]["mode"] == "all"
    # Enrolling twice is an explicit error, not an idempotent no-op.
    assert client.put(
        "/api/product-mix/external",
        json={"fdm4_store": "S_TEST"}).status_code == 400
    # Unenroll drops the supply row but keeps the registry entry.
    assert client.delete(
        "/api/product-mix/external", params={"store": "S_TEST"}).json()["ok"]
    stores = {
        row["fdm4_store"]: row
        for row in client.get("/api/product-mix/stores").json()["stores"]
    }
    assert stores["S_TEST"]["external"] is False
    assert stores["S_TEST"]["mode"] == "all"
    assert client.delete(
        "/api/product-mix/external",
        params={"store": "S_TEST"}).status_code == 404
