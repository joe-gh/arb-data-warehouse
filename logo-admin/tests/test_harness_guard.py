import pytest

from tests.harness_guard import SAFE_LOGO_ADMIN_FLAGS, validate_test_target


NONCE = "0123456789abcdef0123456789abcdef"
DATABASE = "arb_warehouse_test_agent_20260717T120000Z_01234567"


def _validate(
    database_name=DATABASE,
    app_role="logo_admin",
    app_session_role="logo_admin",
    admin_database_name=DATABASE,
    admin_role="postgres",
    **changes,
):
    options = {
        "app_role_flags": SAFE_LOGO_ADMIN_FLAGS,
        "app_membership_count": 0,
        "marker_database_name": database_name,
        "marker_nonce": NONCE,
    }
    options.update(changes)
    return validate_test_target(
        database_name,
        app_role,
        app_session_role,
        admin_database_name,
        admin_role,
        **options,
    )


def test_accepts_randomized_test_database():
    _validate()


@pytest.mark.parametrize("database_name", [
    "arb_warehouse",
    "arb_warehouse_test",
    "production",
    "arb_warehouse_test_bad-name",
])
def test_rejects_non_disposable_name(database_name):
    with pytest.raises(RuntimeError, match="refusing non-test database"):
        _validate(
            database_name=database_name,
            admin_database_name=database_name,
            marker_database_name=database_name,
        )


def test_rejects_wrong_application_role():
    with pytest.raises(RuntimeError, match="must use logo_admin"):
        _validate(app_role="postgres")


def test_rejects_set_role_application_session():
    with pytest.raises(RuntimeError, match="session_user must be logo_admin"):
        _validate(app_session_role="postgres")


def test_rejects_database_mismatch():
    with pytest.raises(RuntimeError, match="different databases"):
        _validate(admin_database_name="arb_warehouse_test_other_01234567")


def test_rejects_logo_admin_as_reset_role():
    with pytest.raises(RuntimeError, match="reset DSN"):
        _validate(admin_role="logo_admin")


def test_rejects_unsafe_role_flags_or_memberships():
    unsafe = dict(SAFE_LOGO_ADMIN_FLAGS, superuser=True)
    with pytest.raises(RuntimeError, match="role flags"):
        _validate(app_role_flags=unsafe)
    with pytest.raises(RuntimeError, match="memberships"):
        _validate(app_membership_count=1)


def test_rejects_missing_or_unrelated_disposable_marker():
    with pytest.raises(RuntimeError, match="marker is missing"):
        _validate(marker_database_name="")
    with pytest.raises(RuntimeError, match="random marker"):
        _validate(marker_nonce="f" * 32)
