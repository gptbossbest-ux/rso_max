#!/usr/bin/env bash
set -uo pipefail

mode="${1:-}"
case "$mode" in
  fast)
    stack=test
    project=rso-max-test
    app_env=.env.test
    runtime_env=.env.test.runtime
    runtime_dir=./runtime/test
    web_port=5001
    restart_interval="${SMOKE_RESTART_INTERVAL_SECONDS:-5}"
    sqlite_pragma=quick_check
    ;;
  full)
    stack=prod
    project=rso-max-prod
    app_env=.env
    runtime_env=.env.prod.runtime
    runtime_dir=./runtime/prod
    web_port=5000
    restart_interval="${SMOKE_RESTART_INTERVAL_SECONDS:-15}"
    sqlite_pragma=integrity_check
    ;;
  *)
    echo "FAIL check=arguments reason=usage-release-smoke-fast-or-full"
    exit 2
    ;;
esac

failures=0
pass() { echo "PASS mode=$mode stack=$stack check=$1${2:+ $2}"; }
fail() { echo "FAIL mode=$mode stack=$stack check=$1${2:+ $2}"; failures=$((failures + 1)); }

for required in compose.yaml "$app_env" "$runtime_env" "$runtime_dir/data/database.sqlite"; do
  [[ -e "$required" ]] || fail preflight "reason=missing path=$required"
done
if ((failures)); then
  echo "SUMMARY mode=$mode stack=$stack result=FAIL failures=$failures"
  exit 1
fi

export COMPOSE_DISABLE_ENV_FILE=1
export APP_ENV_FILE="$app_env"
export APP_RUNTIME_ENV_FILE="$runtime_env"
export RUNTIME_DIR="$runtime_dir"
export WEB_BIND_PORT="$web_port"
export DEPLOY_APP_ENV="$([[ "$stack" == test ]] && echo test || echo production)"
export APP_UID="${APP_UID:-$(id -u)}"
export APP_GID="${APP_GID:-$(id -g)}"
compose=(timeout 15s docker compose --env-file "$app_env" --project-name "$project" --profile bot)
services=(api web bot)
declare -A ids restarts

for service in "${services[@]}"; do
  cid="$("${compose[@]}" ps -q "$service" 2>/dev/null)"
  if [[ -z "$cid" || "$cid" == *$'\n'* ]]; then
    fail container "service=$service reason=missing-or-ambiguous"
    continue
  fi
  ids["$service"]="$cid"
  details="$(timeout 10s docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}|{{.RestartCount}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{.Image}}' "$cid" 2>/dev/null)"
  IFS='|' read -r status health restart_count label_project label_service image_id <<<"$details"
  if [[ "$status" != running || "$health" != healthy ]]; then
    fail container "service=$service status=${status:-missing} health=${health:-missing}"
  elif [[ "$label_project" != "$project" || "$label_service" != "$service" ]]; then
    fail container "service=$service reason=compose-label-mismatch"
  elif [[ ! "$restart_count" =~ ^[0-9]+$ || "$image_id" != sha256:* ]]; then
    fail container "service=$service reason=invalid-metadata"
  else
    restarts["$service"]="$restart_count"
    pass container "service=$service health=healthy restarts=$restart_count"
  fi
done

body="$(curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:$web_port/healthz" 2>/dev/null)"
if [[ "$body" == *'"status":"ok"'* || "$body" == *'"status": "ok"'* ]]; then
  pass loopback-health "port=$web_port"
else
  fail loopback-health "port=$web_port reason=unexpected-response"
fi

heartbeat_mtime="$(stat -c %Y -- "$runtime_dir/data/bot-heartbeat" 2>/dev/null)"
now="$(date +%s)"
if [[ "$heartbeat_mtime" =~ ^[0-9]+$ ]] && ((now >= heartbeat_mtime && now - heartbeat_mtime < 90)); then
  pass heartbeat "age_seconds=$((now - heartbeat_mtime))"
else
  fail heartbeat "reason=missing-or-stale"
fi

api_cid="${ids[api]:-}"
if [[ -n "$api_cid" ]]; then
  sqlite_result="$(timeout 30s docker exec "$api_cid" python -c "import sqlite3; c=sqlite3.connect('file:/app/data/database.sqlite?mode=ro', uri=True); print(c.execute('PRAGMA $sqlite_pragma').fetchone()[0]); c.close()" 2>/dev/null)"
  [[ "$sqlite_result" == ok ]] && pass sqlite "pragma=$sqlite_pragma mode=ro" || fail sqlite "pragma=$sqlite_pragma reason=failed"
else
  fail sqlite "reason=api-container-unavailable"
fi

used="$(df -Pk -- "$runtime_dir" 2>/dev/null | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
if [[ "$used" =~ ^[0-9]+$ ]] && ((100 - used >= 15)); then
  pass disk "free_percent=$((100 - used))"
else
  fail disk "reason=less-than-15-percent-free"
fi

sleep "$restart_interval"
for service in "${services[@]}"; do
  cid="${ids[$service]:-}"
  before="${restarts[$service]:-}"
  [[ -n "$cid" && -n "$before" ]] || continue
  current_id="$("${compose[@]}" ps -q "$service" 2>/dev/null)"
  after="$(timeout 10s docker inspect --format '{{.RestartCount}}' "$cid" 2>/dev/null)"
  if [[ "$current_id" == "$cid" && "$after" == "$before" ]]; then
    pass restarts "service=$service count=$after interval_seconds=$restart_interval"
  else
    fail restarts "service=$service reason=changed"
  fi
done

if [[ -f .release-sha ]]; then
  release_sha="$(tr -d '\r\n' <.release-sha)"
else
  release_sha="$(timeout 5s git rev-parse HEAD 2>/dev/null)"
fi
if [[ "$release_sha" =~ ^[0-9a-f]{40}$ ]]; then
  pass metadata "commit=${release_sha:0:12}"
else
  fail metadata "reason=invalid-release-sha"
fi

if ((failures)); then
  echo "SUMMARY mode=$mode stack=$stack result=FAIL failures=$failures"
  exit 1
fi
echo "SUMMARY mode=$mode stack=$stack result=PASS failures=0"
