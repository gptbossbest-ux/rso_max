#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

stack="${1:-prod}"
case "$stack" in
  prod)
    project="rso-max-prod"
    app_env=".env"
    runtime_env=".env.prod.runtime"
    runtime_dir="./runtime/prod"
    web_port="5000"
    deploy_app_env="production"
    ;;
  test)
    project="rso-max-test"
    app_env=".env.test"
    runtime_env=".env.test.runtime"
    runtime_dir="./runtime/test"
    web_port="5001"
    deploy_app_env="test"
    ;;
  *)
    echo "Usage: $0 [prod|test]" >&2
    exit 2
    ;;
esac

app_uid="${APP_UID:-$(id -u)}"
app_gid="${APP_GID:-$(id -g)}"

dc() {
  APP_ENV_FILE="$app_env" \
  APP_RUNTIME_ENV_FILE="$runtime_env" \
  RUNTIME_DIR="$runtime_dir" \
  WEB_BIND_PORT="$web_port" \
  DEPLOY_APP_ENV="$deploy_app_env" \
  APP_UID="$app_uid" \
  APP_GID="$app_gid" \
    docker compose --project-name "$project" "$@"
}

stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
filename="database-${stamp}.sqlite"
host_target="$runtime_dir/backups/$filename"
host_temporary="$runtime_dir/backups/.${filename}.tmp"
container_temporary="/app/backups/.${filename}.tmp"

cleanup() {
  rm -f -- "$host_temporary"
}
trap cleanup EXIT

dc exec -T api python - "$container_temporary" <<'PY'
import os
import sqlite3
import sys

from config import DB_PATH

target = sys.argv[1]
source = sqlite3.connect(DB_PATH)
destination = sqlite3.connect(target)
try:
    with destination:
        source.backup(destination)
    result = destination.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError("backup integrity_check failed")
finally:
    destination.close()
    source.close()
os.chmod(target, 0o600)
PY

mv -- "$host_temporary" "$host_target"
trap - EXIT
find "$runtime_dir/backups" -type f -name 'database-*.sqlite' -mtime +30 -delete
echo "$host_target"
