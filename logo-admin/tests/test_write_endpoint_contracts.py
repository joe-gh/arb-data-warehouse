"""Black-box compatibility checks for existing transactional HTTP writes."""

from db import database


def _assignment():
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM logo.assignment "
            "ORDER BY fdm4_store, product_style, garment_color_code, option_row, position LIMIT 1"
        )
        row = cursor.fetchone()
    assert row is not None
    return dict(row)


def _assignment_body(row, **changes):
    body = {
        key: row[key]
        for key in (
            "fdm4_store", "product_style", "garment_color_code", "position",
            "option_row", "design_id", "logo_code", "color_scheme_id",
            "location", "optional", "background", "sort_order", "image_url",
            "active",
        )
    }
    body["cost_override"] = (
        str(row["cost_override"]) if row["cost_override"] is not None else None
    )
    body.update(changes)
    return body


def test_save_assignment_preserves_response_and_complete_state(client_as):
    row = _assignment()
    client = client_as("admin-one")
    response = client.put(
        "/api/assignments",
        json=_assignment_body(
            row,
            location="CENTER CHEST",
            image_url="https://example.test/updated.png",
        ),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["assignment"]["location"] == "CENTER CHEST"
    assert payload["assignment"]["updated_by"] == "admin-one"


def test_soft_and_hard_assignment_routes_remain_distinct(client_as):
    row = _assignment()
    params = {
        "fdm4_store": row["fdm4_store"],
        "product_style": row["product_style"],
        "garment_color_code": row["garment_color_code"],
        "position": row["position"],
        "option_row": row["option_row"],
    }
    client = client_as("admin-one")
    soft = client.delete("/api/assignments", params={**params, "hard": "false"})
    assert soft.status_code == 200
    assert soft.json() == {"ok": True, "hard": False}
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT active FROM logo.assignment
             WHERE fdm4_store=%s AND product_style=%s
               AND garment_color_code=%s AND option_row=%s AND position=%s
            """,
            (
                row["fdm4_store"], row["product_style"],
                row["garment_color_code"], row["option_row"], row["position"],
            ),
        )
        assert cursor.fetchone()["active"] is False


def test_store_settings_and_pricing_routes_preserve_contracts(client_as):
    row = _assignment()
    store = row["fdm4_store"]
    client = client_as("admin-one")
    settings = client.put(
        f"/api/settings/{store}",
        json={"enabled": False, "allows_none": True},
    )
    assert settings.status_code == 200
    assert settings.json()["settings"]["updated_by"] == "admin-one"

    with database.cursor() as cursor:
        cursor.execute(
            "SELECT tier_name FROM woo.pricing_tier ORDER BY sort_order LIMIT 1"
        )
        tier = cursor.fetchone()["tier_name"]
    saved = client.put(
        "/api/pricing/store-tier",
        json={"fdm4_store": store, "tier_name": tier, "note": "contract"},
    )
    assert saved.status_code == 200
    assert saved.json()["assignment"] == {
        "fdm4_store": store,
        "tier_name": tier,
        "note": "contract",
    }
    deleted = client.delete(
        "/api/pricing/store-tier",
        params={"fdm4_store": store},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}


def test_style_color_copy_routes_keep_existing_response_shapes(client_as):
    row = _assignment()
    client = client_as("admin-one")
    color = client.delete(
        "/api/assignments-by-color",
        params={
            "fdm4_store": row["fdm4_store"],
            "product_style": row["product_style"],
            "garment_color_code": row["garment_color_code"],
            "hard": "false",
        },
    )
    assert color.status_code == 200
    assert color.json()["hard"] is False
    assert color.json()["removed"] >= 1

    style = client.post(
        "/api/style-active",
        json={
            "store": row["fdm4_store"],
            "style": row["product_style"],
            "active": False,
        },
    )
    assert style.status_code == 200
    assert style.json()["active"] is False
    assert style.json()["updated"] >= 1
