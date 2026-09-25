#!/usr/bin/env bash
set -euo pipefail
umask 077

mode="${1:-}"
case "$mode" in
  fast)
    branch=Main_test
    stack=test
    project=rso-max-test
    app_env_name=.env.test
    runtime_env_name=.env.test.runtime
    web_port=5001
    deploy_app_env=test
    expected_bot_id="${EXPECTED_TEST_BOT_ID:?EXPECTED_TEST_BOT_ID is required}"
    expected_bot_username="${EXPECTED_TEST_BOT_USERNAME:?EXPECTED_TEST_BOT_USERNAME is required}"
    ;;
  full)
    branch=main
    stack=prod
    project=rso-max-prod
    app_env_name=.env
    runtime_env_name=.env.prod.runtime
    web_port=5000
    deploy_app_env=production
    expected_bot_id="${EXPECTED_PROD_BOT_ID:?EXPECTED_PROD_BOT_ID is required}"
    expected_bot_username="${EXPECTED_PROD_BOT_USERNAME:?EXPECTED_PROD_BOT_USERNAME is required}"
    ;;
  *)
    echo "Usage: auto-deploy.sh fast|full" >&2
    exit 2
    ;;
esac

: "${REPO_SLUG:?REPO_SLUG is required}"
[[ "$REPO_SLUG" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
  echo "REPO_SLUG must use owner/name format." >&2
  exit 2
}

root=/srv/bot-sandbox
config_dir="$root/config/$stack"
runtime_dir="$root/state/$stack"
current_link="$root/current/$stack"
state_dir="$root/deploy-state"
release_root="$root/releases"
repo_cache="$state_dir/repository.git"
applied_file="$state_dir/$mode.applied-sha"
lock_file="$state_dir/deploy.lock"
workflow_path=.github/workflows/ci.yml
app_env="$config_dir/$app_env_name"
runtime_env="$config_dir/$runtime_env_name"
release_tmp=
rollback_needed=false
backup_ready=false
backup_path=
previous_release=
previous_sha=
previous_image_tag=
previous_image_id=

mkdir -p "$state_dir"
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "SKIP mode=$mode reason=deployment-lock-busy"
  exit 0
fi

dc() {
  local release="$1"
  local image="$2"
  shift 2
  (
    cd "$release"
    COMPOSE_DISABLE_ENV_FILE=1 \
    APP_ENV_FILE="$app_env" \
    APP_RUNTIME_ENV_FILE="$runtime_env" \
    RUNTIME_DIR="$runtime_dir" \
    WEB_BIND_PORT="$web_port" \
    DEPLOY_APP_ENV="$deploy_app_env" \
    IMAGE_NAME="$image" \
    APP_UID="$(id -u)" \
    APP_GID="$(id -g)" \
    VCS_REF="${BUILD_REVISION:-unknown}" \
      timeout 600s docker compose --env-file "$app_env" --project-name "$project" "$@"
  )
}

container_name() {
  printf '%s-%s-1\n' "$project" "$1"
}

wait_healthy() {
  local expected_image_id="$1"
  local deadline=$((SECONDS + 240))
  local service name details all_healthy
  while ((SECONDS < deadline)); do
    all_healthy=true
    for service in api web bot; do
      name="$(container_name "$service")"
      details="$(timeout 10s docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}} {{.Image}}' "$name" 2>/dev/null || true)"
      [[ "$details" == "running healthy $expected_image_id" ]] || all_healthy=false
    done
    [[ "$all_healthy" == true ]] && return 0
    sleep 3
  done
  echo "The $stack containers did not become healthy within 240 seconds." >&2
  dc "$current_link" "$previous_image_tag" --profile bot ps >&2 || true
  return 1
}

switch_current() {
  local release="$1"
  local temporary="$root/current/.$stack.$$"
  rm -f -- "$temporary"
  ln -s -- "$release" "$temporary"
  mv -Tf -- "$temporary" "$current_link"
}

run_smoke() {
  local release="$1"
  (cd "$release" && timeout 120s /usr/local/libexec/rso-max-release-smoke "$mode")
}

restore_previous() {
  local original_status="$1"
  local rollback_status=0
  trap - EXIT
  set +e
  echo "ROLLBACK mode=$mode stack=$stack reason=deploy-failed" >&2
  rollback_started="$(date +%s)"
  dc "$previous_release" "$previous_image_tag" --profile bot stop bot web api || rollback_status=1
  if [[ "$backup_ready" == true ]]; then
    recovery="$runtime_dir/backups/failed-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    install -d -m 0700 "$recovery" || rollback_status=1
    for database_file in database.sqlite database.sqlite-wal database.sqlite-shm; do
      if [[ -e "$runtime_dir/data/$database_file" ]]; then
        mv -- "$runtime_dir/data/$database_file" "$recovery/$database_file" || rollback_status=1
      fi
    done
    restore_tmp="$runtime_dir/data/.database.sqlite.restore.$$"
    if cp -- "$backup_path" "$restore_tmp" && chmod 0600 "$restore_tmp" && python3 - "$restore_tmp" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
finally:
    connection.close()
PY
    then
      rm -f -- "$runtime_dir/data/database.sqlite-wal" "$runtime_dir/data/database.sqlite-shm"
      mv -f -- "$restore_tmp" "$runtime_dir/data/database.sqlite" || rollback_status=1
    else
      rollback_status=1
      rm -f -- "$restore_tmp"
    fi
  fi
  rm -f -- "$runtime_dir/data/bot-heartbeat" || rollback_status=1
  dc "$previous_release" "$previous_image_tag" --profile bot up -d --force-recreate --no-build api web bot || rollback_status=1
  wait_healthy "$previous_image_id" || rollback_status=1
  heartbeat_mtime="$(stat -c %Y "$runtime_dir/data/bot-heartbeat" 2>/dev/null)"
  [[ "$heartbeat_mtime" =~ ^[0-9]+$ && "$heartbeat_mtime" -ge "$rollback_started" ]] || rollback_status=1
  run_smoke "$previous_release" || rollback_status=1
  switch_current "$previous_release" || rollback_status=1
  if [[ "$rollback_status" -eq 0 ]]; then
    echo "ROLLBACK mode=$mode stack=$stack result=PASS" >&2
    exit "$original_status"
  fi
  echo "ROLLBACK mode=$mode stack=$stack result=FAIL" >&2
  exit 90
}

on_exit() {
  local status="$?"
  [[ -z "$release_tmp" ]] || rm -rf -- "$release_tmp"
  if [[ "$status" -ne 0 && "$rollback_needed" == true ]]; then
    restore_previous "$status"
  fi
  exit "$status"
}
trap on_exit EXIT
trap 'exit 143' TERM INT

for required in "$app_env" "$runtime_env" "$runtime_dir/data/database.sqlite" "$applied_file"; do
  [[ -e "$required" ]] || {
    echo "Missing required $stack deployment state: $required" >&2
    exit 1
  }
done
[[ "$(stat -c '%a' "$app_env")" == 600 && "$(stat -c '%a' "$runtime_env")" == 600 ]] || {
  echo "The $stack env files must have mode 0600." >&2
  exit 1
}
previous_release="$(readlink -f "$current_link")"
[[ "$previous_release" == "$root/"* && -d "$previous_release" ]] || {
  echo "Invalid current release for $stack." >&2
  exit 1
}
previous_sha="$(tr -d '\r\n' <"$applied_file")"
[[ "$previous_sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Invalid recorded SHA for $mode." >&2
  exit 1
}
if [[ -f "$previous_release/.release-sha" ]]; then
  current_release_sha="$(tr -d '\r\n' <"$previous_release/.release-sha")"
else
  current_release_sha="$(git -C "$previous_release" rev-parse HEAD)"
fi
[[ "$current_release_sha" == "$previous_sha" ]] || {
  echo "Current $stack release and recorded SHA disagree." >&2
  exit 1
}

set +e
candidate="$(timeout 45s python3 /usr/local/libexec/rso-max-github-ci-gate --repo "$REPO_SLUG" --branch "$branch" --workflow "$workflow_path")"
gate_status=$?
set -e
if [[ "$gate_status" -eq 3 ]]; then
  echo "SKIP mode=$mode branch=$branch reason=ci-not-ready"
  exit 0
fi
[[ "$gate_status" -eq 0 && "$candidate" =~ ^[0-9a-f]{40}$ ]] || {
  echo "GitHub CI gate failed for $branch." >&2
  exit 1
}
if [[ "$candidate" == "$previous_sha" ]]; then
  echo "SKIP mode=$mode branch=$branch sha=${candidate:0:12} reason=already-applied"
  exit 0
fi

if [[ ! -d "$repo_cache" ]]; then
  git init --bare "$repo_cache" >/dev/null
fi
remote_url="https://github.com/$REPO_SLUG.git"
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0=http.extraHeader \
  GIT_CONFIG_VALUE_0="Authorization: Bearer $GITHUB_TOKEN" \
    timeout 90s git -C "$repo_cache" fetch --no-tags --force "$remote_url" "refs/heads/$branch:refs/deploy/$mode"
else
  timeout 90s git -C "$repo_cache" fetch --no-tags --force "$remote_url" "refs/heads/$branch:refs/deploy/$mode"
fi
fetched="$(git -C "$repo_cache" rev-parse "refs/deploy/$mode^{commit}")"
if [[ "$fetched" != "$candidate" ]]; then
  echo "SKIP mode=$mode branch=$branch reason=branch-advanced-during-poll"
  exit 0
fi
if git -C "$repo_cache" ls-tree -r "$candidate" | awk '$1 == "120000" || $1 == "160000" {bad=1} END {exit bad ? 0 : 1}'; then
  echo "Release trees containing symlinks or submodules are not allowed." >&2
  exit 1
fi

release="$release_root/rso_max-$stack-${candidate:0:12}"
if [[ ! -d "$release" ]]; then
  release_tmp="$(mktemp -d "$release_root/.rso-max-$stack.XXXXXX")"
  git -C "$repo_cache" archive --format=tar "$candidate" | tar -xf - -C "$release_tmp"
  printf '%s\n' "$candidate" >"$release_tmp/.release-sha"
  ln -s -- "$app_env" "$release_tmp/$app_env_name"
  ln -s -- "$runtime_env" "$release_tmp/$runtime_env_name"
  mkdir -p "$release_tmp/runtime"
  ln -s -- "$runtime_dir" "$release_tmp/runtime/$stack"
  chmod 0750 "$release_tmp"
  mv -- "$release_tmp" "$release"
  release_tmp=
fi
[[ "$(tr -d '\r\n' <"$release/.release-sha")" == "$candidate" ]]
[[ "$(readlink -f "$release/$app_env_name")" == "$app_env" ]]
[[ "$(readlink -f "$release/$runtime_env_name")" == "$runtime_env" ]]
[[ "$(readlink -f "$release/runtime/$stack")" == "$runtime_dir" ]]

previous_image_id="$(timeout 10s docker inspect --format '{{.Image}}' "$(container_name api)")"
[[ "$previous_image_id" == sha256:* ]]
previous_image_tag="rso-max:rollback-$stack-${previous_sha:0:12}"
docker image tag "$previous_image_id" "$previous_image_tag"
wait_healthy "$previous_image_id"

image="rso-max:$stack-${candidate:0:12}"
export BUILD_REVISION="$candidate"
dc "$release" "$image" config --quiet
dc "$release" "$image" build
new_image_id="$(docker image inspect --format '{{.Id}}' "$image")"
[[ "$new_image_id" == sha256:* ]]
identity="$(cd "$release" && timeout 30s docker run --rm --read-only --tmpfs /tmp:size=16m,mode=1777 --env-file "$app_env" --env-file "$runtime_env" "$image" python scripts/max_bot_identity.py)"
IFS=$'\t' read -r bot_id bot_username <<<"$identity"
[[ "$bot_id" == "$expected_bot_id" && "$bot_username" == "$expected_bot_username" ]] || {
  echo "MAX identity does not match the pinned $stack bot identity." >&2
  exit 1
}

rollback_needed=true
dc "$release" "$image" --profile bot stop bot web
backup_output="$(cd "$previous_release" && COMPOSE_DISABLE_ENV_FILE=1 timeout 120s ./scripts/backup.sh "$stack")"
backup_path="$previous_release/${backup_output#./}"
[[ -s "$backup_path" ]] || {
  echo "The quiesced $stack backup was not created." >&2
  exit 1
}
backup_ready=true
dc "$release" "$image" --profile bot stop api
rm -f -- "$runtime_dir/data/bot-heartbeat"
dc "$release" "$image" --profile bot up -d --force-recreate --no-build api web bot
wait_healthy "$new_image_id"
run_smoke "$release"
switch_current "$release"

applied_tmp="$state_dir/.$mode.applied-sha.$$"
printf '%s\n' "$candidate" >"$applied_tmp"
chmod 0600 "$applied_tmp"
mv -f -- "$applied_tmp" "$applied_file"
rollback_needed=false
trap - EXIT
echo "DEPLOY mode=$mode stack=$stack branch=$branch sha=${candidate:0:12} result=PASS"
