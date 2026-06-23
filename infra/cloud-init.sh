#!/usr/bin/env bash
#
# cloud-init user-data for the FDM4 warehouse box (Ubuntu 24.04 arm64).
# Installs PostgreSQL 18 + pgvector + PgBouncer + Java + Python and lays down
# a tuned config for a 32 GB box. Runs once at first boot as root.
#
# No secrets are embedded: DB passwords are generated ON the box at boot and
# written to /root/arb_warehouse_credentials.txt (root-only) so they never
# appear in EC2 user-data / instance metadata.
#
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

#######################  packages  #######################
install -d /usr/share/postgresql-common/pgdg
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list

apt-get update -y
apt-get install -y \
  postgresql-18 postgresql-18-pgvector \
  pgbouncer \
  openjdk-21-jre-headless \
  python3 python3-venv python3-pip \
  unzip jq

PGDATA_CONF="/etc/postgresql/18/main"

#######################  postgres tuning (32 GB box)  #######################
cat > "${PGDATA_CONF}/conf.d/arb-warehouse.conf" <<'CONF'
listen_addresses = '127.0.0.1'          # clients reach us via PgBouncer only
max_connections = 100                    # low; PgBouncer fans out in front
shared_buffers = 8GB
effective_cache_size = 24GB              # whole dataset fits in RAM many times over
maintenance_work_mem = 2GB               # fast index builds / vacuum after nightly load
work_mem = 64MB
max_wal_size = 4GB                       # tolerate the nightly bulk-load write burst
min_wal_size = 1GB
checkpoint_completion_target = 0.9
shared_preload_libraries = 'pg_stat_statements'
CONF
mkdir -p "${PGDATA_CONF}/conf.d"
grep -q "include_dir = 'conf.d'" "${PGDATA_CONF}/postgresql.conf" || \
  echo "include_dir = 'conf.d'" >> "${PGDATA_CONF}/postgresql.conf"

systemctl restart postgresql

#######################  roles, db, extension  #######################
ETL_PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
WOO_PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
INS_PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
CREATE ROLE etl_writer      LOGIN PASSWORD '${ETL_PW}';
CREATE ROLE woo_reader      LOGIN PASSWORD '${WOO_PW}';
CREATE ROLE insights_reader LOGIN PASSWORD '${INS_PW}';
CREATE DATABASE arb_warehouse OWNER etl_writer;
SQL

sudo -u postgres psql -v ON_ERROR_STOP=1 -d arb_warehouse <<SQL
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA IF NOT EXISTS fdm4 AUTHORIZATION etl_writer;
GRANT USAGE ON SCHEMA fdm4 TO woo_reader, insights_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA fdm4 TO woo_reader, insights_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE etl_writer IN SCHEMA fdm4
  GRANT SELECT ON TABLES TO woo_reader, insights_reader;
SQL

#######################  pgbouncer (transaction pooling)  #######################
cat > /etc/pgbouncer/pgbouncer.ini <<INI
[databases]
arb_warehouse = host=127.0.0.1 port=5432 dbname=arb_warehouse

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 25
ignore_startup_parameters = extra_float_digits
INI

# Postgres 18 stores passwords as SCRAM-SHA-256 verifiers. Copy those verbatim
# from pg_authid into userlist.txt so PgBouncer authenticates clients (and
# passes through to the server) with matching SCRAM secrets — no MD5 mismatch.
sudo -u postgres psql -tA -c \
  "SELECT '\"'||rolname||'\" \"'||rolpassword||'\"' FROM pg_authid WHERE rolname IN ('etl_writer','woo_reader','insights_reader');" \
  > /etc/pgbouncer/userlist.txt
chown postgres:postgres /etc/pgbouncer/userlist.txt
chmod 600 /etc/pgbouncer/userlist.txt

# Require TLS from clients. Self-signed cert with this instance's private IP in
# the SAN (so verify-ca is possible later). Password is already SCRAM; this
# encrypts the session data across the VPC.
PRIV_IP="$(hostname -I | awk '{print $1}')"
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /etc/pgbouncer/pgbouncer.key -out /etc/pgbouncer/pgbouncer.crt \
  -days 3650 -subj "/CN=arb-warehouse" \
  -addext "subjectAltName=IP:${PRIV_IP},DNS:arb-warehouse"
chown postgres:postgres /etc/pgbouncer/pgbouncer.key /etc/pgbouncer/pgbouncer.crt
chmod 600 /etc/pgbouncer/pgbouncer.key
chmod 644 /etc/pgbouncer/pgbouncer.crt
cat >> /etc/pgbouncer/pgbouncer.ini <<'INI'

client_tls_sslmode = require
client_tls_key_file = /etc/pgbouncer/pgbouncer.key
client_tls_cert_file = /etc/pgbouncer/pgbouncer.crt
INI

systemctl enable pgbouncer
systemctl restart pgbouncer

#######################  extractor runtime scaffold  #######################
install -d -o ubuntu -g ubuntu /opt/fdm4-extractor /opt/fdm4-extractor/jdbc
python3 -m venv /opt/fdm4-extractor/venv
/opt/fdm4-extractor/venv/bin/pip install --upgrade pip
/opt/fdm4-extractor/venv/bin/pip install JayDeBeApi JPype1 psycopg2-binary
chown -R ubuntu:ubuntu /opt/fdm4-extractor

#######################  record credentials (root-only)  #######################
cat > /root/arb_warehouse_credentials.txt <<CREDS
arb_warehouse credentials (generated $(date -u +%FT%TZ))
Connect via PgBouncer on this host, port 6432.

etl_writer       ${ETL_PW}    (read/write, schema fdm4 — extractor)
woo_reader       ${WOO_PW}    (read-only — WooCommerce nightly pull)
insights_reader  ${INS_PW}    (read-only — Insights)

psql "host=127.0.0.1 port=6432 dbname=arb_warehouse user=etl_writer"
CREDS
chmod 600 /root/arb_warehouse_credentials.txt

echo "cloud-init complete: Postgres 18 + pgvector + PgBouncer ready on 6432."
