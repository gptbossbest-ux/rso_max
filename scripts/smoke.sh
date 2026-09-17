#!/usr/bin/env bash
set -uo pipefail

if [[ -n "${SMOKE_WORKING_DIRECTORY:-}" ]]; then
  cd "$SMOKE_WORKING_DIRECTORY" || exit 2
fi

stack="${1:-test}"
if [[ "$stack" != "test" ]]; then
  echo "FAIL stack=unknown check=arguments reason=usage-smoke-test"
  exit 2
fi
project="rso-max-test"
app_env=".env.test"
runtime_env=".env.test.runtime"
runtime_dir="./runtime/test"
web_port="5001"
deploy_app_env="test"

failures=0
pass() {
  echo "PASS stack=$stack check=$1${2:+ $2}"
}
fail() {
  echo "FAIL stack=$stack check=$1${2:+ $2}"
  failures=$((failures + 1))
}

for required_file in compose.yaml "$app_env" "$runtime_env"; do
  if [[ ! -f "$required_file" ]]; then
    fail preflight "reason=missing-file file=$required_file"
  fi
done
if ((failures > 0)); then
  echo "SUMMARY stack=$stack result=FAIL failures=$failures"
  exit 1
fi

export APP_ENV_FILE="$app_env"
export APP_RUNTIME_ENV_FILE="$runtime_env"
export RUNTIME_DIR="$runtime_dir"
export WEB_BIND_PORT="$web_port"
export DEPLOY_APP_ENV="$deploy_app_env"
export APP_UID="${APP_UID:-$(id -u)}"
export APP_GID="${APP_GID:-$(id -g)}"

compose=(docker compose --project-name "$project" --profile bot)
services=(api web bot)
declare -A container_ids restart_before

for service in "${services[@]}"; do
  cid="$(timeout 10s "${compose[@]}" ps -a -q "$service" 2>/dev/null)"
  if [[ -z "$cid" || "$cid" == *$'\n'* ]]; then
    fail container "service=$service reason=missing-or-ambiguous"
    continue
  fi
  container_ids["$service"]="$cid"

  details="$(timeout 10s docker inspect --format \
    '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}|{{.RestartCount}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{.Image}}' \
    "$cid" 2>/dev/null)"
  IFS='|' read -r status health restarts label_project label_service image_id <<<"$details"
  if [[ "$status" != "running" ]]; then
    fail container "service=$service reason=not-running status=${status:-unknown}"
  elif [[ "$health" != "healthy" ]]; then
    fail container "service=$service reason=not-healthy health=${health:-unknown}"
  elif [[ "$label_project" != "$project" || "$label_service" != "$service" ]]; then
    fail container "service=$service reason=compose-label-mismatch"
  elif [[ ! "$restarts" =~ ^[0-9]+$ || -z "$image_id" ]]; then
    fail container "service=$service reason=invalid-inspect-metadata"
  else
    restart_before["$service"]="$restarts"
    pass container "service=$service status=running health=healthy restarts=$restarts"
  fi
done

health_body="$(curl --fail --silent --show-error --max-time 5 \
  "http://127.0.0.1:${web_port}/healthz" 2>/dev/null)"
if [[ "$health_body" == *'"status":"ok"'* || "$health_body" == *'"status": "ok"'* ]]; then
  pass loopback-health "port=$web_port"
else
  fail loopback-health "port=$web_port reason=unexpected-response"
fi

heartbeat="$runtime_dir/data/bot-heartbeat"
heartbeat_mtime="$(stat -c %Y -- "$heartbeat" 2>/dev/null)"
now="$(date +%s)"
if [[ "$heartbeat_mtime" =~ ^[0-9]+$ ]] && ((now >= heartbeat_mtime)) \
  && ((now - heartbeat_mtime < 90)); then
  pass bot-heartbeat "age_seconds=$((now - heartbeat_mtime))"
else
  fail bot-heartbeat "reason=missing-or-stale"
fi

api_cid="${container_ids[api]:-}"
if [[ -n "$api_cid" ]]; then
  quick_check="$(timeout 10s docker exec "$api_cid" python -c \
    "import sqlite3; c=sqlite3.connect('file:/app/data/database.sqlite?mode=ro', uri=True); print(c.execute('PRAGMA quick_check').fetchone()[0]); c.close()" \
    2>/dev/null)"
  if [[ "$quick_check" == "ok" ]]; then
    pass sqlite "pragma=quick_check mode=ro"
  else
    fail sqlite "reason=quick-check-failed"
  fi
else
  fail sqlite "reason=api-container-unavailable"
fi

disk_line="$(df -Pk -- "$runtime_dir" 2>/dev/null | awk 'NR==2 {print $5}')"
disk_used="${disk_line%%%}"
if [[ "$disk_used" =~ ^[0-9]+$ ]]; then
  disk_free=$((100 - disk_used))
  if ((disk_free >= 15)); then
    pass disk "free_percent=$disk_free threshold_percent=15"
  else
    fail disk "free_percent=$disk_free threshold_percent=15"
  fi
else
  fail disk "reason=unable-to-read-free-space"
fi

interval="${SMOKE_RESTART_INTERVAL_SECONDS:-5}"
sleep "$interval"
for service in "${services[@]}"; do
  old_cid="${container_ids[$service]:-}"
  old_restarts="${restart_before[$service]:-}"
  [[ -n "$old_cid" && -n "$old_restarts" ]] || continue

  current_cid="$(timeout 10s "${compose[@]}" ps -q "$service" 2>/dev/null)"
  current_restarts="$(timeout 10s docker inspect --format '{{.RestartCount}}' "$old_cid" 2>/dev/null)"
  if [[ "$current_cid" != "$old_cid" ]]; then
    fail restarts "service=$service reason=container-replaced"
  elif [[ "$current_restarts" != "$old_restarts" ]]; then
    fail restarts "service=$service before=$old_restarts after=${current_restarts:-unknown}"
  else
    pass restarts "service=$service count=$current_restarts interval_seconds=$interval"
  fi
done

commit="$(timeout 5s git rev-parse --short=12 HEAD 2>/dev/null)"
api_image=""
if [[ -n "$api_cid" ]]; then
  api_image="$(timeout 10s docker inspect --format '{{.Image}}' "$api_cid" 2>/dev/null)"
fi
if [[ "$commit" =~ ^[0-9a-f]{7,12}$ && "$api_image" == sha256:* ]]; then
  pass metadata "commit=$commit image=${api_image:0:20}"
else
  fail metadata "reason=unavailable"
fi

if ((failures > 0)); then
  echo "SUMMARY stack=$stack result=FAIL failures=$failures"
  exit 1
fi
echo "SUMMARY stack=$stack result=PASS failures=0"
