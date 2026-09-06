"""The media publisher must never hand one source URL another source's bytes,
and must not report success when the mapping rows never landed."""

import sys
import types

import pytest

from tests.infra.fakes import FakePsql, FakeS3, install_fake_boto3, load_script

publish = load_script("publish-product-media.py", "infra_publish_product_media")


def run_publisher(monkeypatch, s3, *, urls, published=(), canonical=(), bodies=None,
                  fail_flush=False, spill_file=None):
    """Drive main() with the bucket, the database and the fetches all faked."""
    bodies = bodies or {}
    fetched = []
    psql = FakePsql(
        {
            "WITH urls AS": "".join(f"{url}\t{sku}\n" for url, sku in urls),
            "SELECT source_url FROM pim.media_object": "".join(f"{url}\n" for url in published),
            "SELECT DISTINCT s3_key FROM pim.media_object": "".join(f"{key}\n" for key in canonical),
        },
        fail_flush=fail_flush,
    )

    def fake_fetch(url):
        fetched.append(url)
        return bodies[url], "image/jpeg"

    monkeypatch.setattr(publish, "subprocess", types.SimpleNamespace(run=psql))
    monkeypatch.setattr(publish, "fetch", fake_fetch)
    if spill_file is not None:
        monkeypatch.setattr(publish, "FLUSH_FAIL_FILE", str(spill_file))
    install_fake_boto3(monkeypatch, s3)
    monkeypatch.setattr(sys, "argv", ["publish-product-media.py", "--workers", "1"])
    code = publish.main()
    return code, psql, fetched


def mapping(psql):
    """source_url -> s3_key from everything the run flushed."""
    return {row[0]: row[1] for row in psql.flushed_rows()}


def test_same_basename_from_two_hosts_gets_two_objects(monkeypatch):
    first = "https://a.example/media/front.jpg"
    second = "https://b.example/other/front.jpg"
    s3 = FakeS3()

    code, psql, _ = run_publisher(
        monkeypatch, s3,
        urls=[(first, "S1")],
        bodies={first: b"FIRST-BYTES"},
    )
    assert code == 0
    first_key = mapping(psql)[first]
    assert s3.objects[first_key] == b"FIRST-BYTES"

    # Second run: the first URL is already mapped, the second one shares its
    # basename and sku. It must not adopt the object that is already there.
    code, psql, fetched = run_publisher(
        monkeypatch, s3,
        urls=[(first, "S1"), (second, "S1")],
        published=[first],
        canonical=[first_key],
        bodies={second: b"SECOND-BYTES"},
    )
    assert code == 0
    assert fetched == [second]
    second_key = mapping(psql)[second]
    assert second_key != first_key
    assert s3.objects[first_key] == b"FIRST-BYTES"
    assert s3.objects[second_key] == b"SECOND-BYTES"


def test_a_rendition_in_the_listing_is_not_adopted_as_a_source_image(monkeypatch):
    # A WordPress rendition sits beside the canonical images under the same
    # products/<sku>/ prefix, so it shows up in the bucket listing.
    rendition_key = "products/X/shirt-300x300.jpg"
    s3 = FakeS3({rendition_key: b"RENDITION-BYTES"})
    url = "https://cdn.example/photos/shirt-300x300.jpg"

    code, psql, fetched = run_publisher(
        monkeypatch, s3,
        urls=[(url, "X")],
        canonical=[],  # media_object knows nothing about that key
        bodies={url: b"SOURCE-BYTES"},
    )
    assert code == 0
    assert fetched == [url]
    key = mapping(psql)[url]
    assert key != rendition_key
    assert s3.objects[rendition_key] == b"RENDITION-BYTES"
    assert s3.objects[key] == b"SOURCE-BYTES"


def test_adoption_needs_the_mapping_table_not_just_the_listing(monkeypatch):
    url = "https://cdn.example/photos/boot.jpg"
    key = publish.object_key(url, "X")

    # The object is in the bucket but nothing in media_object calls it
    # canonical: re-fetch rather than claim bytes of unknown provenance.
    s3 = FakeS3({key: b"UNKNOWN-BYTES"})
    code, psql, fetched = run_publisher(
        monkeypatch, s3, urls=[(url, "X")], canonical=[], bodies={url: b"FRESH-BYTES"})
    assert code == 0
    assert fetched == [url]
    assert s3.objects[key] == b"FRESH-BYTES"

    # Same object, now recorded as canonical: adopt it without a fetch.
    s3 = FakeS3({key: b"UNKNOWN-BYTES"})
    code, psql, fetched = run_publisher(
        monkeypatch, s3, urls=[(url, "X")], canonical=[key], bodies={})
    assert code == 0
    assert fetched == []
    assert mapping(psql)[url] == key


def test_object_key_is_stable_and_source_specific():
    a = "https://a.example/media/front.jpg"
    b = "https://b.example/other/front.jpg"
    assert publish.object_key(a, "S1") == publish.object_key(a, "S1")
    assert publish.object_key(a, "S1") != publish.object_key(b, "S1")
    assert publish.object_key(a, "S1").startswith("products/S1/")
    assert publish.object_key(a, "S1").endswith(".jpg")
    assert publish.object_key(a, "") .startswith("products/UNKNOWN/")
    # Basenames stay inside the bucket-key budget even for absurd URLs.
    long_url = "https://a.example/" + ("n" * 500) + ".jpg"
    assert len(publish.object_key(long_url, "S1").rsplit("/", 1)[-1]) <= 120
    # No extension is still a usable name.
    assert publish.object_key("https://a.example/image", "S1").rsplit("/", 1)[-1].count("-") >= 1


def test_a_failed_mapping_flush_makes_the_run_incomplete(monkeypatch, tmp_path, capsys):
    url = "https://cdn.example/photos/jacket.jpg"
    s3 = FakeS3()
    spill = tmp_path / "media-publish-failed-rows.tsv"

    code, psql, fetched = run_publisher(
        monkeypatch, s3,
        urls=[(url, "S9")],
        bodies={url: b"JACKET"},
        fail_flush=True,
        spill_file=spill,
    )

    # The upload itself worked, so the objects exist...
    assert fetched == [url]
    assert publish.object_key(url, "S9") in s3.objects
    # ...but nothing maps to them, so the run is not a success.
    assert code == 2
    assert "INCOMPLETE" in capsys.readouterr().out
    assert url in spill.read_text()


def test_flush_rows_reports_success_and_failure(monkeypatch, tmp_path):
    ok = FakePsql()
    monkeypatch.setattr(publish, "subprocess", types.SimpleNamespace(run=ok))
    assert publish.flush_rows([]) is True
    assert publish.flush_rows(["a\tb\tc\td\te\t1\tf"]) is True

    bad = FakePsql(fail_flush=True)
    monkeypatch.setattr(publish, "subprocess", types.SimpleNamespace(run=bad))
    monkeypatch.setattr(publish, "FLUSH_FAIL_FILE", str(tmp_path / "spill.tsv"))
    assert publish.flush_rows(["a\tb\tc\td\te\t1\tf"]) is False
    assert (tmp_path / "spill.tsv").read_text().startswith("a\tb\tc")


def test_the_dead_lock_pad_is_gone():
    source = (publish.__file__ and open(publish.__file__).read())
    assert "lock_pad" not in source
    assert "used_keys_lock" in source


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
