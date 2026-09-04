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
# The person behind this session: the login that ran `sudo mcp-run.sh` (or,
# outside sudo, the current user). Authorization tiers and audit attribution
# in mcp_server.py key on it; an explicit ARB_MCP_OPERATOR in the env wins.
export ARB_MCP_OPERATOR="${ARB_MCP_OPERATOR:-${SUDO_USER:-${USER:-}}}"
exec /usr/sbin/runuser --user arb-logo-admin -- \
    /opt/arb-logo-admin-venv/bin/python /opt/arb-logo-admin/mcp_server.py
