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
# Layer 3  shared/ symlinks + runtime dirs + ownership
# Layer 4  environment patches — downstream-only fixes that promotion reverts
#          (see ENVIRONMENT-PATCHES.md) + sanity gate
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

echo "== Layer 4: environment patches (see ENVIRONMENT-PATCHES.md)"
# Fixes that exist ONLY downstream in crt-platform and are absent from ndcmsl.
# /yuta force-pushes ndcmsl content over crt-platform:devN, so every promotion
# reverts them. Until they are upstreamed, the deploy re-applies them here — the
# last common chokepoint before code goes live (it also covers plain pushes to a
# dev branch, which promotion-time patching would miss).
#
# Each patch MUST be idempotent and MUST hard-fail if the file no longer matches
# a known shape, so an upstream refactor stops the deploy instead of silently
# shipping a regression.

# --- shop-domain: multi-environment URL generation -------------------------
# Without this every instance generates links pointing at the main shop URL
# (prestashop.pvt.create-store.com) instead of its own hostname, because the
# 2018 logic only rewrites a domain that literally contains the string "dev1".
# Leaves the later $host lookup normalisation alone — that one is still needed.
python3 - "$NEW/override/classes/shop/Shop.php" <<'PYPATCH'
import io, sys

NEW_BLOCK = """        // Multi-entorno: usar siempre el hostname del request como dominio
        // de la shop. Asi dev, lab, local y produccion comparten la misma BD
        // y cada entorno genera URLs con su propio dominio sin tocar main=1.
        // Sustituye los bloques anteriores de (JF 2018) para dev* y de
        // Xtras::isBackDomain() para failover - este patron los cubre todos.
        // [deploy-patch] injected by stage-release.sh - see ENVIRONMENT-PATCHES.md
        if (!empty($_SERVER['HTTP_HOST'])) {
"""
MARKER, START_HINT = "Multi-entorno", "(JF)(25/07/2018)"
BODY_HINT, END_HINT = "$row['domain'] = str_replace('dev1'", "if (Xtras::isBackDomain()) {"

path = sys.argv[1]
with io.open(path, encoding="utf-8", errors="surrogateescape") as fh:
    lines = fh.readlines()

if any(MARKER in ln for ln in lines):
    print("   shop-domain: already multi-environment, nothing to do")
    sys.exit(0)

start = None
for i, ln in enumerate(lines):
    # anchor on the block that rewrites $row['domain'] — NOT the later one that
    # rewrites $host for shop lookup, which must survive untouched
    if START_HINT in ln and any(BODY_HINT in l for l in lines[i:i + 8]):
        start = i
        break
if start is None:
    sys.exit("FATAL: Shop.php has neither the multi-environment block nor the known 2018\n"
             "       $row['domain'] block. Upstream changed shape — re-derive this patch\n"
             "       (ENVIRONMENT-PATCHES.md) instead of deploying blind.")

end = None
for j in range(start, min(start + 25, len(lines))):
    if END_HINT in lines[j]:
        end = j
        break
if end is None:
    sys.exit("FATAL: found the 2018 $row['domain'] block but not the following\n"
             "       'if (Xtras::isBackDomain()) {' line it must be merged with.")

with io.open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
    fh.writelines(lines[:start] + [NEW_BLOCK] + lines[end + 1:])
print("   shop-domain: patched (replaced the 2018 dev1/isBackDomain blocks, lines %d-%d)"
      % (start + 1, end + 1))
PYPATCH
php -l "$NEW/override/classes/shop/Shop.php" >/dev/null \
  || { echo "FATAL: Shop.php failed php -l after the environment patch"; exit 1; }
chown apache:apache "$NEW/override/classes/shop/Shop.php"

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
