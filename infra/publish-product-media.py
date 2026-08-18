#!/usr/bin/env python3
"""Publish every unique product image referenced in pim.* to the canonical
S3 bucket (arborwear-product-media), served at https://assets.arborwear.com/.

Runs on the warehouse box as ubuntu (instance role arb-warehouse-media-publisher
provides S3 write). Reads/writes Postgres via `sudo -u postgres psql` so no DB
credentials are needed. Source bytes are fetched as plain public HTTP GETs with
low concurrency - the WP side is never touched or modified.

Idempotent and resumable: URLs already present in pim.media_object are skipped,
so re-running publishes only new images (run it after Sales Layer pushes or a
new backfill to keep the bucket current).

Usage:
  python3 publish-product-media.py [--limit N] [--dry-run] [--workers 4]
"""

import argparse
import hashlib
import json
import mimetypes
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BUCKET = "arborwear-product-media"
CDN_BASE = "https://assets.arborwear.com/"
DB = "arb_warehouse"
UA = "arb-warehouse-media-publisher/1.0 (+warehouse; image sync to assets.arborwear.com)"

URL_QUERY = r"""
COPY (
  WITH urls AS (
    SELECT upper(btrim(sku_parent)) AS sku, img->>'src' AS url
      FROM pim.product_state, jsonb_array_elements(coalesce(payload->'parent'->'images','[]'::jsonb)) img
     WHERE coalesce(img->>'src','') <> ''
    UNION ALL
    SELECT upper(btrim(sku_parent)), v->'image'->>'src'
      FROM pim.product_state, jsonb_array_elements(coalesce(payload->'variations','[]'::jsonb)) v
     WHERE coalesce(v->'image'->>'src','') <> ''
  )
  SELECT DISTINCT ON (url) url, sku FROM urls ORDER BY url, sku
) TO STDOUT
"""


FLUSH_FAIL_FILE = "/home/ubuntu/media-publish-failed-rows.tsv"


def flush_rows(batch: list[str]) -> None:
    """Upsert mapping rows; duplicate source_urls are ignored. Never raises -
    a failed batch is appended to FLUSH_FAIL_FILE for manual replay."""
    if not batch:
        return
    script = (
        "BEGIN;\n"
        "CREATE TEMP TABLE _mo (source_url text, s3_key text, cdn_url text, sku_parent text,"
        " content_md5 text, bytes bigint, content_type text) ON COMMIT DROP;\n"
        "COPY _mo FROM STDIN;\n"
        + "\n".join(batch) + "\n\\.\n"
        "INSERT INTO pim.media_object (source_url, s3_key, cdn_url, sku_parent, content_md5, bytes, content_type)\n"
        "SELECT source_url, s3_key, cdn_url, sku_parent, content_md5, bytes, content_type FROM _mo\n"
        "ON CONFLICT (source_url) DO NOTHING;\n"
        "COMMIT;\n"
    )
    proc = subprocess.run(
        ["sudo", "-u", "postgres", "psql", DB, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=script, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"FLUSH FAILED ({len(batch)} rows spilled): {proc.stderr[:300]}")
        with open(FLUSH_FAIL_FILE, "a") as fh:
            fh.write("\n".join(batch) + "\n")


def psql(sql: str, input_data: str | None = None) -> str:
    proc = subprocess.run(
        ["sudo", "-u", "postgres", "psql", DB, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", sql],
        input=input_data, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr[:500]}")
    return proc.stdout


def sanitize(name: str) -> str:
    name = name.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:120] or "image"


def fetch(url: str) -> tuple[bytes, str]:
    # IRI -> URI: percent-encode non-ASCII path characters (e.g. a literal ″
    # in a filename) or urllib refuses the request outright.
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit((
        parts.scheme, parts.netloc,
        urllib.parse.quote(parts.path, safe="/%"),
        urllib.parse.quote(parts.query, safe="=&%"), parts.fragment,
    ))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
                return data, ctype
        except Exception as e:  # noqa: BLE001 - retried, reported at the end
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed after retries: {last}")


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    import boto3  # deferred: --dry-run listing works without it

    proc = subprocess.run(
        ["sudo", "-u", "postgres", "psql", DB, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", URL_QUERY],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr[:500], file=sys.stderr)
        return 1
    todo = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0].startswith("http"):
            todo.append((parts[0], parts[1]))

    done_raw = psql("COPY (SELECT source_url FROM pim.media_object) TO STDOUT")
    done = set(done_raw.splitlines())
    todo = [t for t in todo if t[0] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"unique image urls to publish: {len(todo)} (already published: {len(done)})")
    if args.dry_run or not todo:
        for u, s in todo[:10]:
            print(f"  would publish {u} -> products/{s}/{sanitize(u)}")
        return 0

    s3 = boto3.client("s3", region_name="us-east-2")

    # Reconcile: objects already uploaded (e.g. by an interrupted run) get their
    # mapping from S3 metadata - single-part ETags are the content md5 - so we
    # never re-fetch bytes the bucket already has.
    existing: dict[str, tuple[str, int]] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix="products/"):
        for obj in page.get("Contents", []):
            etag = obj["ETag"].strip('"')
            existing[obj["Key"]] = (etag if "-" not in etag else "", obj["Size"])
    if existing:
        esc0 = lambda s0: s0.replace("\\", "\\\\").replace("\t", " ").replace("\n", " ").replace("\r", " ")
        reconciled, keep = [], []
        for url, sku in todo:
            key = f"products/{sku or 'UNKNOWN'}/{sanitize(url)}"
            hit = existing.get(key)
            if hit is not None:
                ctype = mimetypes.guess_type(key)[0] or ""
                reconciled.append("\t".join([esc0(url), esc0(key), esc0(CDN_BASE + key), esc0(sku), hit[0], str(hit[1]), esc0(ctype)]))
            else:
                keep.append((url, sku))
        for i in range(0, len(reconciled), 500):
            flush_rows(reconciled[i:i + 500])
        print(f"reconciled from S3 without fetching: {len(reconciled)}; still to fetch: {len(keep)}")
        todo = keep

    used_keys: dict[str, str] = {}
    rows: list[str] = []
    errors: list[str] = []
    lock_pad = 0

    def publish(item: tuple[str, str]) -> str | None:
        url, sku = item
        data, ctype = fetch(url)
        if not ctype.startswith("image/"):
            guess = mimetypes.guess_type(url)[0] or ""
            if not guess.startswith("image/"):
                raise RuntimeError(f"not an image (content-type {ctype!r})")
            ctype = guess
        md5 = hashlib.md5(data).hexdigest()
        base = sanitize(url)
        key = f"products/{sku or 'UNKNOWN'}/{base}"
        prior = used_keys.get(key)
        if prior is not None and prior != md5:
            stem, dot, ext = base.rpartition(".")
            base = f"{stem or ext}-{md5[:8]}{dot}{ext if stem else ''}"
            key = f"products/{sku or 'UNKNOWN'}/{base}"
        used_keys[key] = md5
        s3.put_object(
            Bucket=BUCKET, Key=key, Body=data, ContentType=ctype,
            CacheControl="public, max-age=31536000, immutable",
        )
        esc = lambda s: s.replace("\\", "\\\\").replace("\t", " ").replace("\n", " ").replace("\r", " ")
        rows.append("\t".join([esc(url), esc(key), esc(CDN_BASE + key), esc(sku), md5, str(len(data)), esc(ctype)]))
        return None

    started = time.time()
    ok = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(publish, item): item for item in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            url, _ = futures[fut]
            try:
                fut.result()
                ok += 1
            except Exception as e:  # noqa: BLE001 - collected and reported
                errors.append(f"{url}\t{e}")
            if i % 500 == 0:
                print(f"  {i}/{len(todo)} ({ok} ok, {len(errors)} err, {int(time.time()-started)}s)")
            if len(rows) >= 250:
                batch, rows[:] = rows[:], []
                flush_rows(batch)
    flush_rows(rows)
    print(f"published {ok}/{len(todo)} in {int(time.time()-started)}s; {len(errors)} errors")
    for e in errors[:20]:
        print("  ERR " + e)
    if len(errors) > 20:
        print(f"  ... and {len(errors) - 20} more")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
