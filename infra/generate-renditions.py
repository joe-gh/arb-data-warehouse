#!/usr/bin/env python3
"""Generate WordPress product-image renditions next to canonical S3 objects.

Runs on the warehouse box as ubuntu. The instance role supplies S3 read/write;
Postgres access defaults to the local socket (sudo -u postgres psql), matching
publish-product-media.py. Set ARB_WH_PSQL_DSN to run this on a SEPARATE box
(e.g. a temporary high-CPU generator instance) -- it then shells out to psql
with that connection string instead. Behaviour is otherwise identical.
Only rows exported from WordPress metadata are processed, and the canonical
source key is guarded against overwrite.

Usage:
  python3 generate-renditions.py [--limit N] [--dry-run] [--workers 4]
"""

import argparse
import io
import math
import mimetypes
import os
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

BUCKET = "arborwear-product-media"
DB = "arb_warehouse"

# Remote mode: ARB_WH_PSQL_DSN (e.g. "postgresql://pim_writer:***@host:6432/arb_warehouse")
# lets the generator run off-box. Empty -> local postgres socket, as before.
_DSN = os.environ.get("ARB_WH_PSQL_DSN", "").strip()


def _psql_argv() -> list:
    """psql invocation for the configured target (remote DSN or local socket)."""
    if _DSN:
        return ["psql", _DSN, "-X", "-q", "-v", "ON_ERROR_STOP=1"]
    return ["sudo", "-u", "postgres", "psql", DB, "-X", "-q", "-v", "ON_ERROR_STOP=1"]

CACHE_CONTROL = "public, max-age=31536000, immutable"
FLUSH_FAIL_FILE = "/home/ubuntu/media-rendition-failed-rows.tsv"


@dataclass(frozen=True)
class Rendition:
    canonical_key: str
    rendition_file: str
    width: int
    height: int
    size_name: str
    content_type: str
    crop: bool
    crop_x: str
    crop_y: str

    @property
    def destination_key(self) -> str:
        prefix = self.canonical_key.rsplit("/", 1)[0]
        return f"{prefix}/{self.rendition_file}"


def psql_copy(sql: str) -> str:
    proc = subprocess.run(
        _psql_argv() + ["-c", sql],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr[:500]}")
    return proc.stdout


def load_pending(limit: int) -> list[Rendition]:
    limit_sql = f" LIMIT {limit}" if limit > 0 else ""
    raw = psql_copy(
        "COPY ("
        "SELECT canonical_key, rendition_file, width, height, size_name, content_type, "
        "       CASE WHEN crop THEN 1 ELSE 0 END, crop_x, crop_y "
        "  FROM pim.media_rendition WHERE generated = false "
        f" ORDER BY canonical_key, rendition_file{limit_sql}"
        ") TO STDOUT"
    )
    rows: list[Rendition] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 9:
            continue
        rows.append(
            Rendition(
                canonical_key=parts[0],
                rendition_file=parts[1],
                width=int(parts[2]),
                height=int(parts[3]),
                size_name=parts[4],
                content_type=parts[5],
                crop=parts[6] == "1",
                crop_x=parts[7],
                crop_y=parts[8],
            )
        )
    return rows


def tsv_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\t", " ").replace("\n", " ").replace("\r", " ")


def flush_rows(batch: list[str]) -> bool:
    """Mark successfully uploaded definitions generated; spill on DB failure."""
    if not batch:
        return True
    script = (
        "BEGIN;\n"
        "CREATE TEMP TABLE _mr (canonical_key text, rendition_file text, width int, height int, "
        "content_type text, crop boolean, crop_x text, crop_y text, bytes bigint) ON COMMIT DROP;\n"
        "COPY _mr FROM STDIN;\n"
        + "\n".join(batch)
        + "\n\\.\n"
        "UPDATE pim.media_rendition r\n"
        "   SET generated = true, bytes = x.bytes, updated_at = now()\n"
        "  FROM _mr x\n"
        " WHERE r.canonical_key = x.canonical_key\n"
        "   AND r.rendition_file = x.rendition_file\n"
        "   AND (r.width, r.height, r.content_type, r.crop, r.crop_x, r.crop_y)\n"
        "       IS NOT DISTINCT FROM\n"
        "       (x.width, x.height, x.content_type, x.crop, x.crop_x, x.crop_y);\n"
        "COMMIT;\n"
    )
    proc = subprocess.run(
        _psql_argv() + ["-f", "-"],
        input=script,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True

    print(f"FLUSH FAILED ({len(batch)} rows spilled): {proc.stderr[:300]}", file=sys.stderr)
    with open(FLUSH_FAIL_FILE, "a", encoding="utf-8") as failed:
        failed.write("\n".join(batch) + "\n")
    return False


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def crop_box(width: int, height: int, target_w: int, target_h: int, crop_x: str, crop_y: str) -> tuple[int, int, int, int]:
    ratio = max(target_w / width, target_h / height)
    crop_w = min(width, max(1, round_half_up(target_w / ratio)))
    crop_h = min(height, max(1, round_half_up(target_h / ratio)))

    if crop_x == "left":
        left = 0
    elif crop_x == "right":
        left = width - crop_w
    else:
        left = math.floor((width - crop_w) / 2)

    if crop_y == "top":
        top = 0
    elif crop_y == "bottom":
        top = height - crop_h
    else:
        top = math.floor((height - crop_h) / 2)

    return left, top, left + crop_w, top + crop_h


def target_type(row: Rendition) -> str:
    content_type = row.content_type.lower().strip()
    if content_type in {"image/jpeg", "image/png", "image/webp"}:
        return content_type
    guessed = mimetypes.guess_type(row.rendition_file)[0] or ""
    if guessed in {"image/jpeg", "image/png", "image/webp"}:
        return guessed
    raise RuntimeError(f"unsupported output type {row.content_type!r} for {row.rendition_file}")


def render(base_image, row: Rendition, jpeg_quality: int, webp_quality: int) -> tuple[bytes, str]:
    from PIL import Image, ImageFilter

    image = base_image.copy()
    # Palette images resize poorly (nearest-color artifacts); promote to a
    # continuous mode BEFORE crop/resize. Transparency survives via RGBA.
    if image.mode == "P":
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    if row.crop:
        image = image.crop(crop_box(image.width, image.height, row.width, row.height, row.crop_x, row.crop_y))
    if image.size != (row.width, row.height):
        # Imagick's WordPress editor uses FILTER_TRIANGLE; Pillow BILINEAR is its closest built-in analogue.
        image = image.resize((row.width, row.height), resample=Image.Resampling.BILINEAR)

    output_type = target_type(row)
    icc_profile = base_image.info.get("icc_profile")
    save_args: dict[str, object] = {}
    if icc_profile:
        save_args["icc_profile"] = icc_profile

    if output_type == "image/jpeg":
        if image.mode not in {"RGB", "L", "CMYK"}:
            if "A" in image.getbands():
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
        # Mirrors WordPress Imagick's post-resize JPEG unsharp mask parameters.
        image = image.filter(ImageFilter.UnsharpMask(radius=0.25, percent=800, threshold=17))
        fmt = "JPEG"
        save_args.update(quality=jpeg_quality, optimize=True)
    elif output_type == "image/webp":
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        fmt = "WEBP"
        if base_image.info.get("_arb_webp_lossless"):
            save_args.update(lossless=True, quality=100, method=6)
        else:
            save_args.update(quality=webp_quality, method=6)
    else:
        if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        fmt = "PNG"
        save_args.update(optimize=True, compress_level=9)

    output = io.BytesIO()
    image.save(output, format=fmt, **save_args)
    return output.getvalue(), output_type


def process_source(s3, canonical_key: str, rows: list[Rendition], jpeg_quality: int, webp_quality: int) -> tuple[list[str], list[str]]:
    from PIL import Image, ImageOps

    successes: list[str] = []
    errors: list[str] = []
    canonical_basename = canonical_key.rsplit("/", 1)[-1]
    unsafe = [row for row in rows if row.destination_key == canonical_key or row.rendition_file == canonical_basename]
    if unsafe:
        return [], [f"{row.destination_key}\trefused group containing a canonical overwrite" for row in rows]

    try:
        source = s3.get_object(Bucket=BUCKET, Key=canonical_key)["Body"].read()
        with Image.open(io.BytesIO(source)) as opened:
            opened.seek(0)
            base = ImageOps.exif_transpose(opened).copy()
            base.info.update({k: v for k, v in opened.info.items() if k == "icc_profile"})
            base.info["_arb_webp_lossless"] = source.startswith(b"RIFF") and b"VP8L" in source
    except Exception as exc:  # noqa: BLE001 - one source error applies to every child row
        return [], [f"{row.destination_key}\tread/decode failed: {exc}" for row in rows]

    for row in rows:
        try:
            body, content_type = render(base, row, jpeg_quality, webp_quality)
            s3.put_object(
                Bucket=BUCKET,
                Key=row.destination_key,
                Body=body,
                ContentType=content_type,
                CacheControl=CACHE_CONTROL,
            )
            successes.append(
                "\t".join(
                    [
                        tsv_escape(row.canonical_key),
                        tsv_escape(row.rendition_file),
                        str(row.width),
                        str(row.height),
                        tsv_escape(row.content_type),
                        "t" if row.crop else "f",
                        row.crop_x,
                        row.crop_y,
                        str(len(body)),
                    ]
                )
            )
        except Exception as exc:  # noqa: BLE001 - continue independent sibling renditions
            errors.append(f"{row.destination_key}\t{exc}")
    return successes, errors


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="maximum pending rendition rows")
    parser.add_argument("--dry-run", action="store_true", help="list pending work without S3/DB writes")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--jpeg-quality", type=int, default=82)
    parser.add_argument("--webp-quality", type=int, default=86)
    args = parser.parse_args()

    if args.workers < 1 or args.workers > 128:
        parser.error("--workers must be between 1 and 128")
    if not 1 <= args.jpeg_quality <= 100 or not 1 <= args.webp_quality <= 100:
        parser.error("quality values must be between 1 and 100")

    try:
        pending = load_pending(max(0, args.limit))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(str(exc), file=sys.stderr)
        return 1

    grouped: dict[str, list[Rendition]] = defaultdict(list)
    for row in pending:
        grouped[row.canonical_key].append(row)
    print(f"pending renditions: {len(pending)} across {len(grouped)} canonical object(s)")
    if args.dry_run or not pending:
        for row in pending[:20]:
            mode = f"crop:{row.crop_x}/{row.crop_y}" if row.crop else "fit"
            print(f"  would generate s3://{BUCKET}/{row.destination_key} {row.width}x{row.height} {mode} {row.content_type}")
        return 0

    try:
        import boto3
        import PIL
        from PIL import features

        print(f"runtime: boto3={boto3.__version__} Pillow={PIL.__version__}")
        missing_features = [name for name in ("jpg", "zlib", "webp") if not features.check(name)]
        if missing_features:
            print(f"Pillow lacks required codec(s): {', '.join(missing_features)}", file=sys.stderr)
            return 1
    except ImportError as exc:
        print(f"missing runtime dependency ({exc}); install Ubuntu package python3-pil", file=sys.stderr)
        return 1

    s3 = boto3.client("s3", region_name="us-east-2")
    update_rows: list[str] = []
    errors: list[str] = []
    flush_failed = False
    completed = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_source, s3, key, rows, args.jpeg_quality, args.webp_quality): (key, len(rows))
            for key, rows in grouped.items()
        }
        for future in as_completed(futures):
            key, count = futures[future]
            try:
                successes, source_errors = future.result()
                update_rows.extend(successes)
                errors.extend(source_errors)
            except Exception as exc:  # noqa: BLE001 - worker boundary
                errors.extend([f"{key}\tworker failed: {exc}"] * count)
            completed += count
            if completed % 500 < count:
                print(f"  {completed}/{len(pending)} ({len(update_rows)} ready, {len(errors)} err, {int(time.time()-started)}s)")
            if len(update_rows) >= 250:
                batch, update_rows[:] = update_rows[:], []
                if not flush_rows(batch):
                    flush_failed = True

    if not flush_rows(update_rows):
        flush_failed = True
    ok = len(pending) - len(errors)
    print(f"generated {ok}/{len(pending)} in {int(time.time()-started)}s; {len(errors)} errors")
    for error in errors[:20]:
        print("  ERR " + error)
    if len(errors) > 20:
        print(f"  ... and {len(errors) - 20} more")
    return 0 if not errors and not flush_failed else 2


if __name__ == "__main__":
    sys.exit(main())
