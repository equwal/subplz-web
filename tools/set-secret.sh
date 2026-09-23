#!/usr/bin/env bash
# Put one secret into .env (or into FILE) without it touching shell history,
# the process list or the screen.
#
#   tools/set-secret.sh SUBPLZ_WEB_STRIPE_SECRET_KEY
#   tools/set-secret.sh DIGITALOCEAN_TOKEN /etc/subplz-burst.env
set -euo pipefail

key="${1:?usage: set-secret.sh VARIABLE_NAME [FILE]}"
root="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${2:-$root/.env}"

read -r -s -p "Value for $key (input hidden): " value
echo
[ -n "$value" ] || { echo "Empty value - nothing changed." >&2; exit 1; }

touch "$env_file"
tmp="$(mktemp)"
grep -v "^${key}=" "$env_file" > "$tmp" || true
printf '%s=%s\n' "$key" "$value" >> "$tmp"
cat "$tmp" > "$env_file"
rm -f "$tmp"

# The service does not run as root; keep .env readable by whoever owns the
# checkout, and by nobody else. Another FILE stays with root.
if [ -z "${2:-}" ]; then
  chown "$(stat -c %U:%G "$root")" "$env_file" 2>/dev/null || true
fi
chmod 600 "$env_file"

echo "$key saved in $env_file. Restart the services that read it."
