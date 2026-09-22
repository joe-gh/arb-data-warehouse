"""The Help page's assistant catalog may only name tools that exist."""

import re
from pathlib import Path

import tool_registry

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"


def test_help_names_logo_images_and_assistant_sections():
    html = TEMPLATE.read_text()
    assert 'id="help-images"' in html
    assert 'id="help-assistant"' in html
    assert 'href="#help-images"' in html and 'href="#help-assistant"' in html


def test_every_catalogued_tool_exists():
    html = TEMPLATE.read_text()
    named = set(re.findall(r'data-tool="([a-z0-9_]+)"', html))
    assert len(named) >= 60, "catalog looks empty"
    known = set(tool_registry.APPROVED_AGENT_READ_NAMES) | set(tool_registry.APPROVED_AGENT_WRITE_NAMES)
    missing = sorted(named - known)
    assert not missing, f"Help names tools that do not exist: {missing}"
