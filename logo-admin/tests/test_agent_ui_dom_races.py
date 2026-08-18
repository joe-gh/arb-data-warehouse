"""Run dependency-free Node DOM/race simulations against the UI guard."""

from pathlib import Path
import subprocess


SCRIPT = Path(__file__).with_name("browser") / "test_agent_ui_races.mjs"


def test_deferred_session_history_sse_and_card_races():
    result = subprocess.run(
        ["node", "--test", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-4_000:]
