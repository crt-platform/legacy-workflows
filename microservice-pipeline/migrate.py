#!/usr/bin/env python3
"""
migrate.py — make a microservice branch buildable on the crt-platform pipeline.

Run from the root of a checked-out service repo:

    python3 migrate.py <repo-name>          # e.g. ecom.catalog
    python3 migrate.py <repo-name> --check  # report only, change nothing
    python3 migrate.py <repo-name> --release-branch master

`--release-branch` is the TARGET repo's default branch. Upstream and downstream
disagree about main-vs-master (ndcmsl/ecom.catalog releases from `main`, the
crt-platform copy only has `master`), and a .releaserc naming a branch that does
not exist makes semantic-release die with ERELEASEBRANCHES. Pass it and the
non-existent release branch is rebound; omit it and the value is left alone.

This is the microservices counterpart of `prestashop-deploy/`. PrestaShop's
promotion ATTACHES files that do not exist upstream, so a copy is enough. A
microservice branch already CONTAINS `release.yml`, `.releaserc`, etc. in their
ndcmsl form, so the pipeline has to be transformed in place.

Design rule: PRESERVE, don't impose. The thin `release.yml` has five variants
across the migrated repos and the differences are real — some trigger on `main`,
some on `master`, one carries `paths-ignore`. So every edit is a targeted patch
and files that are already correct are left byte-identical.

Encodes the 11 steps of crt-agents/ci-cd/repo-migration.md. Idempotent: running
it on an already-migrated branch makes no changes at all.

Exit codes: 0 ok · 3 refused (maverick SPA) · 4 assertion failed · 2 usage
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Reusable workflows we own in crt-platform/legacy-workflows. Anything else a
# repo points at (crt-platform/workflows/_nestjs-ecs.yml, _angular-spa.yml,
# _npm-package.yml, testing-central/…) belongs to the modern Docker/ECS
# pipeline and is deliberately NOT touched.
OWNED_WORKFLOWS = ("release", "release-package", "pr-check")

USES_RE = re.compile(
    r"(uses:\s*)(?:ndcmsl|crt-platform)/(?:workflows|legacy-workflows)"
    r"/\.github/workflows/(" + "|".join(OWNED_WORKFLOWS) + r")\.yml@[A-Za-z0-9._/-]+"
)

NPM_TAG_VALUE = "${{ github.ref_name == 'dev' && 'dev' || 'latest' }}"

# Deleted on migration.
#   cliq-*            Zoho Cliq notifications, unused in crt-platform. Also the
#                     files that still hold ndcmsl refs in otherwise-migrated repos.
#   gen-artifact.yml  Calls ndcmsl/workflows/.github/workflows/gen-artifact.yml,
#                     which does not exist in ndcmsl/workflows@main — it is
#                     broken upstream too. It triggers on `push: dev`, so
#                     importing it means a guaranteed failing run on every dev
#                     push. Absent from every migrated crt-platform repo. The
#                     fork's release.yml already uploads the artifact itself
#                     when `is-microservice: true`.
DELETE_WORKFLOWS = ("cliq-release.yml", "cliq-release-package.yml", "gen-artifact.yml")

# A repo-level .npmrc overrides the runner/server global one with an unset
# ${NODE_AUTH_TOKEN} -> literal-string auth -> 401 on every private download.
# ms-01 can look fine on a warm npm cache while ms-02 fails. Never keep one.
NPMRC_PATHS = (".npmrc", "package_src/.npmrc")

CODEOWNERS_PATHS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")

# Files whose ndcmsl references are OURS to fix — a leftover here fails the run.
MUST_BE_CLEAN = ("release.yml", "release-package.yml", "pr-check.yml")


class Result:
    def __init__(self):
        self.changed = []
        self.warnings = []

    def note(self, msg):
        self.changed.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)


def read(path):
    return path.read_text(encoding="utf-8")


def remote_branches(root):
    """Branch names that exist on the target repo's origin. Empty set when git
    is unavailable or the checkout has no remote refs — callers must treat that
    as 'unknown' and skip the check rather than assume nothing exists."""
    try:
        out = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return set()
        return {
            line.split("/", 1)[1]
            for line in out.stdout.split()
            if line.startswith("origin/") and not line.endswith("/HEAD")
        }
    except (OSError, subprocess.SubprocessError):
        return set()


# --------------------------------------------------------------------------
# YAML text patching. Deliberately textual, not a YAML round-trip: the repos'
# workflow files differ in indentation (2 vs 4 space) and quoting style, and a
# parse/dump cycle would reformat every file it touches into a noisy diff.
# --------------------------------------------------------------------------
def ensure_list_entry(text, key, value):
    """Ensure `value` appears in the first `<key>:` list. Handles block and
    inline (`[a, b]`) forms and matches the file's own indentation."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)" + re.escape(key) + r":\s*(.*)$", line)
        if not m:
            continue
        indent, rest = m.group(1), m.group(2).strip()

        if rest.startswith("["):
            items = [x.strip().strip("'\"") for x in rest[1:-1].split(",") if x.strip()]
            if value in items:
                return text, False
            items.append(value)
            lines[i] = f"{indent}{key}: [{', '.join(items)}]"
            return "\n".join(lines), True

        if rest:  # scalar, e.g. `branches: main` — promote to a list
            if rest.strip("'\"") == value:
                return text, False
            lines[i] = f"{indent}{key}:"
            lines.insert(i + 1, f"{indent}  - {rest.strip(chr(39) + chr(34))}")
            lines.insert(i + 2, f"{indent}  - {value}")
            return "\n".join(lines), True

        j, item_indent, items = i + 1, None, []
        while j < len(lines):
            m2 = re.match(r"^(\s+)-\s*(.+?)\s*$", lines[j])
            if not m2:
                break
            if item_indent is None:
                item_indent = m2.group(1)
                if len(item_indent) <= len(indent):
                    break
            elif m2.group(1) != item_indent:
                break
            items.append(m2.group(2).strip("'\""))
            j += 1

        if item_indent is None:
            return text, False
        if value in items:
            return text, False
        lines.insert(j, f"{item_indent}- {value}")
        return "\n".join(lines), True
    return text, False


def _indent_step(lines):
    """Infer the file's indent width (repos use both 2 and 4 space)."""
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(secrets|with):\s*$", line)
        if m and i + 1 < len(lines):
            m2 = re.match(r"^(\s+)\S", lines[i + 1])
            if m2 and len(m2.group(1)) > len(m.group(1)):
                return len(m2.group(1)) - len(m.group(1))
    return 2


def ensure_with_input(text, key, value):
    """Ensure `key: value` exists inside the first `with:` mapping, creating the
    `with:` block if the step has none.

    Real ndcmsl branches often have no `with:` at all — global.content's
    feat/MS-1421 calls the reusable workflow with only `secrets:`. Missing
    `is-microservice: true` is silent and expensive: the reusable release.yml
    gates tag resolution, compression AND artifact upload on it, so the branch
    builds green and produces nothing for Heimdall to deploy."""
    if re.search(r"^\s*" + re.escape(key) + r"\s*:", text, re.M):
        return text, False
    lines = text.split("\n")

    if not any(re.match(r"^(\s*)with:\s*$", ln) for ln in lines):
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)uses:\s*crt-platform/legacy-workflows/", line)
            if not m:
                continue
            base = m.group(1)
            step = _indent_step(lines)
            lines.insert(i + 1, f"{base}with:")
            lines.insert(i + 2, f"{base}{' ' * step}{key}: {value}")
            return "\n".join(lines), True
        return text, False

    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)with:\s*$", line)
        if not m:
            continue
        base = m.group(1)
        j, item_indent, last = i + 1, None, None
        while j < len(lines):
            m2 = re.match(r"^(\s+)(\S+)\s*:", lines[j])
            if not m2:
                break
            if item_indent is None:
                item_indent = m2.group(1)
                if len(item_indent) <= len(base):
                    break
            elif m2.group(1) != item_indent:
                break
            last = j
            j += 1
        if item_indent is None or last is None:
            return text, False
        lines.insert(last + 1, f"{item_indent}{key}: {value}")
        return "\n".join(lines), True
    return text, False


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------
def guard_maverick(wf_dir):
    """Maverick SPAs are not on this pipeline. Their `release.yml` shim calls
    release-maverick.yml and has never once executed in crt-platform (0 runs in
    all 7 SPA repos); they deploy via crt-platform/workflows/_angular-spa.yml to
    S3+CloudFront. Rewriting the shim would resurrect a dead path on the `ms`
    runner. The maverick-*-bff repos are ordinary microservices and pass."""
    for name in ("release.yml", "pr-check.yml"):
        f = wf_dir / name
        if f.is_file() and "release-maverick.yml" in read(f):
            return f"{name} calls release-maverick.yml — this is a Maverick SPA"
    return None


def fix_workflow_uses(wf_dir, res, check):
    for f in sorted(wf_dir.glob("*.y*ml")):
        text = new = read(f)
        new = USES_RE.sub(
            r"\1crt-platform/legacy-workflows/.github/workflows/\2.yml@main", new
        )
        if new != text:
            if not check:
                f.write_text(new, encoding="utf-8")
            res.note(f"{f.name}: uses -> crt-platform/legacy-workflows@main")


def fix_release_yml(wf_dir, res, check):
    f = wf_dir / "release.yml"
    if not f.is_file():
        res.warn("no .github/workflows/release.yml — nothing will build")
        return
    text = new = read(f)
    new, ch = ensure_list_entry(new, "branches", "dev")
    if ch:
        res.note("release.yml: added `dev` to push branches")
    if "is-microservice" not in new:
        new, ch2 = ensure_with_input(new, "is-microservice", "true")
        if ch2:
            res.note("release.yml: added `is-microservice: true`")
    if new != text and not check:
        f.write_text(new, encoding="utf-8")


def fix_release_package_yml(wf_dir, repo, res, check, has_package_src):
    f = wf_dir / "release-package.yml"
    if not f.is_file():
        if has_package_src:
            res.warn("package_src/ exists but there is no release-package.yml")
        return
    if not has_package_src:
        res.warn("release-package.yml exists but there is no package_src/")
    text = new = read(f)
    new, ch = ensure_list_entry(new, "branches", "dev")
    if ch:
        res.note("release-package.yml: added `dev` to push branches")
    # `npm-tag` exists ONLY in the fork. Pointing a repo at the fork without it
    # silently publishes dev pushes to the `latest` dist-tag.
    new, ch = ensure_with_input(new, "npm-tag", NPM_TAG_VALUE)
    if ch:
        res.note("release-package.yml: added `npm-tag` (dev -> --tag dev)")
    if not re.search(r"^\s*package-name\s*:", new, re.M):
        new, ch = ensure_with_input(new, "package-name", f"'{repo}'")
        if ch:
            res.note(f"release-package.yml: added `package-name: '{repo}'`")
    if new != text and not check:
        f.write_text(new, encoding="utf-8")


def delete_files(root, res, check):
    for rel in [f".github/workflows/{n}" for n in DELETE_WORKFLOWS] + list(NPMRC_PATHS):
        p = root / rel
        if p.is_file():
            if not check:
                p.unlink()
            res.note(f"deleted {rel}")


def fix_releaserc(root, res, check, release_branch):
    """Patch ONLY the `branches` key. `.releaserc` carries per-repo plugin
    config (global.carrier has npmPublish:false plus custom git assets) that a
    template overwrite would erase."""
    f = root / ".releaserc"
    if not f.is_file():
        res.warn("no .releaserc — semantic-release will not cut dev prereleases")
        return
    try:
        data = json.loads(read(f))
    except json.JSONDecodeError as e:
        res.warn(f".releaserc is not valid JSON ({e}) — left untouched")
        return

    before = json.dumps(data, sort_keys=True)
    branches = data.get("branches", "master")
    if isinstance(branches, str):
        branches = [branches]

    # A release branch that does not exist in the TARGET repo is fatal, and the
    # failure is opaque. semantic-release resolves configured branches against
    # the ones that actually exist on the remote and silently drops the rest;
    # if that leaves only prerelease entries the run dies with
    #   ERELEASEBRANCHES ... Your configuration for the problematic branches is []
    # This is not hypothetical: ndcmsl/ecom.catalog releases from `main`, the
    # crt-platform copy only has `master`, and promoting carried `main` over
    # (2026-07-31). Upstream and downstream repos genuinely disagree about
    # main-vs-master, so the incoming value cannot be trusted here.
    existing = remote_branches(root)
    if existing and release_branch and release_branch in existing:
        rebound = []
        for b in branches:
            n = b.get("name") if isinstance(b, dict) else b
            if n != "dev" and n not in existing:
                res.note(f".releaserc: release branch '{n}' does not exist here "
                         f"-> '{release_branch}' (repo default)")
                b = {**b, "name": release_branch} if isinstance(b, dict) else release_branch
            rebound.append(b)
        branches = rebound

    names = [(b.get("name") if isinstance(b, dict) else b) for b in branches]

    # An existing `dev` entry is left EXACTLY as it is. global.configuration
    # carries {"name":"dev","channel":"dev","prerelease":"dev"} on purpose;
    # normalising it to prerelease:true would silently change its release
    # channel. We only guarantee that a dev entry exists at all.
    data["branches"] = list(branches)
    if "dev" not in names:
        data["branches"] = list(branches) + [{"name": "dev", "prerelease": True}]
    data.setdefault("tagFormat", "${version}")

    if existing and not [
        (b.get("name") if isinstance(b, dict) else b)
        for b in data["branches"]
        if (isinstance(b, str) or not b.get("prerelease"))
        and (b.get("name") if isinstance(b, dict) else b) in existing
    ]:
        res.warn("no release branch in .releaserc exists in this repo — "
                 "semantic-release will fail with ERELEASEBRANCHES")

    if json.dumps(data, sort_keys=True) == before:
        return
    if not check:
        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    res.note(
        ".releaserc: branches -> "
        + ", ".join(b if isinstance(b, str) else b["name"] for b in data["branches"])
        + " (dev = prerelease)"
    )


def fix_package_src(root, repo, res, check):
    """Rename the PUBLISHED package only. `@ndcmsl/*` entries in dependencies
    are real packages still served from ndcmsl GitHub Packages — never touched.

    Edited as text, not via a json round-trip: these files are hand-formatted
    and a load/dump cycle would reflow every dependency block into the diff."""
    f = root / "package_src" / "package.json"
    if not f.is_file():
        return
    text = read(f)
    try:
        name = json.loads(text).get("name", "")
    except json.JSONDecodeError as e:
        res.warn(f"package_src/package.json is not valid JSON ({e}) — left untouched")
        return

    new = text
    if name.startswith("@ndcmsl/"):
        pkg = name.split("/", 1)[1]
        new, n = re.subn(
            r'("name"\s*:\s*")' + re.escape(name) + r'(")',
            r"\1@crt-platform/" + pkg + r"\2",
            new,
            count=1,
        )
        if n:
            res.note(f"package_src: {name} -> @crt-platform/{pkg}")

    # Only full github.com URLs (repository/bugs/homepage). Deliberately does
    # not match `github:ndcmsl/x` shorthand in a dependency value.
    new, n = re.subn(r"github\.com/ndcmsl/", "github.com/crt-platform/", new)
    if n:
        res.note(f"package_src/package.json: {n} org URL(s) -> crt-platform")

    if new != text and not check:
        f.write_text(new, encoding="utf-8")


def fix_codeowners(root, res, check):
    for rel in CODEOWNERS_PATHS:
        p = root / rel
        if not p.is_file():
            continue
        text = read(p)
        new = text.replace("@ndcmsl/", "@crt-platform/")
        if new != text:
            if not check:
                p.write_text(new, encoding="utf-8")
            res.note(f"{rel}: @ndcmsl/ -> @crt-platform/")


def assert_clean(root, wf_dir, res):
    """Fail on leftovers in the files we own; report the rest. Files belonging
    to the modern pipeline (lab-deploy.yml, release-ecs.yml, crt-release-*) and
    .github/prompts/ docs are someone else's business."""
    failures = []
    for f in sorted(wf_dir.glob("*.y*ml")):
        if "ndcmsl" not in read(f):
            continue
        if f.name in MUST_BE_CLEAN:
            failures.append(f".github/workflows/{f.name}")
        else:
            res.warn(f"{f.name} still references ndcmsl (not ours — left as is)")
    pkg = root / "package_src" / "package.json"
    if pkg.is_file():
        data = json.loads(read(pkg))
        if str(data.get("name", "")).startswith("@ndcmsl/"):
            failures.append("package_src/package.json name")
    return failures


def main():
    argv = sys.argv[1:]
    check = "--check" in argv
    release_branch = None
    if "--release-branch" in argv:
        i = argv.index("--release-branch")
        if i + 1 < len(argv):
            release_branch = argv[i + 1]
            argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith("-")]
    if len(args) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    repo = args[0]
    root = Path(os.environ.get("MIGRATE_ROOT", ".")).resolve()
    wf_dir = root / ".github" / "workflows"
    res = Result()

    if not wf_dir.is_dir():
        print(f"::error::{root} has no .github/workflows — not a pipeline repo")
        return 4

    refusal = guard_maverick(wf_dir)
    if refusal:
        print(f"::error::refused: {refusal}. Maverick SPAs deploy via "
              f"crt-platform/workflows/_angular-spa.yml (S3+CloudFront), not this "
              f"pipeline. maverick-*-bff repos are fine — those are microservices.")
        return 3

    has_package_src = (root / "package_src").is_dir()

    fix_workflow_uses(wf_dir, res, check)
    fix_release_yml(wf_dir, res, check)
    fix_release_package_yml(wf_dir, repo, res, check, has_package_src)
    delete_files(root, res, check)
    fix_releaserc(root, res, check, release_branch)
    fix_package_src(root, repo, res, check)
    fix_codeowners(root, res, check)

    # Run the assertion BEFORE reporting — it raises warnings of its own
    # (leftover ndcmsl references in files we deliberately do not own).
    failures = [] if check else assert_clean(root, wf_dir, res)

    print(f"--- migrate.py {repo} ({'check' if check else 'apply'}) ---")
    if res.changed:
        for c in res.changed:
            print(f"  changed: {c}")
    else:
        print("  no changes needed (branch already carries the crt-platform pipeline)")
    for w in res.warnings:
        print(f"  ::warning::{w}")

    if failures:
        print("::error::ndcmsl references remain in files this script owns: "
              + ", ".join(failures))
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
