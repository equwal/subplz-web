#!/usr/bin/env bash
# Put one secret into .env without it touching shell history, the process list
# or the screen.
#
#   tools/set-secret.sh SUBPLZ_WEB_STRIPE_SECRET_KEY
set -euo pipefail

key="${1:?usage: set-secret.sh VARIABLE_NAME}"
root="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$root/.env"

read -r -s -p "Value for $key (input hidden): " value
echo
[ -n "$value" ] || { echo "Empty value - nothing changed." >&2; exit 1; }

touch "$env_file"
tmp="$(mktemp)"
grep -v "^${key}=" "$env_file" > "$tmp" || true
printf '%s=%s\n' "$key" "$value" >> "$tmp"
cat "$tmp" > "$env_file"
rm -f "$tmp"

# The service does not run as root; keep the file readable by whoever owns the
# checkout, and by nobody else.
chown "$(stat -c %U:%G "$root")" "$env_file" 2>/dev/null || true
chmod 600 "$env_file"

echo "$key saved. Apply it with: systemctl restart subplz-web"
