#!/usr/bin/env bash
# stage-release.sh <release-name>
#
# Runs ON the PrestaShop box (as root, via sudo) after the CI runner has
# rsynced the repo tree into $BASE/releases/<release-name>/.
# Ports the manual runbook's 3 overlay layers (crt-agents/prestashop/
# deployment.md §2.1) to the releases/ + shared/ layout.
#
# Layer 1  instance config copied from the CURRENTLY ACTIVE release
# Layer 2  catch-all sweep of untracked runtime files from the active release
# Layer 3  shared/ symlinks + runtime dirs + ownership + sanity gate
set -euo pipefail

REL="${1:?usage: stage-release.sh <release-name>}"
BASE=/var/www/efectoled.com
NEW="$BASE/releases/$REL"
CUR="$(readlink -f "$BASE/htdocs")"

[ -d "$NEW" ] || { echo "FATAL: $NEW does not exist (rsync step failed?)"; exit 1; }
[ -d "$CUR" ] || { echo "FATAL: no active release behind $BASE/htdocs"; exit 1; }
[ "$NEW" != "$CUR" ] || { echo "FATAL: new release IS the active release"; exit 1; }

echo "== Layer 1: instance config from active release ($CUR)"
LAYER1=(
  config/settings.inc.php
  config/defines.inc.php
  config/defines_uri.inc.php
  config/db_connections_config.inc.php
  config/db_slave_server.inc.php
  config/ip_permitidas.php
  override/classes/shop/ShopUrl.php
  health.php
  # Back-office auth. NOT repo-tracked, and Layer 2 is --ignore-existing, so a
  # release that ships its own copy silently wins and per-box users are lost.
  # This bit on 2026-08-17: the dev-only `dev-kris` user vanished from dev1 on
  # its first deploy, leaving /area-12/ reachable only from allow-listed office
  # IPs (the other htpasswd users are prod-lineage with unrecoverable plaintext).
  # A 401 looks identical whether the box is protected or locked out, so this
  # fails silently — see prestashop/fleet-instance-provisioning.md §7.
  superadmin/.htpasswd
  area-12/.htaccess
  stats/.htpasswd
)
for f in "${LAYER1[@]}"; do
  [ -e "$CUR/$f" ] || { echo "FATAL: Layer-1 file missing on active release: $f"; exit 1; }
  install -D /dev/null "$NEW/$f" 2>/dev/null || true   # ensure parent dir
  cp -a "$CUR/$f" "$NEW/$f"
  echo "   $f"
done

echo "== Layer 2: untracked-runtime sweep (--ignore-existing) from active release"
rsync -a --ignore-existing \
  --exclude "cache/"    --exclude "log"      --exclude "img"      --exclude "download" \
  --exclude "mysql-log/" --exclude "*.log"   --exclude "tools/smarty/cache/" \
  --exclude "tools/smarty/compile/"          --exclude "core/cache/" \
  --exclude "img.local-pre-efs/" --exclude "download.local-pre-efs/" \
  --exclude ".git" --exclude ".github" \
  "$CUR"/ "$NEW"/

echo "== Layer 3: shared symlinks, runtime dirs, ownership"
# img/download/log must be symlinks into shared/ — replace any repo-tracked dirs
for d in img download log; do
  if [ -e "$NEW/$d" ] && [ ! -L "$NEW/$d" ]; then rm -rf "$NEW/${d:?}"; fi
  [ -L "$NEW/$d" ] || ln -s "../../shared/$d" "$NEW/$d"
done
mkdir -p "$NEW"/cache/smarty/compile "$NEW"/cache/smarty/cache "$NEW"/cache/cachefs \
         "$NEW"/core/cache "$NEW"/tools/smarty/cache "$NEW"/tools/smarty/compile
rm -f "$NEW/cache/class_index.php"
# sibling-escape shim (DB config DG_LOG_SQL_ERROR_FILE_NAME → ../monolog/…)
[ -L "$BASE/releases/monolog" ] || ln -s ../monolog "$BASE/releases/monolog"
chown -R apache:apache "$NEW"

echo "== release marker (web-checkable: /RELEASE.txt)"
{
  echo "release: $REL"
  echo "staged:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host:    $(hostname)"
} > "$NEW/RELEASE.txt"
chown apache:apache "$NEW/RELEASE.txt"

echo "== sanity gate"
php -l "$NEW/config/settings.inc.php" >/dev/null
php -l "$NEW/config/defines.inc.php"  >/dev/null
# the defines MUST be realpath-derived (symlinked-docroot requirement)
grep -q "realpath(dirname(__FILE__)" "$NEW/config/defines.inc.php" \
  || { echo "FATAL: defines.inc.php is not realpath-derived — hardcoded root breaks the symlink layout"; exit 1; }

echo "STAGED: $REL"
