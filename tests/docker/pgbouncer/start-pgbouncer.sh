#!/bin/sh
# Writes the PgBouncer configuration from the environment and runs PgBouncer in the foreground.
set -eu

server_host="${PGBOUNCER_SERVER_HOST:-host.docker.internal}"
server_port="${PGBOUNCER_SERVER_PORT:-5433}"
server_user="${PGBOUNCER_SERVER_USER:-postgres}"
server_password="${PGBOUNCER_SERVER_PASSWORD:-postgres}"

cat > /etc/pgbouncer/userlist.txt <<EOF
"${server_user}" "${server_password}"
EOF

cat > /etc/pgbouncer/pgbouncer.ini <<EOF
[databases]
* = host=${server_host} port=${server_port} user=${server_user} password=${server_password}

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
max_client_conn = 2000
default_pool_size = 40
max_db_connections = 60
max_prepared_statements = 200
track_extra_parameters = search_path
server_reset_query =
admin_users = ${server_user}
EOF

exec pgbouncer /etc/pgbouncer/pgbouncer.ini
