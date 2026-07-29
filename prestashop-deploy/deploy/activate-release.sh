#!/usr/bin/env bash
# activate-release.sh <release-name> [host-header]
#
# Atomically points $BASE/htdocs at releases/<release-name>, recycles Apache
# workers gracefully (no dropped requests, flushes OPcache/realpath caches),
# and health-checks locally. Rollback = call again with the previous release
# (recorded in $BASE/.previous-release).
set -euo pipefail

REL="${1:?usage: activate-release.sh <release-name> [host-header]}"
HDR="${2:-prestashop-dev1.pvt.create-store.com}"
BASE=/var/www/efectoled.com
TARGET="releases/$REL"

[ -d "$BASE/$TARGET" ] || { echo "FATAL: $BASE/$TARGET does not exist"; exit 1; }

PREV="$(readlink "$BASE/htdocs" | sed 's|releases/||')"
echo "$PREV" > "$BASE/.previous-release"
echo "== activating $REL (previous: $PREV)"

# atomic flip: build a temp symlink, rename over htdocs
ln -sfn "$TARGET" "$BASE/.htdocs.tmp"
mv -T "$BASE/.htdocs.tmp" "$BASE/htdocs"

apachectl graceful
sleep 3

echo "== local health check (Host: $HDR)"
CODE=$(curl -sk -m 30 -o /dev/null -w '%{http_code}' -H "Host: $HDR" https://127.0.0.1/es/ || echo 000)
if [ "$CODE" != "200" ]; then
  echo "FATAL: health check returned $CODE (previous release: $PREV — rollback with: activate-release.sh $PREV)"
  exit 1
fi
echo "ACTIVE: $REL (health 200)"
