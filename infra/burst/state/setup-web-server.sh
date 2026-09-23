#!/bin/bash
# Set up the shared state of burst mode on the web server (Debian 12):
#
#   - Postgres on 127.0.0.1: role and database "subplz".
#   - Redis on 127.0.0.1, with a password, and a journal on disk.
#   - The user "subplz-tunnel": burst workers log in as this user only to
#     forward two ports (Postgres and Redis). No shell, no other port.
#   - A nightly Postgres backup to the bucket (enabled by the runbook).
#
# Terraform (main.tf) runs this script on the web server itself. It is
# idempotent: a second run changes nothing. Input: POSTGRES_PASSWORD,
# REDIS_PASSWORD, TUNNEL_PUBLIC_KEY in the environment.
set -euo pipefail
: "${POSTGRES_PASSWORD:?}" "${REDIS_PASSWORD:?}" "${TUNNEL_PUBLIC_KEY:?}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q postgresql redis-server rclone

# --- Postgres: the Debian package listens on localhost only. -----------------
runuser -u postgres -- psql -q -v ON_ERROR_STOP=1 -v pw="$POSTGRES_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE subplz LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'subplz')\gexec
ALTER ROLE subplz PASSWORD :'pw';
SELECT 'CREATE DATABASE subplz OWNER subplz' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'subplz')\gexec
SQL

# --- Redis: localhost only, a password, and a journal (the queue survives a restart).
conf=/etc/redis/redis.conf
set_conf() {
  if grep -q "^$1 " "$conf"; then
    sed -i "s|^$1 .*|$1 $2|" "$conf"
  else
    echo "$1 $2" >> "$conf"
  fi
}
set_conf bind "127.0.0.1 -::1"
set_conf requirepass "$REDIS_PASSWORD"
set_conf appendonly yes
systemctl restart redis-server

# --- The tunnel user. ---------------------------------------------------------
if ! id subplz-tunnel >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin subplz-tunnel
fi
home=$(getent passwd subplz-tunnel | cut -d: -f6)
install -d -m 700 -o subplz-tunnel -g subplz-tunnel "$home/.ssh"
printf 'restrict,port-forwarding,permitopen="127.0.0.1:5432",permitopen="127.0.0.1:6379" %s\n' \
  "$TUNNEL_PUBLIC_KEY" > "$home/.ssh/authorized_keys"
chown subplz-tunnel:subplz-tunnel "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"

# --- Nightly backup of Postgres to the bucket. The runbook writes
# /etc/subplz-rclone.env and /etc/subplz-backup.env, then enables the timer.
cat > /etc/systemd/system/subplz-pgdump.service <<'UNIT'
[Unit]
Description=Back up the subplz Postgres database to the bucket

[Service]
Type=oneshot
EnvironmentFile=/etc/subplz-rclone.env
EnvironmentFile=/etc/subplz-backup.env
ExecStart=/bin/bash -c 'set -o pipefail; runuser -u postgres -- pg_dump -Fc subplz | rclone rcat "r2:$BUCKET/backups/subplz-$(date -u +%%Y%%m%%d).dump"'
UNIT
cat > /etc/systemd/system/subplz-pgdump.timer <<'UNIT'
[Unit]
Description=Back up the subplz Postgres database each night

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
echo "web server ready: Postgres, Redis, tunnel user"
