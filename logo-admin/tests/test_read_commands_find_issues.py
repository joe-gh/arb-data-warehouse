"""FindIssuesCommand.checks must accept every issue check name, including
design_conflicts (added alongside wordpress_mismatch as a default check),
and still reject bogus names."""

import pytest
from pydantic import ValidationError

from read_commands import FindIssuesCommand


ALL_CHECK_NAMES = [
    "no_logos",
    "colors_unclassified",
    "rules_expiring",
    "stores_frozen",
    "stock_overrides_stale",
    "uncategorized_products",
    "design_conflicts",
    "wordpress_mismatch",
]


def test_design_conflicts_check_name_validates():
    command = FindIssuesCommand(checks=["design_conflicts"])
    assert command.checks == ["design_conflicts"]


def test_all_eight_check_names_validate_together():
    command = FindIssuesCommand(checks=ALL_CHECK_NAMES)
    assert command.checks == ALL_CHECK_NAMES


def test_a_ninth_bogus_check_name_is_rejected():
    with pytest.raises(ValidationError):
        FindIssuesCommand(checks=ALL_CHECK_NAMES + ["not_a_real_check"])
