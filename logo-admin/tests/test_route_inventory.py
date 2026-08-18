"""Every HTTP route is explicitly classified by capability and side effect."""

from main import app


READ_ROUTES = {
    ("GET", "/api/stores"),
    ("GET", "/api/styles"),
    ("GET", "/api/style"),
    ("GET", "/api/designs"),
    ("GET", "/api/designs/{design_id}"),
    ("GET", "/api/vocab"),
    ("GET", "/api/settings/{store}"),
    ("GET", "/api/import-report"),
    ("GET", "/api/audit-log"),
    ("GET", "/api/pricing/tiers"),
    ("GET", "/api/pricing/store-tiers"),
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
    ("POST", "/api/price-rules/preview"),
    ("POST", "/api/product-mix/preview"),
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
