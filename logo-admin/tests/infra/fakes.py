"""Stand-ins for everything the infra scripts touch outside this process.

These tests never reach S3, the Sales Layer API, the network or a shell; the
one exception is the pull_pim retirement test, which needs a real database to
exercise the migration it depends on.
"""

import hashlib
import importlib.util
import io
import sys
import types
from pathlib import Path

INFRA_DIR = Path(__file__).resolve().parents[3] / "infra"


def load_script(filename, module_name):
    """Load an infra script by file path.

    The repository root's `infra/` directory is shadowed by logo-admin's own
    `infra` package under pytest's rootdir, so a plain import can never reach
    these modules - the same reason tests/test_pipeline_manifest.py loads
    load_dump.py this way.
    """
    path = INFRA_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeS3:
    """The slice of the boto3 S3 client the media scripts use: an in-memory
    bucket, put_object, get_object and the list_objects_v2 paginator."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None, CacheControl=None):
        self.puts.append(Key)
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise KeyError(f"no such object: {Key}")
        return {"Body": io.BytesIO(self.objects[Key])}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _FakePaginator(self)


class _FakePaginator:
    def __init__(self, s3):
        self._s3 = s3

    def paginate(self, Bucket=None, Prefix=""):
        contents = [
            {
                "Key": key,
                "ETag": '"' + hashlib.md5(body).hexdigest() + '"',
                "Size": len(body),
            }
            for key, body in sorted(self._s3.objects.items())
            if key.startswith(Prefix)
        ]
        return [{"Contents": contents}]


def install_fake_boto3(monkeypatch, s3):
    """Publish a boto3 module whose client() hands back the fake bucket.
    The media scripts import boto3 inside main(), so this is enough."""
    module = types.ModuleType("boto3")
    module.__version__ = "0-fake"
    module.client = lambda *a, **kw: s3
    monkeypatch.setitem(sys.modules, "boto3", module)
    return module


class _FakeImage:
    """Just enough of a Pillow image for generate-renditions' decode step;
    the rendering itself is stubbed out in the tests that use this."""

    def __init__(self):
        self.info = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def seek(self, frame):
        return None

    def copy(self):
        clone = _FakeImage()
        clone.info = dict(self.info)
        return clone


def install_fake_pil(monkeypatch):
    """Publish a PIL package with the two names process_source imports."""
    pil = types.ModuleType("PIL")
    image_module = types.ModuleType("PIL.Image")
    image_module.open = lambda *a, **kw: _FakeImage()
    imageops_module = types.ModuleType("PIL.ImageOps")
    imageops_module.exif_transpose = lambda image: image
    pil.Image = image_module
    pil.ImageOps = imageops_module
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_module)
    monkeypatch.setitem(sys.modules, "PIL.ImageOps", imageops_module)
    return pil


class FakePsql:
    """A subprocess.run stand-in for the `psql` shell-outs.

    Answers the COPY reads by SQL text and records every mapping flush so a
    test can see exactly which rows would have been written.
    """

    def __init__(self, answers=None, fail_flush=False):
        self.answers = dict(answers or {})
        self.fail_flush = fail_flush
        self.flushes = []
        self.queries = []

    def __call__(self, argv, input=None, capture_output=False, text=False, **kwargs):
        if "-f" in argv:
            self.flushes.append(input or "")
            if self.fail_flush:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="flush refused")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        sql = argv[argv.index("-c") + 1] if "-c" in argv else ""
        self.queries.append(sql)
        for fragment, answer in self.answers.items():
            if fragment in sql:
                return types.SimpleNamespace(returncode=0, stdout=answer, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def flushed_rows(self):
        """Every mapping row across all flushes, as split TSV fields."""
        rows = []
        for script in self.flushes:
            body = script.split("COPY _mo FROM STDIN;\n", 1)
            if len(body) != 2:
                body = script.split("COPY _mr FROM STDIN;\n", 1)
            if len(body) != 2:
                continue
            for line in body[1].split("\n\\.\n", 1)[0].splitlines():
                if line:
                    rows.append(line.split("\t"))
        return rows
