#!/usr/bin/env bash
#
# ryan-local.sh — the local equivalent of the /ryan promotion.
#
# WHY THIS EXISTS
#   promote-microservice.yml hardcodes `UPSTREAM_ORG: ndcmsl` (env, line 59) and
#   fetches the source branch from there (`git fetch upstream refs/heads/<src>`).
#   A branch that exists ONLY in crt-platform — because you cannot push to
#   ndcmsl — can therefore never be promoted by /ryan, by Slack or by manual
#   dispatch. This script performs the same transform locally, on a branch you
#   already have, and force-updates the destination the same way.
#
# WHAT IT REPLICATES (steps map 1:1 onto promote-microservice.yml)
#   1. validate name/branch + refuse master|main|lab|develop  (workflow step "v")
#   2. work on a throwaway `_promote` branch cut from your source ref  (:134)
#   3. resolve the TARGET repo's default branch for --release-branch  (:140-142)
#   4. run microservice-pipeline/migrate.py                           (:143-145)
#   5. git add -A + the same commit message, idempotent               (:146-148)
#   6. gate: npm ci --dry-run --ignore-scripts                        (:153-181)
#   7. force-update the destination: push +_promote:refs/heads/<dest> (:185)
#   8. print the post-merge checklist from the run summary            (:218-236)
#
# The transform itself is NOT reimplemented: migrate.py is fetched from
# crt-platform/legacy-workflows@main (or reused from a local clone), so this
# stays byte-identical to what the pipeline does.
#
# SAFETY
#   - Your source branch is never modified: all work happens on `_promote`.
#   - Nothing is pushed unless you pass --push, and that asks for confirmation.
#   - Refuses to run on a dirty working tree unless --allow-dirty.
#   - --check reports what would change, in place, writing nothing.
#
# USAGE
#   cd <service repo> && bash ryan-local.sh --check
#   cd <service repo> && bash ryan-local.sh                 # transform + commit
#   cd <service repo> && bash ryan-local.sh --push          # ... and force-push
#
# Runs in Git Bash on Windows and in any POSIX shell. Needs git and python3.
#
set -euo pipefail

readonly SELF="${0##*/}"
readonly SELF_DIR="$(cd "$(dirname "$0")" && pwd)"   # resolved before any cd
readonly PROMOTE_BRANCH="_promote"
readonly TARGET_ORG="crt-platform"

# ── defaults ────────────────────────────────────────────────────────────────
DEST="dev"
SOURCE_REF=""
RELEASE_BRANCH=""
REPO_NAME=""
REMOTE=""
MIGRATE_PY="${LEGACY_WORKFLOWS:+$LEGACY_WORKFLOWS/microservice-pipeline/migrate.py}"
REQUESTED_BY=""
CHECK_ONLY=0
DO_PUSH=0
SKIP_VALIDATION=0
ALLOW_DIRTY=0
CLONE_DIR=""

die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
warn() { printf '\033[33mwarn:\033[0m %s\n'  "$*" >&2; }
step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }

cleanup() { [ -n "$CLONE_DIR" ] && rm -rf "$CLONE_DIR"; }
trap cleanup EXIT

usage() {
  sed -n '3,40p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF

Options
  --dest <branch>            destination to force-update (default: dev)
  --source <ref>             branch/ref to promote (default: current HEAD)
  --release-branch <branch>  value for migrate.py --release-branch
                             (default: the target repo's default branch)
  --repo <name>              service name (default: derived from the remote URL)
  --remote <name>            git remote pointing at $TARGET_ORG (default: detected)
  --migrate-py <path>        path to microservice-pipeline/migrate.py
                             (default: \$LEGACY_WORKFLOWS, else shallow-clone)
  --requested-by <who>       audit string for the commit message
  --check                    report only, change nothing, no branch switch
  --push                     force-update <dest> after the gate passes
  --skip-validation          skip the npm ci --dry-run gate (escape hatch)
  --allow-dirty              proceed with uncommitted changes
  -h, --help                 this text
EOF
}

# ── args ────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --dest)           DEST="${2:?}"; shift 2 ;;
    --source)         SOURCE_REF="${2:?}"; shift 2 ;;
    --release-branch) RELEASE_BRANCH="${2:?}"; shift 2 ;;
    --repo)           REPO_NAME="${2:?}"; shift 2 ;;
    --remote)         REMOTE="${2:?}"; shift 2 ;;
    --migrate-py)     MIGRATE_PY="${2:?}"; shift 2 ;;
    --requested-by)   REQUESTED_BY="${2:?}"; shift 2 ;;
    --check)          CHECK_ONLY=1; shift ;;
    --push)           DO_PUSH=1; shift ;;
    --skip-validation) SKIP_VALIDATION=1; shift ;;
    --allow-dirty)    ALLOW_DIRTY=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown option: $1 (try --help)" ;;
  esac
done

command -v git     >/dev/null || die "git not found"
command -v python3 >/dev/null || die "python3 not found"
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository"

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"   # migrate.py must run from the repo root (see its --help)

# ── step 1: validate, and refuse the branches the workflow refuses ──────────
step "Validate inputs"

case "$DEST" in
  master|main|lab|develop)
    die "destination '$DEST' is refused — this flow force-updates its destination,
       and that is a release/onprem-sync branch. Use dev, or a review branch." ;;
esac

# Detect the remote that points at crt-platform (their repo may call it `crt`,
# `origin`, or anything else; ndcmsl is deliberately NOT accepted as a target).
if [ -z "$REMOTE" ]; then
  REMOTE="$(git remote -v | awk -v org="$TARGET_ORG" '$3=="(push)" && index($2, org"/") {print $1; exit}')"
  [ -n "$REMOTE" ] || die "no git remote points at $TARGET_ORG — pass --remote"
fi
REMOTE_URL="$(git remote get-url "$REMOTE")"
case "$REMOTE_URL" in
  *"$TARGET_ORG"/*) : ;;
  *) die "remote '$REMOTE' is $REMOTE_URL — not a $TARGET_ORG remote. Refusing:
       this script force-updates the destination and must never target ndcmsl." ;;
esac

# Service repos carry dots (ecom.catalog, ecom-legacy.etl.write).
if [ -z "$REPO_NAME" ]; then
  REPO_NAME="${REMOTE_URL##*/}"; REPO_NAME="${REPO_NAME%.git}"
fi
printf '%s' "$REPO_NAME" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$' \
  || die "invalid microservice name: $REPO_NAME"
printf '%s' "$DEST" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$' \
  || die "invalid branch name: $DEST"

[ -n "$SOURCE_REF" ] || SOURCE_REF="$(git rev-parse --abbrev-ref HEAD)"
[ "$SOURCE_REF" != "HEAD" ] || die "detached HEAD — pass --source <ref>"
git rev-parse --verify --quiet "$SOURCE_REF" >/dev/null || die "ref not found: $SOURCE_REF"
SOURCE_SHA="$(git rev-parse "$SOURCE_REF")"

if [ "$ALLOW_DIRTY" -eq 0 ] && [ -n "$(git status --porcelain)" ]; then
  git status --short >&2
  die "working tree is dirty — commit, stash, or pass --allow-dirty.
       (migrate.py + 'git add -A' would otherwise sweep the above into the commit.)"
fi

[ -n "$REQUESTED_BY" ] || REQUESTED_BY="$(git config user.email 2>/dev/null || echo "${USER:-${USERNAME:-local}}")"

info "repo:        $TARGET_ORG/$REPO_NAME  (remote '$REMOTE')"
info "source:      $SOURCE_REF @ ${SOURCE_SHA:0:7}"
info "destination: $DEST  (force-update)"

# ── obtain the canonical transform ──────────────────────────────────────────
step "Locate microservice-pipeline/migrate.py"

# Shipped inside microservice-pipeline/ — the copy next to us is the canonical one.
if [ -z "$MIGRATE_PY" ] && [ -f "$SELF_DIR/migrate.py" ]; then
  MIGRATE_PY="$SELF_DIR/migrate.py"
fi

if [ -n "$MIGRATE_PY" ] && [ -f "$MIGRATE_PY" ]; then
  info "using $MIGRATE_PY"
else
  [ -n "$MIGRATE_PY" ] && warn "not found: $MIGRATE_PY — falling back to a clone"
  CLONE_DIR="$(mktemp -d)"
  info "shallow-cloning $TARGET_ORG/legacy-workflows@main ..."
  git clone --depth 1 --quiet \
    "https://github.com/$TARGET_ORG/legacy-workflows.git" "$CLONE_DIR/lw" \
    || die "could not clone legacy-workflows — check credentials, or pass --migrate-py"
  MIGRATE_PY="$CLONE_DIR/lw/microservice-pipeline/migrate.py"
  [ -f "$MIGRATE_PY" ] || die "migrate.py missing in the clone (moved upstream?)"
fi

# ── step 3: the TARGET repo's default branch, for the .releaserc rebind ─────
# semantic-release silently drops configured branches that do not exist on the
# remote, so a .releaserc carried over from ndcmsl naming `main` kills the
# release in a repo that only has `master` (ERELEASEBRANCHES).
if [ -z "$RELEASE_BRANCH" ]; then
  RELEASE_BRANCH="$(git symbolic-ref --short "refs/remotes/$REMOTE/HEAD" 2>/dev/null | sed "s#^$REMOTE/##" || true)"
  [ -n "$RELEASE_BRANCH" ] || RELEASE_BRANCH="$(git remote show "$REMOTE" 2>/dev/null | awk '/HEAD branch/ {print $NF}' || true)"
fi
if [ -n "$RELEASE_BRANCH" ]; then
  info "target default branch: $RELEASE_BRANCH"
else
  warn "could not resolve the target default branch — .releaserc will be left alone
       (pass --release-branch if the release dies with ERELEASEBRANCHES)"
fi

# ── --check: report in place, write nothing, touch no refs ──────────────────
if [ "$CHECK_ONLY" -eq 1 ]; then
  step "migrate.py --check (writes nothing)"
  python3 "$MIGRATE_PY" "$REPO_NAME" ${RELEASE_BRANCH:+--release-branch "$RELEASE_BRANCH"} --check
  printf '\nNothing was changed. Re-run without --check to transform and commit.\n'
  exit 0
fi

# ── step 2: throwaway branch, exactly like `git checkout -B _promote` ───────
step "Cut $PROMOTE_BRANCH from $SOURCE_REF"
ORIGINAL_REF="$(git rev-parse --abbrev-ref HEAD)"
git checkout -B "$PROMOTE_BRANCH" "$SOURCE_SHA" --quiet
info "on $PROMOTE_BRANCH (your '$ORIGINAL_REF' is untouched)"

# ── step 4+5: transform, then one commit with the pipeline's own message ────
step "Transform pipeline files"
MIGRATE_LOG="$(mktemp)"
if ! python3 "$MIGRATE_PY" "$REPO_NAME" ${RELEASE_BRANCH:+--release-branch "$RELEASE_BRANCH"} \
     2>&1 | tee "$MIGRATE_LOG"; then
  git checkout "$ORIGINAL_REF" --quiet
  die "migrate.py failed (exit 4 = assertion, 2 = usage) — $PROMOTE_BRANCH left at source"
fi

git add -A
if git diff --cached --quiet; then
  info "pipeline already current — nothing to attach"
else
  git commit --quiet -m "ci: attach crt-platform pipeline (promoted from $TARGET_ORG/$REPO_NAME:$SOURCE_REF@${SOURCE_SHA:0:7}, requested by $REQUESTED_BY via $SELF)"
  info "committed $(git rev-parse --short HEAD)"
  git show --stat --oneline HEAD | sed 's/^/  /'
fi

# ── step 6: the gate — fails BEFORE anything is pushed ─────────────────────
if [ "$SKIP_VALIDATION" -eq 0 ]; then
  step "Validate dependencies (npm ci --dry-run --ignore-scripts)"
  if ! command -v npm >/dev/null; then
    warn "npm not found — gate skipped; CI will run it on the runner (npm 10.9.8)"
  elif [ ! -f package-lock.json ]; then
    git checkout "$ORIGINAL_REF" --quiet
    die "no package-lock.json on this branch — npm ci cannot run.
       Fix: NODE_AUTH_TOKEN=\$TOKEN npm install --package-lock-only --ignore-scripts, then commit."
  else
    NPM_MAJOR="$(npm -v | cut -d. -f1)"
    info "npm $(npm -v) / node $(node -v 2>/dev/null || echo '?')"
    [ "$NPM_MAJOR" = "10" ] || warn "the runner uses npm 10.9.8; npm $NPM_MAJOR validates
       locks that npm 10's 'npm ci' then rejects with EUSAGE. Treat a pass here as
       provisional (crt-agents/ci-cd/AGENT.md gotcha 7)."
    # --ignore-scripts is required, not optional: npm runs `prepare` even under
    # --dry-run, and these repos use husky → 'sh: 1: husky: not found', exit 127.
    if ! npm ci --dry-run --ignore-scripts; then
      git checkout "$ORIGINAL_REF" --quiet
      die "dependency validation failed — the promoted branch would not build.
       A revoked @ndcmsl package needs a redirect (see microservice-pipeline/redirects.json
       and crt-agents/ci-cd/repo-migration.md §4/§5), or the lock needs regenerating with npm 10.
       Override with --skip-validation once you know why it fails."
    fi
  fi
fi

# ── step 7: force-update the destination ───────────────────────────────────
PUSH_CMD="git push $REMOTE +$PROMOTE_BRANCH:refs/heads/$DEST"

if [ "$DO_PUSH" -eq 0 ]; then
  step "Ready — nothing pushed"
  cat <<EOF
  $PROMOTE_BRANCH is transformed and committed. To force-update $DEST:

      $PUSH_CMD

  Then: git checkout $ORIGINAL_REF && git branch -D $PROMOTE_BRANCH
  Or re-run this script with --push.
EOF
  exit 0
fi

step "Force-update $TARGET_ORG/$REPO_NAME:$DEST"
warn "this REPLACES $DEST — anything on it that is not in $SOURCE_REF is lost."
printf '  Type the destination branch name to confirm: '
read -r CONFIRM
[ "$CONFIRM" = "$DEST" ] || { git checkout "$ORIGINAL_REF" --quiet; die "aborted — nothing pushed"; }
eval "$PUSH_CMD"

git checkout "$ORIGINAL_REF" --quiet
info "back on $ORIGINAL_REF ($PROMOTE_BRANCH kept for reference)"

# ── step 8: the post-merge checklist the run summary prints ─────────────────
cat <<EOF

### Pushed — a promotion does NOT deploy
1. release.yml cuts x.y.z-dev.N and uploads artifact main-<version>.
2. Config must be on ms-deploy **main**: $REPO_NAME/prod/crt_dev/app.settings.js
   (Heimdall fetches the DEFAULT branch, never dev).
3. Secret heimdall/$REPO_NAME/prod must exist.
4. Deploy from Heimdall (mode PROD, infrastructure CRT_DEV), then re-apply hot-patches.

⚠ Your code lives only in $TARGET_ORG. The next /ryan on this repo force-pushes
  $DEST from ndcmsl and will drop it. Get the commits into ndcmsl, or tell the team.
EOF
