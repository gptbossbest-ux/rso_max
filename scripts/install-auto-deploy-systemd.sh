#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi
if [[ "$#" -lt 9 || "$#" -gt 10 ]]; then
  echo "Usage: $0 OWNER/REPO TEST_RELEASE TEST_SHA TEST_BOT_ID TEST_BOT_USERNAME PROD_RELEASE PROD_SHA PROD_BOT_ID PROD_BOT_USERNAME [--enable]" >&2
  exit 2
fi

repo_slug="$1"
test_release="$(realpath -e -- "$2")"
test_sha="$3"
test_bot_id="$4"
test_bot_username="$5"
prod_release="$(realpath -e -- "$6")"
prod_sha="$7"
prod_bot_id="$8"
prod_bot_username="$9"
enable=false
if [[ "${10:-}" == --enable ]]; then
  enable=true
elif [[ -n "${10:-}" ]]; then
  echo "The only optional argument is --enable." >&2
  exit 2
fi

[[ "$repo_slug" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]
[[ "$test_sha" =~ ^[0-9a-f]{40}$ && "$prod_sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$test_bot_id" =~ ^[0-9]+$ && "$prod_bot_id" =~ ^[0-9]+$ ]]
[[ "$test_bot_username" =~ ^[A-Za-z0-9_.-]+$ && "$prod_bot_username" =~ ^[A-Za-z0-9_.-]+$ ]]
if [[ "$test_bot_id" == "$prod_bot_id" || "${test_bot_username,,}" == "${prod_bot_username,,}" ]]; then
  echo "Test and production must pin different MAX bot identities." >&2
  exit 1
fi
case "$test_release" in /srv/bot-sandbox/*) ;; *) exit 1 ;; esac
case "$prod_release" in /srv/bot-sandbox/*) ;; *) exit 1 ;; esac

release_sha() {
  local release="$1"
  if [[ -f "$release/.release-sha" ]]; then
    tr -d '\r\n' <"$release/.release-sha"
  else
    git -C "$release" rev-parse HEAD
  fi
}

test "$(release_sha "$test_release")" = "$test_sha"
test "$(release_sha "$prod_release")" = "$prod_sha"
for release_spec in "$test_release:$test_sha" "$prod_release:$prod_sha"; do
  release="${release_spec%:*}"
  sha="${release_spec##*:}"
  test -e "$release/.git"
  test "$(git -C "$release" rev-parse HEAD)" = "$sha"
  test -z "$(git -C "$release" status --porcelain --untracked-files=normal)"
done

for required in \
  "$test_release/.env.test" \
  "$test_release/.env.test.runtime" \
  "$prod_release/.env" \
  "$prod_release/.env.prod.runtime" \
  /srv/bot-sandbox/state/test/data/database.sqlite \
  /srv/bot-sandbox/state/prod/data/database.sqlite; do
  test -f "$required"
done
test "$(realpath -e "$test_release/runtime/test")" = /srv/bot-sandbox/state/test
test "$(realpath -e "$prod_release/runtime/prod")" = /srv/bot-sandbox/state/prod
id botadmin >/dev/null 2>&1
getent group botadmin >/dev/null 2>&1

check_bootstrap_empty() {
  python3 - "$1" "$2" <<'PY'
import pathlib
import sys

env_path, database_path = map(pathlib.Path, sys.argv[1:])
if database_path.exists() and database_path.stat().st_size:
    value = ""
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and line.partition("=")[0].strip() == "BOOTSTRAP_ADMIN_PASSWORD":
            value = line.partition("=")[2].strip().strip("'\"")
    if value:
        raise SystemExit("BOOTSTRAP_ADMIN_PASSWORD must be empty for an existing database")
PY
}
check_bootstrap_empty "$test_release/.env.test.runtime" /srv/bot-sandbox/state/test/data/database.sqlite
check_bootstrap_empty "$prod_release/.env.prod.runtime" /srv/bot-sandbox/state/prod/data/database.sqlite

install -d -o botadmin -g botadmin -m 0750 \
  /srv/bot-sandbox/config/test \
  /srv/bot-sandbox/config/prod \
  /srv/bot-sandbox/current \
  /srv/bot-sandbox/deploy-state \
  /srv/bot-sandbox/releases
install -d -o root -g root -m 0755 /usr/local/libexec /etc/rso-max-deploy
install -o botadmin -g botadmin -m 0600 "$test_release/.env.test" /srv/bot-sandbox/config/test/.env.test
install -o botadmin -g botadmin -m 0600 "$test_release/.env.test.runtime" /srv/bot-sandbox/config/test/.env.test.runtime
install -o botadmin -g botadmin -m 0600 "$prod_release/.env" /srv/bot-sandbox/config/prod/.env
install -o botadmin -g botadmin -m 0600 "$prod_release/.env.prod.runtime" /srv/bot-sandbox/config/prod/.env.prod.runtime

if [[ ! -e /etc/rso-max-deploy/github.conf ]]; then
  printf 'REPO_SLUG=%s\nGITHUB_TOKEN=\nEXPECTED_TEST_BOT_ID=%s\nEXPECTED_TEST_BOT_USERNAME=%s\nEXPECTED_PROD_BOT_ID=%s\nEXPECTED_PROD_BOT_USERNAME=%s\n' \
    "$repo_slug" "$test_bot_id" "$test_bot_username" "$prod_bot_id" "$prod_bot_username" \
    >/etc/rso-max-deploy/github.conf
  chown root:root /etc/rso-max-deploy/github.conf
  chmod 0600 /etc/rso-max-deploy/github.conf
else
  test "$(stat -c '%U:%G:%a' /etc/rso-max-deploy/github.conf)" = root:root:600
  grep -Fx "REPO_SLUG=$repo_slug" /etc/rso-max-deploy/github.conf >/dev/null
  grep -Fx "EXPECTED_TEST_BOT_ID=$test_bot_id" /etc/rso-max-deploy/github.conf >/dev/null
  grep -Fx "EXPECTED_TEST_BOT_USERNAME=$test_bot_username" /etc/rso-max-deploy/github.conf >/dev/null
  grep -Fx "EXPECTED_PROD_BOT_ID=$prod_bot_id" /etc/rso-max-deploy/github.conf >/dev/null
  grep -Fx "EXPECTED_PROD_BOT_USERNAME=$prod_bot_username" /etc/rso-max-deploy/github.conf >/dev/null
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
install -o root -g root -m 0755 "$repo_dir/scripts/auto-deploy.sh" /usr/local/libexec/rso-max-auto-deploy
install -o root -g root -m 0755 "$repo_dir/scripts/github_ci_gate.py" /usr/local/libexec/rso-max-github-ci-gate
install -o root -g root -m 0755 "$repo_dir/scripts/release-smoke.sh" /usr/local/libexec/rso-max-release-smoke

for unit in \
  rso-max-auto-deploy-fast.service \
  rso-max-auto-deploy-fast.timer \
  rso-max-auto-deploy-full.service \
  rso-max-auto-deploy-full.timer; do
  install -o root -g root -m 0644 "$repo_dir/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done

atomic_link() {
  local target="$1" name="$2"
  local temporary="/srv/bot-sandbox/current/.$name.$$"
  rm -f -- "$temporary"
  ln -s -- "$target" "$temporary"
  mv -Tf -- "$temporary" "/srv/bot-sandbox/current/$name"
}
atomic_link "$test_release" test
atomic_link "$prod_release" prod

test_image="$(docker inspect --format '{{.Image}}' rso-max-test-api-1)"
prod_image="$(docker inspect --format '{{.Image}}' rso-max-prod-api-1)"
for service in api web bot; do
  test "$(docker inspect --format '{{.Image}}' "rso-max-test-$service-1")" = "$test_image"
  test "$(docker inspect --format '{{.Image}}' "rso-max-prod-$service-1")" = "$prod_image"
done
test "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$test_image")" = "$test_sha"
test "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$prod_image")" = "$prod_sha"
(cd "$test_release" && /usr/local/libexec/rso-max-release-smoke fast)
(cd "$prod_release" && /usr/local/libexec/rso-max-release-smoke full)

printf '%s\n' "$test_sha" >/srv/bot-sandbox/deploy-state/fast.applied-sha
printf '%s\n' "$prod_sha" >/srv/bot-sandbox/deploy-state/full.applied-sha
chown botadmin:botadmin /srv/bot-sandbox/deploy-state/*.applied-sha
chmod 0600 /srv/bot-sandbox/deploy-state/*.applied-sha

docker image tag "$test_image" "rso-max:rollback-test-${test_sha:0:12}"
docker image tag "$prod_image" "rso-max:rollback-prod-${prod_sha:0:12}"

systemctl daemon-reload
if [[ "$enable" == true ]]; then
  set -a
  # This file is root-owned, contains only GitHub access configuration, and is
  # never printed. shellcheck disable is intentional for a systemd env file.
  source /etc/rso-max-deploy/github.conf
  set +a
  test "$(/usr/local/libexec/rso-max-github-ci-gate --repo "$repo_slug" --branch Main_test --workflow .github/workflows/ci.yml)" = "$test_sha"
  test "$(/usr/local/libexec/rso-max-github-ci-gate --repo "$repo_slug" --branch main --workflow .github/workflows/ci.yml)" = "$prod_sha"
  systemctl enable --now rso-max-auto-deploy-fast.timer rso-max-auto-deploy-full.timer
else
  echo "Installed disabled. Re-run with the same baselines and --enable after both branch heads match them."
fi
