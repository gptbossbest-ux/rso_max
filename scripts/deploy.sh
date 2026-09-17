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
    other_app_env=".env.test"
    other_runtime_env=".env.test.runtime"
    ;;
  test)
    project="rso-max-test"
    app_env=".env.test"
    runtime_env=".env.test.runtime"
    runtime_dir="./runtime/test"
    web_port="5001"
    deploy_app_env="test"
    other_app_env=".env"
    other_runtime_env=".env.prod.runtime"
    ;;
  *)
    echo "Usage: $0 [prod|test]" >&2
    exit 2
    ;;
esac

for required_file in "$app_env" "$runtime_env"; do
  if [[ ! -f "$required_file" ]]; then
    echo "Missing $required_file for the $stack stack." >&2
    exit 1
  fi
done

app_uid="${APP_UID:-$(id -u)}"
app_gid="${APP_GID:-$(id -g)}"
if [[ "$app_uid" == "0" || "$app_gid" == "0" ]]; then
  echo "Run deploy as the non-root owner of runtime files or set non-zero APP_UID/APP_GID." >&2
  exit 1
fi

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

install -d -m 0700 "$runtime_dir/data" "$runtime_dir/backups"
install -d -m 0750 "$runtime_dir/logs" "$runtime_dir/kv"

dc config --quiet
dc build

image_output="$(dc config --images)"
image_name="${image_output%%$'\n'*}"
if [[ -z "$image_name" ]]; then
  echo "Unable to resolve the deployment image name." >&2
  exit 1
fi

bot_identity() {
  local primary_env="$1"
  local secondary_env="$2"
  local allow_empty="$3"
  local -a env_args=(--env-file "$primary_env")
  local -a command=(python scripts/max_bot_identity.py)
  if [[ -f "$secondary_env" ]]; then
    env_args+=(--env-file "$secondary_env")
  fi
  if [[ "$allow_empty" == "true" ]]; then
    command+=(--allow-empty)
  fi
  docker run --rm --read-only --tmpfs /tmp:size=16m,mode=1777 \
    "${env_args[@]}" "$image_name" "${command[@]}"
}

selected_identity="$(bot_identity "$app_env" "$runtime_env" false)"
IFS=$'\t' read -r selected_id selected_username <<<"$selected_identity"
if [[ -z "$selected_id" || -z "$selected_username" ]]; then
  echo "MAX /me returned an invalid identity for the $stack stack." >&2
  exit 1
fi

if [[ -f "$other_app_env" ]]; then
  if other_identity="$(bot_identity "$other_app_env" "$other_runtime_env" true)"; then
    IFS=$'\t' read -r other_id other_username <<<"$other_identity"
    if [[ "$selected_id" == "$other_id" || "${selected_username,,}" == "${other_username,,}" ]]; then
      echo "The prod and test stacks must use different MAX bots." >&2
      exit 1
    fi
  else
    status=$?
    if [[ "$status" -ne 3 ]]; then
      echo "The other configured stack has an invalid MAX bot token." >&2
      exit "$status"
    fi
  fi
fi

echo "MAX bot identity validated for the $stack stack: $selected_username (id $selected_id)."
dc --profile bot stop bot
rm -f -- "$runtime_dir/data/bot-heartbeat"
dc --profile bot up -d --force-recreate api web bot

healthy=false
for _ in $(seq 1 30); do
  if dc exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" \
    >/dev/null 2>&1 \
    && dc exec -T web python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)" \
    >/dev/null 2>&1 \
    && dc exec -T bot python -c \
    "import os,time; p='/app/data/bot-heartbeat'; assert os.path.exists(p) and time.time()-os.path.getmtime(p)<90" \
    >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 2
done

if [[ "$healthy" != true ]]; then
  echo "The $stack API, web, or bot service did not become healthy in time." >&2
  dc --profile bot ps >&2
  exit 1
fi

dc --profile bot ps
