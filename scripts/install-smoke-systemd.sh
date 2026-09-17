#!/usr/bin/env bash
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi
if [[ "$#" -ne 1 || "$1" != /* ]]; then
  echo "Usage: $0 /srv/bot-sandbox/releases/ABSOLUTE_RELEASE_PATH" >&2
  exit 2
fi

release_dir="$(realpath -e -- "$1")"
case "$release_dir" in
  /srv/bot-sandbox/releases/*) ;;
  *)
    echo "Release must resolve below /srv/bot-sandbox/releases." >&2
    exit 1
    ;;
esac

for required_file in \
  "$release_dir/compose.yaml" \
  "$release_dir/scripts/smoke.sh" \
  "$release_dir/deploy/systemd/rso-max-smoke-test.service" \
  "$release_dir/deploy/systemd/rso-max-smoke-test.timer"; do
  if [[ ! -f "$required_file" ]]; then
    echo "Missing required release file: $required_file" >&2
    exit 1
  fi
done
if ! id botadmin >/dev/null 2>&1; then
  echo "Required service account botadmin does not exist." >&2
  exit 1
fi
if ! getent group botadmin >/dev/null 2>&1; then
  echo "Required service group botadmin does not exist." >&2
  exit 1
fi

install -d -o root -g root -m 0755 /usr/local/libexec
install -d -o root -g root -m 0755 /srv/bot-sandbox/current

libexec_tmp="$(mktemp /usr/local/libexec/.rso-max-smoke.XXXXXX)"
link_tmp="/srv/bot-sandbox/current/.test.$$"
cleanup() {
  rm -f -- "$libexec_tmp" "$link_tmp"
}
trap cleanup EXIT

install -o root -g root -m 0755 "$release_dir/scripts/smoke.sh" "$libexec_tmp"
mv -f -- "$libexec_tmp" /usr/local/libexec/rso-max-smoke

ln -s -- "$release_dir" "$link_tmp"
mv -Tf -- "$link_tmp" /srv/bot-sandbox/current/test

install -o root -g root -m 0644 \
  "$release_dir/deploy/systemd/rso-max-smoke-test.service" \
  /etc/systemd/system/rso-max-smoke-test.service
install -o root -g root -m 0644 \
  "$release_dir/deploy/systemd/rso-max-smoke-test.timer" \
  /etc/systemd/system/rso-max-smoke-test.timer

trap - EXIT
systemctl daemon-reload
systemctl enable --now rso-max-smoke-test.timer
systemctl start rso-max-smoke-test.service
systemctl --no-pager --full status rso-max-smoke-test.timer
