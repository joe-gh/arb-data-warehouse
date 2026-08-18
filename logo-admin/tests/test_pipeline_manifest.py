"""The hourly loader accepts only one complete, canonical pull generation."""

import csv
import json

import pytest

import importlib.util
from pathlib import Path

# Load by file path: the repo root's `infra/` namespace package is shadowed by
# the regular `logo-admin/infra/` package under pytest's rootdir, so a plain
# `from infra import load_dump` can never resolve to the pipeline module.
pytest.importorskip("psycopg2")  # load_dump imports it at module scope
_LOAD_DUMP_PATH = Path(__file__).resolve().parents[2] / "infra" / "load_dump.py"
_spec = importlib.util.spec_from_file_location("pipeline_load_dump", _LOAD_DUMP_PATH)
load_dump = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load_dump)


def _write_run(tmp_path, *, publishable=True, omitted=None):
    run_dir = tmp_path / "run-20260801T120000.000000Z-1"
    run_dir.mkdir()
    tables = sorted(load_dump.REQUIRED_TABLES - ({omitted} if omitted else set()))
    entries = {}
    for table in tables:
        filename = f"{table}.csv"
        with (run_dir / filename).open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["fixture_column"])
        entries[table] = {
            "status": "ok",
            "rows": 0,
            "columns": 1,
            "filename": filename,
        }
    manifest = {
        "run_id": "20260801T120000.000000Z",
        "complete": True,
        "publishable": publishable,
        "required_tables": tables,
        "tables": entries,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    return run_dir


def test_loader_accepts_exact_canonical_manifest(tmp_path):
    run_dir = _write_run(tmp_path)
    rows = load_dump.validated_manifest(str(run_dir))
    assert {table for table, _path, _count in rows} == load_dump.REQUIRED_TABLES


def test_loader_rejects_partial_or_nonpublishable_manifest(tmp_path):
    partial = _write_run(tmp_path, omitted="item")
    with pytest.raises(RuntimeError, match="canonical production inventory"):
        load_dump.validated_manifest(str(partial))

    other_root = tmp_path / "other"
    other_root.mkdir()
    diagnostic = _write_run(other_root, publishable=False)
    with pytest.raises(RuntimeError, match="not complete"):
        load_dump.validated_manifest(str(diagnostic))


def test_current_pointer_must_match_manifest_run_id(tmp_path):
    run_dir = _write_run(tmp_path)
    (tmp_path / "current.json").write_text(json.dumps({
        "run_dir": run_dir.name,
        "run_id": "different-generation",
    }))
    resolved, pointer_id = load_dump.resolve_run_dir(str(tmp_path))
    with pytest.raises(RuntimeError, match="run ids differ"):
        load_dump.validated_manifest(resolved, pointer_id)
