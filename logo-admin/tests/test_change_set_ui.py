from pathlib import Path


APP = Path(__file__).resolve().parents[1]
JAVASCRIPT = (APP / "static" / "app.js").read_text()
TEMPLATE = (APP / "templates" / "dashboard.html").read_text()


def _assistant_javascript() -> str:
    start = JAVASCRIPT.index("// ===== Allowlisted in-app assistant =====")
    end = JAVASCRIPT.index("  function wireEvents()", start)
    return JAVASCRIPT[start:end]


def test_review_has_required_human_actions_and_no_redo():
    source = _assistant_javascript()
    assert "Confirm changes" in source
    assert "Discard" in source
    assert "Undo applied changes" in source
    assert "/apply" in source
    assert "/discard" in source
    assert "/undo" in source
    assert "redo" not in source.lower()
    assert "redo" not in TEMPLATE.lower()
    assert "assistantState.writesEnabled" in source
    assert "Agent writes are disabled in this environment." in source


def test_apply_submits_the_revision_and_hash_that_were_rendered():
    source = _assistant_javascript()
    assert 'appendReviewField(metadata, "Revision", changeSet.revision)' in source
    assert 'appendReviewField(metadata, "Preview hash", changeSet.previewHash' in source
    assert "revision: displayedChangeSet.revision" in source
    assert "preview_hash: displayedChangeSet.previewHash" in source


def test_hard_delete_needs_explicit_extra_acknowledgement():
    source = _assistant_javascript()
    phrase = (
        "I understand this permanently deletes assignment data "
        "and may remove companion rows."
    )
    assert phrase in source
    assert "confirm.disabled = Boolean(acknowledgement)" in source
    assert "acknowledge_hard_delete: acknowledged" in source


def test_stale_preview_and_undo_conflict_are_explained_without_overwrite():
    source = _assistant_javascript()
    assert "error.status === 409" in source
    assert "Review the refreshed values before confirming again." in source
    assert "Nothing was overwritten." in source


def test_spreadsheet_mapping_is_a_separate_confirmation():
    source = _assistant_javascript()
    assert "/api/agent/spreadsheets" in source
    assert "/confirm-mapping" in source
    assert "mapping_revision: displayedJob.revision" in source
    assert "mapping_hash: displayedJob.mappingHash" in source
    assert "Confirm mapping and stage rows" in source
    assert "Review the change set before confirming" in source


def test_public_state_ids_are_validated_before_becoming_route_segments():
    source = _assistant_javascript()
    assert "UUID_PATTERN" in source
    assert "HASH_PATTERN" in source
    assert "encodeURIComponent(displayedChangeSet.id)" in source
    assert "encodeURIComponent(displayedJob.id)" in source
