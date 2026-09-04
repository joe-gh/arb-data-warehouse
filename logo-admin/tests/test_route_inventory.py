"""Every HTTP route is explicitly classified by capability and side effect."""

from main import app


READ_ROUTES = {
    ("GET", "/api/stores"),
    ("GET", "/api/styles"),
    ("GET", "/api/style"),
    ("GET", "/api/styles/similar"),
    ("GET", "/api/styles/coverage"),
    ("GET", "/api/designs"),
    ("GET", "/api/designs/{design_id}"),
    ("GET", "/api/vocab"),
    ("GET", "/api/settings/{store}"),
    ("GET", "/api/sync-status"),
    ("GET", "/api/design-usage"),
    ("GET", "/api/import-report"),
    ("GET", "/api/audit-log"),
    ("GET", "/api/pricing/tiers"),
    ("GET", "/api/pricing/store-tiers"),
    ("GET", "/api/logo-ownership"),
    ("GET", "/api/logo-ownership/preview"),
    ("GET", "/api/price-rules/check"),
    ("GET", "/api/stock-overrides"),
    ("GET", "/api/stock-overrides/brands"),
    ("GET", "/api/categories/targets"),
    ("GET", "/api/categories/snapshots"),
    ("GET", "/api/categories/blogs"),
    ("GET", "/api/categories/wp-status"),
    ("GET", "/api/categories/readiness"),
    ("GET", "/api/categories/tree"),
    ("GET", "/api/categories/tree/effective"),
    ("GET", "/api/categories/overrides"),
    ("GET", "/api/categories/mapping"),
    ("GET", "/api/categories/mapping/suggest"),
    ("GET", "/api/categories/rules"),
    ("GET", "/api/categories/assignments"),
    ("GET", "/api/categories/membership"),
    ("GET", "/api/categories/preview/blog"),
    ("GET", "/api/categories/uncategorized-ack"),
    ("GET", "/api/categories/runs"),
    ("GET", "/api/categories/runs/{run_id}"),
    ("GET", "/api/logo-names"),
    ("GET", "/api/colors"),
    ("GET", "/api/bulk-apply/batches"),
    ("GET", "/api/sync-blocks"),
    ("GET", "/api/price-rules"),
    ("GET", "/api/price-rules/dimensions"),
    ("GET", "/api/product-mix/stores"),
    ("GET", "/api/health/overview"),
    ("GET", "/api/product-mix"),
    ("GET", "/api/product-mix/style"),
}

TRANSACTIONAL_WRITES = {
    ("PUT", "/api/settings/{store}"),
    ("PUT", "/api/settings/{store}/extra-customers"),
    ("POST", "/api/assignments/logo-cost"),
    ("PUT", "/api/default-costs"),
    ("PUT", "/api/assignments"),
    ("DELETE", "/api/assignments"),
    ("DELETE", "/api/assignments-by-color"),
    ("POST", "/api/style-active"),
    ("POST", "/api/apply-all-colors"),
    ("POST", "/api/copy-style"),
    ("PUT", "/api/pricing/store-tier"),
    ("DELETE", "/api/pricing/store-tier"),
    ("PUT", "/api/logo-names"),
    ("POST", "/api/logo-names/repull"),
    ("PUT", "/api/colors"),
    ("POST", "/api/bulk-apply/execute"),
    ("POST", "/api/bulk-apply/undo"),
    ("POST", "/api/design-swap"),
    ("POST", "/api/assignments/reorder"),
    ("PUT", "/api/style-color-order"),
    ("POST", "/api/assignments/paste"),
    ("POST", "/api/assignments/paste-batch"),
    ("POST", "/api/style-active-batch"),
    ("POST", "/api/copy-style-batch"),
    ("POST", "/api/styles/fill-gaps"),
    ("PUT", "/api/sync-blocks"),
    ("PUT", "/api/sync-blocks/toggle"),
    ("DELETE", "/api/sync-blocks"),
    ("PUT", "/api/price-rules"),
    ("PUT", "/api/price-rules/toggle"),
    ("DELETE", "/api/price-rules"),
    ("PUT", "/api/product-mix/stores"),
    ("PUT", "/api/product-mix/stores/mode"),
    ("DELETE", "/api/product-mix/stores"),
    ("PUT", "/api/product-mix/style"),
    ("PUT", "/api/product-mix"),
    ("DELETE", "/api/product-mix"),
    ("POST", "/api/product-mix/import"),
    ("PUT", "/api/product-mix/external"),
    ("DELETE", "/api/product-mix/external"),
    ("PUT", "/api/stock-overrides"),
    ("PUT", "/api/stock-overrides/toggle"),
    ("PUT", "/api/stock-overrides/brands"),
    ("DELETE", "/api/stock-overrides"),
    ("DELETE", "/api/stock-overrides/brands"),
    ("POST", "/api/categories/nodes"),
    ("PUT", "/api/categories/nodes/{node_id}"),
    ("POST", "/api/categories/nodes/{node_id}/move"),
    ("DELETE", "/api/categories/nodes/{node_id}"),
    ("POST", "/api/categories/draft/seed"),
    ("PUT", "/api/categories/overrides"),
    ("DELETE", "/api/categories/overrides/{override_id}"),
    ("PUT", "/api/categories/mapping"),
    ("DELETE", "/api/categories/mapping/{old_slug}"),
    ("PUT", "/api/categories/rules"),
    ("DELETE", "/api/categories/rules/{rule_id}"),
    ("PUT", "/api/categories/assignments"),
    ("DELETE", "/api/categories/assignments/{assignment_id}"),
    ("PUT", "/api/categories/uncategorized-ack"),
    ("DELETE", "/api/categories/uncategorized-ack/{sku}"),
    ("POST", "/api/categories/runs/{run_id}/pause"),
    ("POST", "/api/categories/runs/{run_id}/cancel"),
    ("POST", "/api/categories/runs/{run_id}/jobs/{job_id}/skip"),
}

NONTRANSACTIONAL_OR_EXPORT = {
    ("GET", "/api/export"),
    ("POST", "/api/import"),
    ("GET", "/api/product-link"),
    ("GET", "/api/audit-log/export"),
    ("POST", "/api/upload"),
    ("POST", "/api/sync"),
    ("POST", "/api/legacy-import"),
    ("POST", "/api/legacy-import-images"),
    ("POST", "/api/bulk-apply/preview"),
    ("POST", "/api/design-swap/preview"),
    ("POST", "/api/copy-style-batch/preview"),
    ("POST", "/api/styles/fill-gaps/preview"),
    ("POST", "/api/price-rules/preview"),
    ("POST", "/api/product-mix/preview"),
    ("GET", "/api/import-report/export"),
    ("POST", "/api/logo-ownership"),
    ("POST", "/api/categories/snapshots/import"),
    ("POST", "/api/categories/rules/evaluate"),
    ("GET", "/api/categories/assignments/export"),
    ("POST", "/api/categories/assignments/import"),
    ("POST", "/api/categories/preview"),
    ("POST", "/api/categories/runs"),
    ("POST", "/api/categories/runs/{run_id}/start"),
    ("POST", "/api/categories/runs/{run_id}/resume"),
    ("POST", "/api/categories/runs/{run_id}/jobs/{job_id}/retry"),
    ("POST", "/api/categories/runs/{run_id}/jobs/{job_id}/restore"),
    ("POST", "/api/categories/freeze"),
    ("POST", "/api/categories/drift-audit"),
}

AGENT_ROUTES = {
    ("POST", "/api/agent/sessions"),
    ("GET", "/api/agent/sessions"),
    ("GET", "/api/agent/sessions/{session_id}"),
    ("POST", "/api/agent/chat"),
    ("GET", "/api/agent/change-sets/{change_set_id}"),
    ("POST", "/api/agent/change-sets/{change_set_id}/apply"),
    ("POST", "/api/agent/change-sets/{change_set_id}/discard"),
    ("POST", "/api/agent/change-sets/{change_set_id}/undo"),
    ("POST", "/api/agent/spreadsheets"),
    ("GET", "/api/agent/spreadsheets/{job_id}"),
    ("POST", "/api/agent/spreadsheets/{job_id}/confirm-mapping"),
}


# Machine feed endpoints (/feed/*): bearer-token consumers, no session/CSRF,
# exposed by nginx without the operator IP allowlist. Any new /feed route must
# be reviewed and classified here.
MACHINE_FEED_ROUTES = {
    ("GET", "/feed/version"),
    ("GET", "/feed/products"),
    ("GET", "/feed/logos"),
    ("GET", "/feed/stores"),
    ("GET", "/feed/categories"),
}


def _actual_routes(prefix):
    actual = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(prefix):
            continue
        for method in getattr(route, "methods", set()):
            if method not in {"HEAD", "OPTIONS"}:
                actual.add((method, path))
    return actual


def _actual_api_routes():
    return _actual_routes("/api/")


def test_every_feed_route_is_classified_as_machine_only():
    assert _actual_routes("/feed/") == MACHINE_FEED_ROUTES
    assert MACHINE_FEED_ROUTES.isdisjoint(READ_ROUTES)
    assert MACHINE_FEED_ROUTES.isdisjoint(TRANSACTIONAL_WRITES)


def test_every_api_route_has_exactly_one_classification():
    groups = [
        READ_ROUTES,
        TRANSACTIONAL_WRITES,
        NONTRANSACTIONAL_OR_EXPORT,
        AGENT_ROUTES,
    ]
    all_classified = set().union(*groups)
    assert sum(len(group) for group in groups) == len(all_classified)
    assert _actual_api_routes() == all_classified


def test_agent_lifecycle_routes_are_never_legacy_transactional_routes():
    assert AGENT_ROUTES.isdisjoint(TRANSACTIONAL_WRITES)
    assert AGENT_ROUTES.isdisjoint(NONTRANSACTIONAL_OR_EXPORT)
