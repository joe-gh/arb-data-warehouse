"""A stateful stand-in for the WordPress category broker.

The old test doubles answered every phase with a synthesized success, so the
engine's contract with WordPress (fences, parking, membership replacement,
delete-only-when-empty, redirects, restore) was never exercised. This fake
keeps real per-blog state - terms, memberships, uncategorized products, the
UNSPSC site option, Redirection rules - and implements each broker phase the
way arb-category-apply.php does, including its refusals. Exports come from
that state, so the engine's live-state fence, post-apply verification and
restore verification all run for real against it.

It deliberately mirrors the broker's OBSERVABLE behaviour, not its code: if
the two ever disagree the integration script (tests/integration/) against a
real WordPress is the tie-breaker.
"""

from typing import Any, Dict, List, Optional, Set

import categories_service
from db import database


TEMP_PREFIX = "catmgrtmp-"


class FakeWordPress:
    def __init__(self, fail_on=None):
        self.blogs: Dict[int, Dict[str, Any]] = {}
        self.calls: List[Any] = []
        self.fail_on: Set[str] = set(fail_on or ())
        self.next_term_id = 5000
        self.redirection_available = True
        self.redirect_group_id = 1
        self.redirects: Dict[str, Dict[str, Any]] = {}     # old_path -> rule
        self.site_options: Dict[str, Any] = {"unspsc_category_mapping": []}
        self.es_queue: List[Any] = []
        self.status_overrides: Dict[str, Any] = {}
        self.record_probes = False

    # ------------------------------------------------------------ seeding

    def seed(self, blog_id: int, path: str, terms: List[Dict[str, Any]],
             products: List[Dict[str, Any]],
             uncategorized: Optional[List[Dict[str, Any]]] = None) -> None:
        blog = {"path": path, "terms": {}, "products": {}, "uncategorized": {}}
        for t in terms:
            blog["terms"][int(t["term_id"])] = {
                "slug": t["slug"], "name": t.get("name") or t["slug"],
                "parent": int(t.get("parent") or 0),
                "description": t.get("description") or "",
                "sort_order": int(t.get("sort_order") or 0),
                "thumbnail_id": int(t.get("thumbnail_id") or 0),
                "name_locked": bool(t.get("name_locked")),
                "parked_from": t.get("parked_from") or "",
                "created_by_run": None,
            }
            self.next_term_id = max(self.next_term_id, int(t["term_id"]) + 1)
        for p in products:
            product = blog["products"].setdefault(
                int(p["product_id"]), {"sku": str(p.get("sku") or "").upper(), "term_ids": set()},
            )
            if int(p["term_id"]) in blog["terms"]:
                product["term_ids"].add(int(p["term_id"]))
        for u in uncategorized or []:
            blog["uncategorized"][int(u["product_id"])] = str(u.get("sku") or "").upper()
        self.blogs[blog_id] = blog

    def seed_from_snapshot(self, env: str, blog_id: int) -> None:
        with database.cursor() as cursor:
            cursor.execute("SELECT blog_path FROM catmgr.snapshot WHERE env=%s AND blog_id=%s", (env, blog_id))
            row = cursor.fetchone()
            cursor.execute(
                "SELECT term_id, slug, name, parent_term_id AS parent, description, sort_order,"
                " thumbnail_id, name_locked, parked_from FROM catmgr.wp_term"
                " WHERE env=%s AND blog_id=%s ORDER BY term_id", (env, blog_id))
            terms = [dict(r) for r in cursor.fetchall()]
            cursor.execute(
                "SELECT term_id, product_id, sku FROM catmgr.wp_term_product WHERE env=%s AND blog_id=%s",
                (env, blog_id))
            products = [dict(r) for r in cursor.fetchall()]
            cursor.execute(
                "SELECT product_id, sku FROM catmgr.wp_uncategorized_product WHERE env=%s AND blog_id=%s",
                (env, blog_id))
            uncategorized = [dict(r) for r in cursor.fetchall()]
        self.seed(blog_id, row["blog_path"] if row else "/", terms, products, uncategorized)

    # ------------------------------------------------------------ reads

    def export(self, env: str, blog_id: int) -> Dict[str, Any]:
        blog = self.blogs[blog_id]
        terms = [{"term_id": tid, **{k: v for k, v in t.items() if k != "created_by_run"}}
                 for tid, t in sorted(blog["terms"].items())]
        products = [{"term_id": tid, "product_id": pid, "sku": p["sku"]}
                    for pid, p in sorted(blog["products"].items())
                    for tid in sorted(p["term_ids"])]
        uncategorized = [{"product_id": pid, "sku": sku}
                         for pid, sku in sorted(blog["uncategorized"].items())]
        for pid, p in blog["products"].items():
            if not p["term_ids"] and pid not in blog["uncategorized"]:
                uncategorized.append({"product_id": pid, "sku": p["sku"]})
        return {
            "broker_version": 2, "blog_id": blog_id, "blog_path": blog["path"],
            "terms": terms, "products": products, "products_total": len(products),
            "next_after": None, "uncategorized": uncategorized,
            "site_options": dict(self.site_options) if blog_id == 1 else {},
        }

    def status(self, env: str) -> Dict[str, Any]:
        base = {
            "broker_version": 2, "freeze": True, "redirection_active": self.redirection_available,
            "redirect_group_id": self.redirect_group_id if self.redirection_available else 0,
            "rocket_present": False, "durable_jobs": True, "avne_available": True,
            "unspsc_available": True, "job_table": True, "wp_version": "6.9",
        }
        base.update(self.status_overrides)
        return base

    # ------------------------------------------------------------ dispatch

    def __call__(self, env: str, path: str, payload: Dict[str, Any]) -> Any:
        if path == "/job":
            if self.record_probes:
                self.calls.append((env, path, payload))
            raise categories_service.BrokerError("No job with that key.", 404)
        self.calls.append((env, path, payload))
        if path in self.fail_on:
            raise categories_service.BrokerError(f"boom on {path}", 503)
        blog = self.blogs.get(int(payload.get("blog_id") or 0))
        if blog is None:
            raise categories_service.BrokerError("Unknown or archived blog.", 404)
        expected_path = payload.get("expected_blog_path")
        if expected_path and expected_path.rstrip("/") != blog["path"].rstrip("/"):
            raise categories_service.BrokerError(
                f"Plan was built for blog path {expected_path} but blog is {blog['path']}.", 409)
        handler = {
            "/apply-terms": self._apply_terms,
            "/apply-memberships": self._apply_memberships,
            "/finalize": self._finalize,
            "/restore": self._restore,
        }.get(path)
        if handler is None:
            raise AssertionError(f"unexpected broker path {path}")
        return handler(int(payload["blog_id"]), blog, payload)

    # ------------------------------------------------------------ helpers

    def _by_slug(self, blog, slug):
        for tid, t in blog["terms"].items():
            if t["slug"] == slug:
                return tid, t
        return None, None

    def _products_of(self, blog, tid):
        return [pid for pid, p in blog["products"].items() if tid in p["term_ids"]]

    def _park(self, blog, tid, run_id, request_id, reason):
        term = blog["terms"][tid]
        if not term["parked_from"]:
            term["parked_from"] = term["slug"]
        term["slug"] = f"{TEMP_PREFIX}{tid}"

    def _rewrite_unspsc(self, renames, merges):
        mapping = self.site_options.get("unspsc_category_mapping") or []
        present = {e.get("category_slug") for e in mapping}
        rewritten = 0
        for entry in mapping:
            slug = entry.get("category_slug")
            if slug in renames and renames[slug]:
                entry["category_slug"] = renames[slug]
                present.add(renames[slug])
                rewritten += 1
        kept = []
        for entry in mapping:
            slug = entry.get("category_slug")
            if slug in merges:
                target = merges[slug]
                rewritten += 1
                if target and target not in present:
                    entry["category_slug"] = target
                    present.add(target)
                    kept.append(entry)
                continue
            kept.append(entry)
        self.site_options["unspsc_category_mapping"] = kept
        return rewritten

    # ------------------------------------------------------------ phases

    def _apply_terms(self, blog_id, blog, payload):
        updates = payload.get("updates") or []
        creates = payload.get("creates") or []
        doomed = payload.get("doomed") or []
        run_id = int(payload.get("run_id") or 0)
        drift = []
        for u in updates:
            term = blog["terms"].get(int(u["term_id"]))
            if term is None:
                drift.append({"term_id": u["term_id"], "expected_slug": u["expected_slug"], "live": None})
                continue
            acceptable = {u["expected_slug"], u["set"]["slug"], f"{TEMP_PREFIX}{u['term_id']}"}
            if term["slug"] not in acceptable:
                drift.append({"term_id": u["term_id"], "expected_slug": u["expected_slug"], "live": term["slug"]})
        for d in doomed:
            term = blog["terms"].get(int(d["term_id"]))
            if term is None:
                continue
            if d.get("expected_slug") and term["slug"] not in {d["expected_slug"], f"{TEMP_PREFIX}{d['term_id']}"}:
                drift.append({"term_id": d["term_id"], "expected_slug": d["expected_slug"], "live": term["slug"], "doomed": True})
        vacating = {u["expected_slug"] for u in updates if u["expected_slug"] != u["set"]["slug"]}
        vacating |= {d["expected_slug"] for d in doomed if d.get("expected_slug")}
        for c in creates:
            if c["slug"] in vacating:
                continue
            tid, term = self._by_slug(blog, c["slug"])
            if term is not None and term.get("created_by_run") != run_id:
                drift.append({"term_id": tid, "slug": c["slug"], "live": term["slug"], "create": True,
                              "reason": "unplanned term already owns the slug"})
        if drift:
            return {"ok": False, "code": "arb_catmgr_drift",
                    "message": f"{len(drift)} term(s) no longer match the plan", "drift": drift}
        temp = 0
        for u in updates:
            term = blog["terms"][int(u["term_id"])]
            if term["slug"] != u["set"]["slug"] and not term["slug"].startswith(TEMP_PREFIX):
                self._park(blog, int(u["term_id"]), run_id, payload.get("request_id"), "update")
                temp += 1
        for d in doomed:
            tid = int(d["term_id"])
            if tid in blog["terms"] and not blog["terms"][tid]["slug"].startswith(TEMP_PREFIX):
                self._park(blog, tid, run_id, payload.get("request_id"), "doomed")
                temp += 1
        created = 0
        for c in creates:
            tid, term = self._by_slug(blog, c["slug"])
            if term is not None:
                if term.get("created_by_run") != run_id:
                    raise categories_service.BrokerError(
                        f"Create refused for {c['slug']}: an unplanned term owns the slug.", 409)
                term.update({"name": c["name"], "description": c.get("description") or "",
                             "sort_order": int(c.get("sort_order") or 0)})
                continue
            tid = self.next_term_id
            self.next_term_id += 1
            blog["terms"][tid] = {
                "slug": c["slug"], "name": c["name"], "parent": 0,
                "description": c.get("description") or "", "sort_order": int(c.get("sort_order") or 0),
                "thumbnail_id": 0, "name_locked": False, "parked_from": "", "created_by_run": run_id,
            }
            created += 1
        es_ids: Set[int] = set()
        for u in updates:
            tid = int(u["term_id"])
            term = blog["terms"][tid]
            term.update({"slug": u["set"]["slug"], "name": u["set"]["name"],
                         "description": u["set"].get("description") or "",
                         "sort_order": int(u["set"].get("sort_order") or 0), "parked_from": ""})
            if u.get("changed", {"slug": True}):
                es_ids.update(self._products_of(blog, tid))
        for row in [{"slug": u["set"]["slug"], "parent_slug": u["set"].get("parent_slug") or ""} for u in updates] + \
                   [{"slug": c["slug"], "parent_slug": c.get("parent_slug") or ""} for c in creates]:
            tid, term = self._by_slug(blog, row["slug"])
            if term is None:
                continue
            parent_id = 0
            if row["parent_slug"]:
                ptid, parent = self._by_slug(blog, row["parent_slug"])
                if parent is None or ptid == tid:
                    raise categories_service.BrokerError(
                        f"Parent {row['parent_slug']} for {row['slug']} does not exist on this blog.", 409)
                parent_id = ptid
            term["parent"] = parent_id
        rewritten = 0
        if blog_id == 1:
            rewritten = self._rewrite_unspsc(payload.get("unspsc_renames") or {}, payload.get("unspsc_merges") or {})
        if es_ids:
            self.es_queue.append((blog_id, sorted(es_ids)))
        return {"ok": True, "updated": len(updates), "created": created, "temp_passed": temp,
                "es_queued": len(es_ids), "unspsc_rewritten": rewritten}

    def _apply_memberships(self, blog_id, blog, payload):
        applied = 0
        skipped = []
        missing_slugs = set()
        changed = []
        for row in payload.get("rows") or []:
            pid = int(row["product_id"])
            product = blog["products"].get(pid)
            if product is None and pid in blog["uncategorized"]:
                product = blog["products"].setdefault(pid, {"sku": blog["uncategorized"][pid], "term_ids": set()})
            if product is None:
                skipped.append({"product_id": pid, "reason": "missing"})
                continue
            expected_sku = str(row.get("expected_sku") or "").upper()
            if product["sku"] != expected_sku:
                skipped.append({"product_id": pid, "reason": "sku_mismatch", "live": product["sku"], "expected": expected_sku})
                continue
            final_ids = []
            row_missing = False
            for slug in row.get("final_slugs") or []:
                tid, term = self._by_slug(blog, slug)
                if term is None:
                    missing_slugs.add(slug)
                    row_missing = True
                else:
                    final_ids.append(tid)
            if row_missing:
                skipped.append({"product_id": pid, "reason": "missing_slug"})
                continue
            if "expected_term_ids" in row:
                expected_ids = sorted(int(x) for x in row["expected_term_ids"])
                current = sorted(product["term_ids"])
                if current != expected_ids and current != sorted(set(final_ids)):
                    skipped.append({"product_id": pid, "reason": "membership_drift",
                                    "live": current, "expected": expected_ids})
                    continue
            product["term_ids"] = set(final_ids)
            blog["uncategorized"].pop(pid, None)
            changed.append(pid)
            applied += 1
        if changed:
            self.es_queue.append((blog_id, sorted(changed)))
        return {"ok": not skipped and not missing_slugs, "applied": applied,
                "skipped": skipped[:50], "skipped_count": len(skipped),
                "missing_slugs": sorted(missing_slugs)}

    def _finalize(self, blog_id, blog, payload):
        deleted = 0
        report = []
        for d in payload.get("deletes") or []:
            tid = int(d["term_id"])
            term = blog["terms"].get(tid)
            if term is None:
                report.append({"term_id": tid, "status": "already_gone"})
                continue
            if d.get("expected_slug") and term["slug"] not in {d["expected_slug"], f"{TEMP_PREFIX}{tid}"}:
                report.append({"term_id": tid, "status": "slug_drift", "live": term["slug"]})
                continue
            attached = self._products_of(blog, tid)
            if attached:
                report.append({"term_id": tid, "status": "has_products", "products": attached[:20], "count": len(attached)})
                continue
            for child in blog["terms"].values():
                if child["parent"] == tid:
                    child["parent"] = term["parent"]
            del blog["terms"][tid]
            deleted += 1
        created = []
        failed = []
        if blog_id == 1 and payload.get("redirects"):
            for r in payload["redirects"]:
                if not self.redirection_available:
                    failed.append({"old_path": r["old_path"], "reason": "redirection_unavailable"})
                    continue
                if self.redirect_group_id < 1:
                    failed.append({"old_path": r["old_path"], "reason": "no_enabled_group"})
                    continue
                existing = self.redirects.get(r["old_path"])
                if existing and existing["new_path"] == r["new_path"] and existing["code"] == 301 and existing["enabled"]:
                    created.append({"old_path": r["old_path"], "new_path": r["new_path"], "existing": True})
                    continue
                if existing:
                    existing.update({"new_path": r["new_path"], "code": 301, "enabled": True})
                    created.append({"old_path": r["old_path"], "new_path": r["new_path"], "updated": True})
                    continue
                self.redirects[r["old_path"]] = {"new_path": r["new_path"], "code": 301, "enabled": True}
                created.append({"old_path": r["old_path"], "new_path": r["new_path"]})
        rewritten = 0
        if blog_id == 1:
            rewritten = self._rewrite_unspsc(payload.get("unspsc_renames") or {}, payload.get("unspsc_merges") or {})
        refused = sum(1 for r in report if r["status"] in ("slug_drift", "failed", "has_products"))
        return {"ok": refused == 0 and not failed, "deleted": deleted, "delete_report": report,
                "refused_deletes": refused, "recounted_terms": len(blog["terms"]),
                "es_processed": None, "redirects_created": created, "redirects_failed": failed,
                "unspsc_rewritten": rewritten}

    def _restore(self, blog_id, blog, payload):
        snapshot = payload.get("snapshot") or {}
        terms = snapshot.get("terms") or []
        products = snapshot.get("products") or []
        phase = payload.get("phase") or ""
        failures: List[Dict[str, Any]] = []
        id_to_slug = {int(t["term_id"]): t["slug"] for t in terms}
        slug_to_id: Dict[str, int] = {}
        created = updated = 0
        options_restored = 0
        if phase in ("", "terms"):
            options = snapshot.get("site_options") or {}
            if blog_id == 1 and isinstance(options.get("unspsc_category_mapping"), list):
                self.site_options["unspsc_category_mapping"] = [dict(e) for e in options["unspsc_category_mapping"]]
                options_restored += 1
            for t in terms:
                tid = int(t["term_id"])
                slug = t["slug"]
                live = blog["terms"].get(tid)
                if live is not None:
                    if live["slug"] != slug:
                        holder_id, holder = self._by_slug(blog, slug)
                        if holder is not None and holder_id != tid:
                            self._park(blog, holder_id, 0, "", "restore-holder")
                    live.update({"slug": slug, "name": t.get("name") or slug,
                                 "description": t.get("description") or "", "parked_from": ""})
                    slug_to_id[slug] = tid
                    updated += 1
                    continue
                existing_id, existing = self._by_slug(blog, slug)
                if existing is not None:
                    existing.update({"name": t.get("name") or slug, "description": t.get("description") or "",
                                     "parked_from": ""})
                    slug_to_id[slug] = existing_id
                    updated += 1
                else:
                    new_id = self.next_term_id
                    self.next_term_id += 1
                    blog["terms"][new_id] = {"slug": slug, "name": t.get("name") or slug, "parent": 0,
                                             "description": t.get("description") or "", "sort_order": 0,
                                             "thumbnail_id": 0, "name_locked": False, "parked_from": "",
                                             "created_by_run": None}
                    slug_to_id[slug] = new_id
                    created += 1
            for t in terms:
                tid = slug_to_id.get(t["slug"])
                if tid is None:
                    continue
                parent_snapshot = int(t.get("parent") or 0)
                parent_id = 0
                if parent_snapshot:
                    parent_slug = id_to_slug.get(parent_snapshot)
                    if parent_slug in slug_to_id:
                        parent_id = slug_to_id[parent_slug]
                    else:
                        failures.append({"slug": t["slug"], "step": "parent", "error": "parent not restored"})
                blog["terms"][tid].update({"parent": parent_id, "sort_order": int(t.get("sort_order") or 0),
                                           "thumbnail_id": int(t.get("thumbnail_id") or 0),
                                           "name_locked": bool(t.get("name_locked"))})
        else:
            for t in terms:
                tid, term = self._by_slug(blog, t["slug"])
                if term is not None:
                    slug_to_id[t["slug"]] = tid
        if phase == "terms":
            return {"ok": not failures, "phase": "terms", "terms": len(slug_to_id), "terms_expected": len(terms),
                    "created": created, "updated": updated, "options_restored": options_restored,
                    "failures": failures}
        removed = 0
        if phase in ("", "finalize"):
            keep = set(slug_to_id)
            for tid in list(blog["terms"]):
                if blog["terms"][tid]["slug"] not in keep and blog["terms"][tid]["slug"] != "uncategorized":
                    parent = blog["terms"][tid]["parent"]
                    for child in blog["terms"].values():
                        if child["parent"] == tid:
                            child["parent"] = parent
                    del blog["terms"][tid]
                    removed += 1
        by_product: Dict[int, List[int]] = {}
        for p in products:
            pid = int(p["product_id"])
            slug = id_to_slug.get(int(p["term_id"]))
            if slug is None or slug not in slug_to_id:
                failures.append({"product_id": pid, "term_id": p["term_id"], "step": "membership_term_missing"})
                by_product.setdefault(pid, [])
                continue
            by_product.setdefault(pid, []).append(slug_to_id[slug])
        restored = 0
        if phase in ("", "memberships"):
            for pid, tids in by_product.items():
                product = blog["products"].get(pid)
                if product is None:
                    if pid in blog["uncategorized"]:
                        product = blog["products"].setdefault(pid, {"sku": blog["uncategorized"].pop(pid), "term_ids": set()})
                    else:
                        failures.append({"product_id": pid, "step": "product_missing"})
                        continue
                product["term_ids"] = set(tids)
                restored += 1
        if phase == "memberships":
            return {"ok": not failures, "phase": "memberships", "products_restored": restored,
                    "products_expected": len(by_product), "products_offset": payload.get("products_offset"),
                    "failures": failures}
        return {"ok": not failures, "phase": phase or "all", "terms": len(slug_to_id), "terms_expected": len(terms),
                "products_restored": restored, "products_expected": len(by_product), "terms_removed": removed,
                "options_restored": options_restored, "failures": failures}
