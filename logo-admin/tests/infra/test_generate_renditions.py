"""Renditions are written beside their source, so a rendition filename can
collide with a different canonical image. Nothing in pim.media_object may ever
be a rendition destination."""

import pytest

from tests.infra.fakes import FakeS3, install_fake_pil, load_script

renditions = load_script("generate-renditions.py", "infra_generate_renditions")

CANONICAL = "products/X/shirt.jpg"
OTHER_CANONICAL = "products/X/shirt-300x300.jpg"


def rendition(rendition_file, width=300, height=300):
    return renditions.Rendition(
        canonical_key=CANONICAL,
        rendition_file=rendition_file,
        width=width,
        height=height,
        size_name="thumbnail",
        content_type="image/jpeg",
        crop=False,
        crop_x="center",
        crop_y="center",
    )


@pytest.fixture
def s3():
    return FakeS3({CANONICAL: b"CANONICAL-BYTES", OTHER_CANONICAL: b"OTHER-CANONICAL-BYTES"})


@pytest.fixture
def stub_render(monkeypatch):
    install_fake_pil(monkeypatch)
    monkeypatch.setattr(renditions, "render", lambda base, row, jq, wq: (b"RENDERED", "image/jpeg"))


def test_a_destination_that_is_another_canonical_image_is_refused(s3, stub_render):
    protected = frozenset({CANONICAL, OTHER_CANONICAL})

    successes, errors = renditions.process_source(
        s3, CANONICAL, [rendition("shirt-300x300.jpg")], 82, 86, protected)

    assert successes == []
    assert errors == [f"{OTHER_CANONICAL}\trefused group containing a canonical overwrite"]
    assert s3.puts == []
    assert s3.objects[OTHER_CANONICAL] == b"OTHER-CANONICAL-BYTES"


def test_a_free_destination_still_generates(s3, stub_render):
    protected = frozenset({CANONICAL, OTHER_CANONICAL})

    successes, errors = renditions.process_source(
        s3, CANONICAL, [rendition("shirt-150x150.jpg", 150, 150)], 82, 86, protected)

    assert errors == []
    assert s3.puts == ["products/X/shirt-150x150.jpg"]
    assert s3.objects["products/X/shirt-150x150.jpg"] == b"RENDERED"
    assert successes[0].split("\t")[:2] == [CANONICAL, "shirt-150x150.jpg"]


def test_one_protected_destination_refuses_its_whole_group(s3, stub_render):
    protected = frozenset({CANONICAL, OTHER_CANONICAL})

    successes, errors = renditions.process_source(
        s3, CANONICAL,
        [rendition("shirt-150x150.jpg", 150, 150), rendition("shirt-300x300.jpg")],
        82, 86, protected)

    assert successes == []
    assert len(errors) == 2
    assert s3.puts == []


def test_the_original_self_overwrite_guards_still_hold(s3, stub_render):
    successes, errors = renditions.process_source(
        s3, CANONICAL, [rendition("shirt.jpg")], 82, 86, frozenset())
    assert successes == []
    assert "refused group containing a canonical overwrite" in errors[0]
    assert s3.puts == []


def test_load_canonical_keys_reads_the_mapping_table(monkeypatch):
    seen = []

    def fake_copy(sql):
        seen.append(sql)
        return "products/X/shirt.jpg\nproducts/X/shirt-300x300.jpg\n\n"

    monkeypatch.setattr(renditions, "psql_copy", fake_copy)
    keys = renditions.load_canonical_keys()

    assert keys == frozenset({CANONICAL, OTHER_CANONICAL})
    assert "pim.media_object" in seen[0]


def test_an_unreadable_key_list_fails_closed(monkeypatch):
    def fake_copy(sql):
        raise RuntimeError("psql failed")

    monkeypatch.setattr(renditions, "psql_copy", fake_copy)
    with pytest.raises(RuntimeError):
        renditions.load_canonical_keys()
