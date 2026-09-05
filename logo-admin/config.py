"""Environment-backed configuration for the Logo Admin service."""

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
from types import MappingProxyType
from typing import FrozenSet, Mapping, Optional
from urllib.parse import urlsplit


# Absolute cap on one issued session cookie. Sessions slide: an active
# operator gets a fresh cookie once an hour (auth.SESSION_RENEW_AFTER_SECONDS),
# up to auth.SESSION_ABSOLUTE_MAX_SECONDS since sign-in.
MAX_SESSION_SECONDS = 24 * 60 * 60


class ConfigurationError(RuntimeError):
    """Raised when a required deployment setting is absent or unsafe."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = os.environ.get(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return parsed


def _user_allowlist(name: str = "AGENT_ALLOWED_USERS") -> FrozenSet[str]:
    """Parse a case-insensitive, comma-separated WordPress login allowlist."""

    return frozenset(
        login.strip().lower()
        for login in os.environ.get(name, "").split(",")
        if login.strip()
    )


def _https_url(name: str) -> str:
    value = _required(name)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigurationError(f"{name} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            f"{name} must not contain credentials, a query, or a fragment"
        )
    try:
        parsed.port
    except ValueError:
        raise ConfigurationError(f"{name} contains an invalid port") from None
    return value


def _media_base() -> str:
    # Public base URL where uploaded/mirrored logo images are served. With the
    # media-box publisher (infra/publish-logo-media.sh) this points at the
    # media server (e.g. https://media.arborwear.com/images/logos/warehouse/);
    # a trailing-slash app-local /logo-media/ path also works if the app is
    # ever exposed directly.
    value = _required("MEDIA_BASE")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("MEDIA_BASE must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "MEDIA_BASE must not contain credentials, a query, or a fragment"
        )
    if not parsed.path.endswith("/"):
        raise ConfigurationError("MEDIA_BASE must end with a trailing slash")
    try:
        parsed.port
    except ValueError:
        raise ConfigurationError("MEDIA_BASE contains an invalid port") from None
    return value


def _art_base() -> str:
    value = os.environ.get(
        "FDM4_ART_BASE", "https://media.arborwear.com/images/logos/"
    ).strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("FDM4_ART_BASE must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "FDM4_ART_BASE must not contain credentials, a query, or a fragment"
        )
    try:
        parsed.port
    except ValueError:
        raise ConfigurationError("FDM4_ART_BASE contains an invalid port") from None
    return value.rstrip("/") + "/"


@dataclass(frozen=True)
class CatmgrTarget:
    """One category-editor WordPress environment (page-scoped, unlike WP_SYNC_URL)."""

    env: str
    base_url: str  # e.g. https://arb-dev.arborwear.com/wp-json/arb/v1/logo-admin/categories
    user: str
    app_password: str

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc


CATMGR_ENVS = ("dev", "prod")


def _catmgr_targets(enabled: bool) -> Mapping[str, CatmgrTarget]:
    """Parse CATMGR_DEV_URL/_USER/_APP_PASSWORD and CATMGR_PROD_* triples.

    An environment exists iff its URL is set; a URL without complete
    credentials is a deployment mistake and fails even when the feature is
    disabled, so a half-finished env file never ships dark and broken.
    """

    targets = {}
    for env in CATMGR_ENVS:
        prefix = f"CATMGR_{env.upper()}"
        url = os.environ.get(f"{prefix}_URL", "").strip()
        user = os.environ.get(f"{prefix}_USER", "").strip()
        app_password = os.environ.get(f"{prefix}_APP_PASSWORD", "").strip()
        if not url:
            if user or app_password:
                raise ConfigurationError(
                    f"{prefix}_USER/_APP_PASSWORD are set but {prefix}_URL is missing"
                )
            continue
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ConfigurationError(f"{prefix}_URL must be an absolute HTTPS URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigurationError(
                f"{prefix}_URL must not contain credentials, a query, or a fragment"
            )
        try:
            parsed.port
        except ValueError:
            raise ConfigurationError(f"{prefix}_URL contains an invalid port") from None
        if not user or not app_password:
            raise ConfigurationError(
                f"{prefix}_URL requires both {prefix}_USER and {prefix}_APP_PASSWORD"
            )
        targets[env] = CatmgrTarget(
            env=env,
            base_url=url.rstrip("/"),
            user=user,
            app_password=app_password,
        )
    if enabled and not targets:
        raise ConfigurationError(
            "CATMGR_ENABLED requires at least one CATMGR_DEV_URL / CATMGR_PROD_URL target"
        )
    return MappingProxyType(targets)


@dataclass(frozen=True)
class Settings:
    database_dsn: str
    session_secret: str
    session_cookie_secure: bool
    wp_auth_url: str
    wp_sync_url: str
    wp_sync_user: str
    wp_sync_app_password: str
    wp_http_timeout: int
    wp_sync_timeout: int
    upload_dir: Path
    media_base: str
    fdm4_art_base: str
    max_upload_bytes: int
    agent_enabled: bool
    agent_writes_enabled: bool
    agent_repull_function_sha256: Optional[str]
    agent_allowed_users: FrozenSet[str]
    # Optional subset of the approved write tools the assistant may stage.
    # Empty = all approved writes (when AGENT_WRITES_ENABLED). Lets a pilot
    # switch mutations on one tool at a time.
    agent_write_tools: FrozenSet[str]
    openai_api_key: Optional[str]
    openai_model: Optional[str]
    agent_daily_token_cap: int
    agent_monthly_token_cap: int
    agent_requests_per_minute: int
    agent_max_input_chars: int
    agent_max_output_tokens: int
    agent_max_tool_calls: int
    agent_max_tool_result_bytes: int
    agent_max_turn_replay_bytes: int
    agent_max_concurrent_turns: int
    agent_max_change_set_items: int
    agent_turn_timeout_seconds: int
    agent_chat_retention_days: int
    agent_upload_dir: Path
    agent_max_spreadsheet_bytes: int
    agent_max_spreadsheet_rows: int
    agent_max_spreadsheet_columns: int
    agent_max_cell_chars: int
    agent_max_xlsx_entries: int
    agent_max_xlsx_uncompressed_bytes: int
    catmgr_enabled: bool
    catmgr_targets: Mapping[str, CatmgrTarget]
    catmgr_wp_timeout: int
    catmgr_apply_users: FrozenSet[str]
    catmgr_view_users: FrozenSet[str]
    session_cookie_name: str = "arb_logo_admin_session"
    session_max_age: int = MAX_SESSION_SECONDS
    db_pool_min: int = 1
    db_pool_max: int = 8


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process."""

    session_secret = _required("SESSION_SECRET")
    if len(session_secret) < 32:
        raise ConfigurationError("SESSION_SECRET must be at least 32 characters")

    try:
        upload_dir = Path(_required("UPLOAD_DIR")).expanduser().resolve(
            strict=False
        )
    except OSError as exc:
        raise ConfigurationError("UPLOAD_DIR cannot be resolved safely") from exc
    if not upload_dir.is_absolute() or upload_dir == Path("/"):
        raise ConfigurationError("UPLOAD_DIR must be an absolute non-root path")

    try:
        agent_upload_dir = Path(
            os.environ.get(
                "AGENT_UPLOAD_DIR",
                "/var/lib/arb-logo-admin/agent-uploads",
            ).strip()
        ).expanduser().resolve(strict=False)
    except OSError as exc:
        raise ConfigurationError(
            "AGENT_UPLOAD_DIR cannot be resolved safely"
        ) from exc
    if not agent_upload_dir.is_absolute() or agent_upload_dir == Path("/"):
        raise ConfigurationError(
            "AGENT_UPLOAD_DIR must be an absolute non-root path"
        )
    if agent_upload_dir == upload_dir or agent_upload_dir.is_relative_to(upload_dir):
        raise ConfigurationError(
            "AGENT_UPLOAD_DIR must not be served from UPLOAD_DIR"
        )

    pool_min = _integer("DATABASE_POOL_MIN", 1, 1, 20)
    pool_max = _integer("DATABASE_POOL_MAX", 8, pool_min, 50)

    agent_enabled = _boolean("AGENT_ENABLED", False)
    agent_writes_enabled = _boolean("AGENT_WRITES_ENABLED", False)
    catmgr_enabled = _boolean("CATMGR_ENABLED", False)
    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip() or None
    openai_model = os.environ.get("OPENAI_MODEL", "").strip() or None
    agent_repull_function_sha256 = os.environ.get(
        "AGENT_REPULL_FUNCTION_SHA256", ""
    ).strip().lower() or None
    if agent_repull_function_sha256 is not None and (
        len(agent_repull_function_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in agent_repull_function_sha256
        )
    ):
        raise ConfigurationError(
            "AGENT_REPULL_FUNCTION_SHA256 must be 64 hexadecimal characters"
        )
    if agent_writes_enabled and not agent_enabled:
        raise ConfigurationError(
            "AGENT_WRITES_ENABLED requires AGENT_ENABLED"
        )
    if agent_enabled and not openai_api_key:
        raise ConfigurationError(
            "OPENAI_API_KEY is required when AGENT_ENABLED=true"
        )
    if agent_enabled and not openai_model:
        raise ConfigurationError(
            "OPENAI_MODEL is required when AGENT_ENABLED=true"
        )

    agent_daily_token_cap = _integer(
        "AGENT_DAILY_TOKEN_CAP", 100_000, 2_048, 1_000_000_000
    )
    agent_monthly_token_cap = _integer(
        "AGENT_MONTHLY_TOKEN_CAP", 2_000_000, 2_048, 10_000_000_000
    )
    if agent_monthly_token_cap < agent_daily_token_cap:
        raise ConfigurationError(
            "AGENT_MONTHLY_TOKEN_CAP must be at least AGENT_DAILY_TOKEN_CAP"
        )
    agent_max_concurrent_turns = _integer(
        "AGENT_MAX_CONCURRENT_TURNS", 4, 1, 32
    )
    if agent_max_concurrent_turns > pool_max:
        raise ConfigurationError(
            "AGENT_MAX_CONCURRENT_TURNS must not exceed DATABASE_POOL_MAX"
        )
    agent_max_tool_calls = _integer(
        "AGENT_MAX_TOOL_CALLS", 12, 1, 100
    )
    agent_max_tool_result_bytes = _integer(
        "AGENT_MAX_TOOL_RESULT_BYTES", 100_000, 1_024, 2 * 1024 * 1024
    )
    agent_max_turn_replay_bytes = _integer(
        "AGENT_MAX_TURN_REPLAY_BYTES", 1_000_000, 64 * 1024, 2 * 1024 * 1024
    )
    if agent_max_tool_result_bytes > agent_max_turn_replay_bytes:
        raise ConfigurationError(
            "AGENT_MAX_TOOL_RESULT_BYTES must not exceed "
            "AGENT_MAX_TURN_REPLAY_BYTES"
        )

    return Settings(
        database_dsn=_required("DATABASE_DSN"),
        session_secret=session_secret,
        session_cookie_secure=_boolean("SESSION_COOKIE_SECURE", True),
        wp_auth_url=_https_url("WP_AUTH_URL"),
        wp_sync_url=_https_url("WP_SYNC_URL"),
        wp_sync_user=_required("WP_SYNC_USER"),
        wp_sync_app_password=_required("WP_SYNC_APP_PASSWORD"),
        wp_http_timeout=_integer("WP_HTTP_TIMEOUT", 15, 1, 120),
        wp_sync_timeout=_integer("WP_SYNC_TIMEOUT", 120, 5, 900),
        upload_dir=upload_dir,
        media_base=_media_base(),
        fdm4_art_base=_art_base(),
        max_upload_bytes=_integer(
            "MAX_UPLOAD_BYTES", 5 * 1024 * 1024, 1024, 50 * 1024 * 1024
        ),
        agent_enabled=agent_enabled,
        agent_writes_enabled=agent_writes_enabled,
        agent_repull_function_sha256=agent_repull_function_sha256,
        agent_allowed_users=_user_allowlist(),
        agent_write_tools=_user_allowlist("AGENT_WRITE_TOOLS"),
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        agent_daily_token_cap=agent_daily_token_cap,
        agent_monthly_token_cap=agent_monthly_token_cap,
        agent_requests_per_minute=_integer(
            "AGENT_REQUESTS_PER_MINUTE", 10, 1, 600
        ),
        agent_max_input_chars=_integer(
            "AGENT_MAX_INPUT_CHARS", 8_000, 100, 100_000
        ),
        agent_max_output_tokens=_integer(
            "AGENT_MAX_OUTPUT_TOKENS", 2_048, 64, 32_768
        ),
        agent_max_tool_calls=agent_max_tool_calls,
        agent_max_tool_result_bytes=agent_max_tool_result_bytes,
        agent_max_turn_replay_bytes=agent_max_turn_replay_bytes,
        agent_max_concurrent_turns=agent_max_concurrent_turns,
        agent_max_change_set_items=_integer(
            "AGENT_MAX_CHANGE_SET_ITEMS", 50, 1, 500
        ),
        agent_turn_timeout_seconds=_integer(
            "AGENT_TURN_TIMEOUT_SECONDS", 90, 10, 600
        ),
        agent_chat_retention_days=_integer(
            "AGENT_CHAT_RETENTION_DAYS", 30, 1, 365
        ),
        agent_upload_dir=agent_upload_dir,
        agent_max_spreadsheet_bytes=_integer(
            "AGENT_MAX_SPREADSHEET_BYTES",
            5 * 1024 * 1024,
            1024,
            25 * 1024 * 1024,
        ),
        agent_max_spreadsheet_rows=_integer(
            "AGENT_MAX_SPREADSHEET_ROWS", 500, 1, 2000
        ),
        agent_max_spreadsheet_columns=_integer(
            "AGENT_MAX_SPREADSHEET_COLUMNS", 40, 1, 100
        ),
        agent_max_cell_chars=_integer(
            "AGENT_MAX_CELL_CHARS", 2_000, 1, 20_000
        ),
        agent_max_xlsx_entries=_integer(
            "AGENT_MAX_XLSX_ENTRIES", 200, 10, 2_000
        ),
        agent_max_xlsx_uncompressed_bytes=_integer(
            "AGENT_MAX_XLSX_UNCOMPRESSED_BYTES",
            50 * 1024 * 1024,
            1024,
            250 * 1024 * 1024,
        ),
        catmgr_enabled=catmgr_enabled,
        catmgr_targets=_catmgr_targets(catmgr_enabled),
        catmgr_wp_timeout=_integer("CATMGR_WP_TIMEOUT", 120, 5, 900),
        catmgr_apply_users=_user_allowlist("CATMGR_APPLY_USERS"),
        catmgr_view_users=_user_allowlist("CATMGR_VIEW_USERS"),
        session_max_age=_integer(
            "SESSION_TTL_SECONDS", MAX_SESSION_SECONDS, 300, MAX_SESSION_SECONDS
        ),
        db_pool_min=pool_min,
        db_pool_max=pool_max,
    )
