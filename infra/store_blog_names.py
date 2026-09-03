#!/usr/bin/env python3
"""Turn "blog_id<TAB>blogname" lines (from `wp eval` on the prod web box) into
UPDATE statements for woo.store_blog_map.blog_name. Entities are decoded and
quotes escaped; blogs that are not in the map simply match no row.

    python3 infra/store_blog_names.py < blognames.tsv > store-blog-names.sql
"""
import html
import sys

print("BEGIN;")
for line in sys.stdin:
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 2 or not parts[0].strip().isdigit():
        continue
    name = html.unescape(parts[1]).strip().replace("'", "''")
    print(f"UPDATE woo.store_blog_map SET blog_name = '{name}' WHERE blog_id = {int(parts[0])};")
print("COMMIT;")
