"""First-boot provisioning must not trace the passwords it generates.

cloud-init.sh runs under `set -euxo pipefail`, so every traced line is copied
to /var/log/cloud-init-output.log (group adm) and the serial console. The
script is far too entangled with apt, systemd and postgres to execute here, so
this reads it and checks the xtrace state at every line that handles a
generated password.
"""

import re

from tests.infra.fakes import INFRA_DIR

CLOUD_INIT = INFRA_DIR / "cloud-init.sh"

ASSIGNMENT = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*_PW=")


def _trace_states():
    """Yield (line number, line, xtrace on?) for the whole script."""
    tracing = False
    for number, line in enumerate(CLOUD_INIT.read_text().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("set ") or stripped == "set":
            for token in stripped.split()[1:]:
                if token.startswith("-") and "x" in token:
                    tracing = True
                elif token.startswith("+") and "x" in token:
                    tracing = False
        yield number, line, tracing


def test_generated_passwords_are_never_traced():
    states = list(_trace_states())
    assignments = [(n, line) for n, line, _ in states if ASSIGNMENT.match(line)]
    assert assignments, "no *_PW= assignments found; has the script been renamed?"

    traced = [(n, line) for n, line, tracing in states if tracing and "_PW" in line]
    assert traced == [], f"password handling under xtrace at lines {[n for n, _ in traced]}"


def test_tracing_is_restored_after_the_credentials_file_is_locked_down():
    states = list(_trace_states())
    chmod = [n for n, line, _ in states if "chmod 600 /root/arb_warehouse_credentials.txt" in line]
    assert chmod, "the credentials file is no longer locked down"
    after = [tracing for n, _line, tracing in states if n > chmod[-1]]
    assert after and after[-1], "xtrace is never turned back on after the credentials are written"
