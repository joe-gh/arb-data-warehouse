#!/usr/bin/env bash
# Launch the Logo Admin MCP server (stdio) with the app's protected env.
# Intended invocation: ssh ubuntu@<warehouse> sudo /opt/arb-logo-admin/mcp-run.sh
# Root-owned; the env file is root-readable only.
set -euo pipefail
set -a
# shellcheck disable=SC1091
source /etc/arb-logo-admin.env
set +a
cd /opt/arb-logo-admin
umask 027
exec /usr/sbin/runuser --user arb-logo-admin -- \
    /opt/arb-logo-admin-venv/bin/python /opt/arb-logo-admin/mcp_server.py
