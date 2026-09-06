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

import binascii
import hashlib
from typing import Any, Dict, List, Optional, Set

import categories_service
from db import database


TEMP_PREFIX = "catmgrtmp-"
# The broker's own limits (arb-category-apply.php), mirrored so the engine
# meets the same refusals here as on a real site.
BROKER_VERSION = 3
EXPORT_PRODUCTS_MAX = 20000
RESTORE_ROWS_MAX = 6000
RESTORE_REDIRECTS_MAX = 2000


class FakeWordPress:
    def __init__(self, fail_on=None):
        self.blogs: Dict[int, Dict[str, Any]] = {}
        self.calls: List[Any] = []
        self.fail_on: Set[str] = set(fail_on or ())
        self.next_term_id = 5000
        self.redirection_available = True
        self.redirect_group_id = 1
        self.redirects: Dict[str, Dict[str, Any]] = {}     # old_path -> rule
        self.next_redirect_id = 1
        self.site_options: Dict[str, Any] = {"unspsc_category_mapping": []}
        self.es_queue: List[Any] = []
        self.status_overrides: Dict[str, Any] = {}
        self.record_probes = False
        # Durable job rows, keyed exactly like the broker's job table
        # (request_id:phase[:page]); /job answers from here and a phase whose
        # key already converged replays its stored body.
        self.jobs: Dict[str, Dict[str, Any]] = {}
        # Paging knobs for the /export stand-in.
        self.export_pages = 0
        self.on_export_page = None
        self.uncategorized_page_size = EXPORT_PRODUCTS_MAX
        # A pre-v3 broker cuts the uncategorized set off with no cursor.
        self.uncategorized_pageable = True

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

    def export_generation(self, blog_id: int) -> str:
        """The broker's state digest: md5 of the five integers
        count|sum|xor of crc32(term_taxonomy_id-object_id) and
        count|xor of crc32(term_id|slug|name|parent|description).

        WordPress has a separate term_taxonomy_id per term; this fake has one
        id per term, so it keys the membership half on term_id. Only equality
        between two pages of one export matters."""

        blog = self.blogs[blog_id]
        rows = [(tid, pid) for pid, p in blog["products"].items()
                for tid in p["term_ids"]]
        checksum = 0
        parity = 0
        for tid, pid in rows:
            value = binascii.crc32(f"{tid}-{pid}".encode("utf-8"))
            checksum += value
            parity ^= value
        term_parity = 0
        for tid, t in blog["terms"].items():
            term_parity ^= binascii.crc32(
                f"{tid}|{t['slug']}|{t['name']}|{t['parent']}|{t['description']}"
                .encode("utf-8")
            )
        body = "|".join(str(n) for n in (len(rows), checksum, parity,
                                         len(blog["terms"]), term_parity))
        return hashlib.md5(body.encode("utf-8"), usedforsecurity=False).hexdigest()

    def export(self, env: str, blog_id: int) -> Dict[str, Any]:
        blog = self.blogs[blog_id]
        terms = [{"term_id": tid, **{k: v for k, v in t.items() if k != "created_by_run"}}
                 for tid, t in sorted(blog["terms"].items())]
        # Membership rows come back in (term_id, product_id) order, the order
        # the broker's keyset paging walks.
        products = sorted(
            ({"term_id": tid, "product_id": pid, "sku": p["sku"]}
             for pid, p in blog["products"].items() for tid in p["term_ids"]),
            key=lambda r: (r["term_id"], r["product_id"]),
        )
        uncategorized = {pid: sku for pid, sku in blog["uncategorized"].items()}
        for pid, p in blog["products"].items():
            if not p["term_ids"] and pid not in uncategorized:
                uncategorized[pid] = p["sku"]
        uncategorized_rows = [{"product_id": pid, "sku": sku}
                              for pid, sku in sorted(uncategorized.items())]
        return {
            "broker_version": BROKER_VERSION, "blog_id": blog_id,
            "blog_path": blog["path"],
            "export_generation": self.export_generation(blog_id),
            "terms": terms, "products": products, "products_total": len(products),
            "next_after": None, "uncategorized": uncategorized_rows,
            "uncategorized_total": len(uncategorized_rows),
            "uncategorized_truncated": False, "next_uncategorized_after": None,
            "site_options": dict(self.site_options) if blog_id == 1 else {},
        }

    def broker_export(self, env: str, path: str, method: str = "GET",
                      payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One /export PAGE, the way the broker answers one. Stands in for
        categories_service._broker so the engine's paging, its generation
        fence and the uncategorized cursor are all exercised."""

        assert path == "/export", path
        payload = dict(payload or {})
        blog_id = int(payload.get("blog_id") or 0)
        if blog_id not in self.blogs:
            raise categories_service.BrokerError("Unknown or archived blog.", 404)
        self.export_pages += 1
        if self.on_export_page is not None:
            self.on_export_page(self.export_pages)
        full = self.export(env, blog_id)
        generation = full["export_generation"]
        expected = str(payload.get("expected_generation") or "")
        if expected and expected != generation:
            raise categories_service.BrokerError(
                "arb_catmgr_export_moved: The live categories changed while the"
                " export was being read; re-import", 409)
        limit = int(payload.get("products_limit") or 0)
        if limit < 1 or limit > EXPORT_PRODUCTS_MAX:
            limit = EXPORT_PRODUCTS_MAX
        rows = full["products"]
        keyset = (payload.get("after_term_id") is not None
                  or payload.get("after_product_id") is not None)
        offset = max(0, int(payload.get("products_offset") or 0))
        if keyset:
            after = (int(payload.get("after_term_id") or 0),
                     int(payload.get("after_product_id") or 0))
            page = [r for r in rows
                    if (r["term_id"], r["product_id"]) > after][:limit]
        else:
            page = rows[offset:offset + limit]
        last = ({"term_id": page[-1]["term_id"], "product_id": page[-1]["product_id"]}
                if page else None)

        uncategorized: List[Dict[str, Any]] = []
        uncat_total = None
        uncat_truncated = False
        next_uncat = None
        uncat_paging = payload.get("after_uncategorized_id") is not None
        if uncat_paging or (payload.get("include_uncategorized")
                            and not keyset and offset == 0):
            after_uncat = int(payload.get("after_uncategorized_id") or 0)
            uncat_limit = self.uncategorized_page_size
            all_uncat = full["uncategorized"]
            uncat_total = len(all_uncat)
            uncategorized = [r for r in all_uncat
                             if r["product_id"] > after_uncat][:uncat_limit]
            uncat_truncated = uncat_total > len(uncategorized)
            if self.uncategorized_pageable and len(uncategorized) >= uncat_limit:
                next_uncat = uncategorized[-1]["product_id"]
        return {
            **full,
            "products": page,
            "products_offset": offset,
            "next_after": last if len(page) >= limit else None,
            "uncategorized": uncategorized,
            "uncategorized_total": uncat_total,
            "uncategorized_truncated": uncat_truncated,
            "next_uncategorized_after": next_uncat,
        }

    def status(self, env: str) -> Dict[str, Any]:
        base = {
            "broker_version": BROKER_VERSION, "freeze": True,
            "redirection_active": self.redirection_available,
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
            job = self.jobs.get(str(payload.get("key") or ""))
            if job is None:
                raise categories_service.BrokerError("No job with that key.", 404)
            return {"ok": True,
                    "job": {"key": job["key"], "phase": job["phase"],
                            "blog_id": job["blog_id"], "status": job["status"],
                            "heartbeat_age": 0, "stale": False, "progress": None},
                    "result": job["result"], "error": job["error"]}
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
        key = self.job_key(path, payload)
        stored = self.jobs.get(key) if key else None
        if stored is not None and stored["status"] == "done":
            # job_open replays a key that already converged instead of running
            # the phase twice.
            body = dict(stored["result"]) if isinstance(stored["result"], dict) else {"ok": True}
            body["job"] = {"key": key, "phase": stored["phase"], "status": "done",
                           "replayed": True}
            return body
        return self.record_job(path, payload,
                               handler(int(payload["blog_id"]), blog, payload))

    # ------------------------------------------------------------ durable jobs

    @staticmethod
    def job_phase(path: str, payload: Dict[str, Any]) -> str:
        phase = path.strip("/")
        if phase == "restore":
            return "restore-" + (str(payload.get("phase") or "") or "all")
        return phase

    def job_key(self, path: str, payload: Dict[str, Any]) -> Optional[str]:
        request_id = str(payload.get("request_id") or "")
        if not request_id:
            return None
        key = f"{request_id[:64]}:{self.job_phase(path, payload)}"
        page = payload.get("page")
        if page is not None and page != "":
            key += f":{int(page)}"
        return key

    def record_job(self, path: str, payload: Dict[str, Any], result: Any) -> Any:
        """File the durable row the broker keeps for one phase call. A body
        that reports ok=false is stored FAILED with its report, so a retry
        re-runs the phase instead of replaying the refusal for ever."""

        key = self.job_key(path, payload)
        if not key:
            return result
        failed = isinstance(result, dict) and result.get("ok") is False
        self.jobs[key] = {
            "key": key, "phase": self.job_phase(path, payload),
            "blog_id": int(payload.get("blog_id") or 0),
            "status": "failed" if failed else "done",
            "result": result,
            "error": (str(result.get("message") or "reported refusals")
                      if failed else None),
        }
        return result

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

    def _next_redirect_id(self):
        value = self.next_redirect_id
        self.next_redirect_id += 1
        return value

    def _redirect_prior(self, old_path):
        """One journal entry: what the rule at this URL was before finalize."""
        entry = {"old_path": old_path, "present": False,
                 "group_id": self.redirect_group_id}
        existing = self.redirects.get(old_path)
        if old_path and existing and self.redirection_available and self.redirect_group_id > 0:
            entry.update({
                "present": True, "id": int(existing.get("id") or 0),
                "url": old_path, "action_data": existing["new_path"],
                "action_code": int(existing["code"]), "action_type": "url",
                "status": "enabled" if existing["enabled"] else "disabled",
            })
        return entry

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
        prior: List[Dict[str, Any]] = []
        if blog_id == 1 and payload.get("redirects"):
            # What every planned path looked like BEFORE the first write: the
            # journal a restore puts back from.
            for r in payload["redirects"]:
                prior.append(self._redirect_prior(str(r.get("old_path") or "")))
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
                    existing.setdefault("id", self._next_redirect_id())
                    existing.update({"new_path": r["new_path"], "code": 301, "enabled": True})
                    created.append({"old_path": r["old_path"], "new_path": r["new_path"], "updated": True})
                    continue
                self.redirects[r["old_path"]] = {"id": self._next_redirect_id(),
                                                 "new_path": r["new_path"],
                                                 "code": 301, "enabled": True}
                created.append({"old_path": r["old_path"], "new_path": r["new_path"]})
        rewritten = 0
        if blog_id == 1:
            rewritten = self._rewrite_unspsc(payload.get("unspsc_renames") or {}, payload.get("unspsc_merges") or {})
        refused = sum(1 for r in report if r["status"] in ("slug_drift", "failed", "has_products"))
        return {"ok": refused == 0 and not failed, "deleted": deleted, "delete_report": report,
                "refused_deletes": refused, "recounted_terms": len(blog["terms"]),
                "es_processed": None, "redirects_created": created, "redirects_failed": failed,
                "redirects_prior": prior, "redirects_prior_count": len(prior),
                "unspsc_rewritten": rewritten}

    def _restore(self, blog_id, blog, payload):
        snapshot = payload.get("snapshot") or {}
        terms = snapshot.get("terms") or []
        products = snapshot.get("products") or []
        # Products that had NO category when the snapshot was taken, and the
        # journal of what finalize did to the Redirection rules.
        uncategorized = snapshot.get("uncategorized") or []
        snapshot_redirects = snapshot.get("redirects") or []
        phase = payload.get("phase") or ""
        do_terms = phase in ("", "terms")
        do_memberships = phase in ("", "memberships")
        do_finalize = phase in ("", "finalize")
        if do_memberships and len(products) + len(uncategorized) > RESTORE_ROWS_MAX:
            raise categories_service.BrokerError(
                f"Too many membership rows in one restore page (max"
                f" {RESTORE_ROWS_MAX}); page the snapshot.", 400)
        if len(snapshot_redirects) > RESTORE_REDIRECTS_MAX:
            raise categories_service.BrokerError(
                f"Too many redirect rows in one restore (max {RESTORE_REDIRECTS_MAX}).", 400)
        failures: List[Dict[str, Any]] = []
        id_to_slug = {int(t["term_id"]): t["slug"] for t in terms}
        slug_to_id: Dict[str, int] = {}
        created = updated = 0
        options_restored = 0
        if do_terms:
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
        if do_finalize:
            keep = set(slug_to_id)
            for tid in list(blog["terms"]):
                if blog["terms"][tid]["slug"] not in keep and blog["terms"][tid]["slug"] != "uncategorized":
                    parent = blog["terms"][tid]["parent"]
                    for child in blog["terms"].values():
                        if child["parent"] == tid:
                            child["parent"] = parent
                    del blog["terms"][tid]
                    removed += 1
        # Products the snapshot recorded as uncategorized are seeded with an
        # empty set FIRST, so one the run assigned to a category is cleared
        # again; a membership row for the same product overwrites that seed.
        by_product: Dict[int, List[int]] = {}
        skip_product: Set[int] = set()
        for u in uncategorized:
            pid = int((u.get("product_id") if isinstance(u, dict) else u) or 0)
            if pid >= 1:
                by_product[pid] = []
        for p in products:
            pid = int(p.get("product_id") or 0)
            if pid < 1:
                continue
            slug = id_to_slug.get(int(p.get("term_id") or 0))
            if slug is None or slug not in slug_to_id:
                # Writing a partial set here would strip the categories the
                # snapshot DID bring back: report it and leave the product be.
                failures.append({"product_id": pid, "term_id": p.get("term_id"),
                                 "step": "membership_term_missing"})
                by_product.pop(pid, None)
                skip_product.add(pid)
                continue
            if pid in skip_product:
                continue
            by_product.setdefault(pid, []).append(slug_to_id[slug])
        restored = 0
        if do_memberships:
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
        redirects_restored = 0
        redirects_removed = 0
        if do_finalize and blog_id == 1 and snapshot_redirects:
            for r in snapshot_redirects:
                if not isinstance(r, dict):
                    continue
                old_path = str(r.get("old_path") or "")
                group_id = int(r.get("group_id") or 0)
                if not self.redirection_available:
                    failures.append({"old_path": old_path, "step": "redirection_unavailable"})
                    continue
                if not old_path or group_id < 1:
                    continue          # finalize never wrote this row either
                if not r.get("present"):
                    # Nothing was here before the run: the rule now at this URL
                    # is the run's own and must go.
                    if self.redirects.pop(old_path, None) is not None:
                        redirects_removed += 1
                    if old_path in self.redirects:
                        failures.append({"old_path": old_path, "step": "redirect_delete",
                                         "error": "1 rule(s) still at this URL"})
                    continue
                action_data = str(r.get("action_data") or "")
                action_code = int(r.get("action_code") or 301)
                enabled = str(r.get("status") or "enabled") == "enabled"
                self.redirects[old_path] = {
                    "id": int(r.get("id") or 0) or self._next_redirect_id(),
                    "new_path": action_data, "code": action_code, "enabled": enabled,
                }
                check = self.redirects[old_path]
                if (check["new_path"] != action_data or check["code"] != action_code
                        or check["enabled"] != enabled):
                    failures.append({"old_path": old_path, "step": "redirect_restore",
                                     "error": "rule did not go back to the journaled state"})
                    continue
                redirects_restored += 1
        return {"ok": not failures, "phase": phase or "all", "terms": len(slug_to_id),
                "terms_expected": len(terms), "products_restored": restored,
                "products_expected": len(by_product), "terms_removed": removed,
                "options_restored": options_restored,
                "redirects_restored": redirects_restored,
                "redirects_removed": redirects_removed, "failures": failures}
