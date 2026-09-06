"""The curated category import stages per invocation and never claims success
after a remote step failed.

ssh and scp are stubbed on PATH: nothing leaves this machine.
"""

import os
import re
import subprocess

import pytest

from tests.infra.fakes import INFRA_DIR

SCRIPT = INFRA_DIR / "import-curated-categories.sh"

SSH_STUB = r"""#!/bin/bash
printf 'SSH ARGV: %s\n' "$*" >> "$STUB_LOG"
payload="$(cat)"
if [ -n "$payload" ]; then printf 'SSH STDIN: %s\n' "$payload" >> "$STUB_LOG"; fi
all="$* $payload"
case "$all" in
  *"mktemp -d /tmp/arb-curated-export."*)
    n=$(cat "$STUB_COUNTER" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$STUB_COUNTER"
    echo "/tmp/arb-curated-export.stub$n"; exit 0 ;;
  *"mktemp -d /tmp/arb-curated-load."*)
    n=$(cat "$STUB_COUNTER" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$STUB_COUNTER"
    echo "/tmp/arb-curated-load.stub$n"; exit 0 ;;
esac
if [ -n "${STUB_FAIL_MATCH:-}" ]; then
  case "$all" in *"$STUB_FAIL_MATCH"*) exit 1 ;; esac
fi
exit 0
"""

SCP_STUB = r"""#!/bin/bash
printf 'SCP ARGV: %s\n' "$*" >> "$STUB_LOG"
dest="${@: -1}"
dest="${dest%/}"
if [ -d "$dest" ]; then
  if [ "${STUB_EMPTY_EXPORT:-}" = "1" ]; then
    : > "$dest/curated_categories.tsv"
  else
    printf '1\t2\tslug\tName\t\t0\tslug\t0\t0\n' > "$dest/curated_categories.tsv"
  fi
  printf '1\t2\tSKU1\t3\n' > "$dest/curated_category_products.tsv"
fi
exit 0
"""

REMOTE_PATH = re.compile(r"/tmp/arb-curated-(?:export|load)\.stub\d+")


@pytest.fixture
def sandbox(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in (("ssh", SSH_STUB), ("scp", SCP_STUB)):
        path = binaries / name
        path.write_text(body)
        path.chmod(0o755)
    counter = tmp_path / "counter"
    counter.write_text("0")
    return binaries, counter


def run_import(sandbox, tmp_path, name, blog="1", fail_match=None, empty_export=False):
    binaries, counter = sandbox
    log = tmp_path / f"{name}.log"
    log.write_text("")
    environment = dict(os.environ)
    environment.update({
        "PATH": f"{binaries}:{environment['PATH']}",
        "STUB_LOG": str(log),
        "STUB_COUNTER": str(counter),
    })
    if fail_match:
        environment["STUB_FAIL_MATCH"] = fail_match
    if empty_export:
        environment["STUB_EMPTY_EXPORT"] = "1"
    result = subprocess.run(
        [str(SCRIPT), blog],
        env=environment, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=60,
    )
    return result, log.read_text()


def test_two_invocations_never_share_a_remote_path(sandbox, tmp_path):
    first, first_log = run_import(sandbox, tmp_path, "first", blog="1")
    second, second_log = run_import(sandbox, tmp_path, "second", blog="2")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "== done" in first.stdout and "== done" in second.stdout

    first_paths = set(REMOTE_PATH.findall(first_log))
    second_paths = set(REMOTE_PATH.findall(second_log))
    assert first_paths and second_paths
    assert first_paths.isdisjoint(second_paths)

    # The fixed staging filenames the two blogs used to fight over are gone.
    for log in (first_log, second_log):
        assert "/tmp/curated_categories.tsv" not in log
        assert "/tmp/curated_category_products.tsv" not in log
        assert "/tmp/ecc.php" not in log


def test_a_failed_export_fails_the_wrapper(sandbox, tmp_path):
    result, log = run_import(sandbox, tmp_path, "export-fail", fail_match="wp eval-file")

    assert result.returncode != 0
    assert "== done" not in result.stdout
    # The load must never have started.
    assert "arb-curated-load" not in log


def test_a_failed_load_fails_the_wrapper(sandbox, tmp_path):
    result, log = run_import(sandbox, tmp_path, "load-fail", fail_match="sudo -u postgres psql")

    assert result.returncode != 0
    assert "== done" not in result.stdout
    assert "arb-curated-load" in log


def test_an_empty_export_never_replaces_the_stored_tree(sandbox, tmp_path):
    result, log = run_import(sandbox, tmp_path, "empty", empty_export=True)

    assert result.returncode != 0
    assert "not replacing" in result.stderr
    assert "arb-curated-load" not in log
    assert "DELETE FROM curated.category" not in log


def test_the_remote_work_fails_fast_and_loads_atomically(sandbox, tmp_path):
    result, log = run_import(sandbox, tmp_path, "shape")

    assert result.returncode == 0
    assert log.count("set -euo pipefail") >= 2
    assert "trap 'rc=$?;" in log
    # A no-match from the deprecation filter must not look like a failure.
    assert "grep -v Deprecated || true" in log
    # One transaction for the whole replace, not four independent statements.
    assert "--single-transaction" in log
    assert "-c 'BEGIN'" not in log


def test_a_non_numeric_blog_id_is_refused(sandbox, tmp_path):
    result, _log = run_import(sandbox, tmp_path, "bad-blog", blog="1; DROP TABLE curated.category")

    assert result.returncode == 2
    assert "blog id must be a number" in result.stderr
