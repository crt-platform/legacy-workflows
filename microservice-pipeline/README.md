# microservice-pipeline — canonical crt-platform pipeline transform

**This folder is the source of truth** for what
`.github/workflows/promote-microservice.yml` does to every branch promoted from
`ndcmsl/<service>` into `crt-platform/<service>`.

| File | Role |
|---|---|
| `migrate.py` | the transform — encodes the 11 steps of `crt-agents/ci-cd/repo-migration.md` |
| `redirects.json` | reference table — **printing disabled 2026-07-31** (see below), kept as documentation |

> **`redirects.json` is currently not surfaced.** The workflow used to dump it
> when dependency validation failed; that was switched off by request because
> several `@ndcmsl` packages still have no crt-platform equivalent, so the table
> pointed at fixes that cannot be applied yet. The file remains the written
> record of the mapping. Re-enable by uncommenting the two lines in the
> `Validate dependencies` step of `promote-microservice.yml`.

Edit the transform HERE; the next promotion carries the new version.

## Why this is a script and `prestashop-deploy/` is a folder of files

PrestaShop's promotion **attaches** `deploy/` + `dev-deploy.yml`, which do not
exist upstream — copying them is the whole job. A microservice branch already
**contains** `release.yml`, `release-package.yml`, `.releaserc`, `CODEOWNERS`
and `package_src/package.json` in their ndcmsl form, so the pipeline has to be
transformed in place.

It cannot be a template overwrite either. The thin `release.yml` has **five
distinct variants across the migrated repos**, and the differences are real:
some trigger on `main`, some on `master`, one carries `paths-ignore`, and
indentation is 2-space in some repos and 4-space in others. So every edit is a
targeted patch and an already-correct file is left byte-identical.

## What it changes

1. `release.yml` / `release-package.yml` / `pr-check.yml` — `uses:` →
   `crt-platform/legacy-workflows/.github/workflows/<name>.yml@main`
2. `release.yml` — `dev` added to the push branches (existing `main`/`master`
   preserved); `is-microservice: true` ensured
3. `release-package.yml` — `dev` added; `npm-tag:` ensured (**exists only in
   the fork** — without it, dev pushes publish to the `latest` dist-tag);
   `package-name:` filled from the repo name if absent
4. `cliq-release.yml`, `cliq-release-package.yml`, `gen-artifact.yml` deleted.
   The cliq ones are unused Zoho notifications. `gen-artifact.yml` calls a
   reusable workflow that **does not exist in `ndcmsl/workflows@main`** — it is
   broken upstream too — and triggers on `push: dev`, so importing it would
   mean a guaranteed failing run on every dev push. It is absent from every
   migrated crt-platform repo, and the fork's `release.yml` already uploads the
   artifact itself when `is-microservice: true`
5. any repo-level `.npmrc` deleted (it overrides the runner/server global one
   with an unset `${NODE_AUTH_TOKEN}` → 401 on every private package; ms-01's
   warm cache hides it while ms-02 fails)
6. `.releaserc` — a `dev` prerelease entry ensured, and any **release branch
   that does not exist in the target repo is rebound to the repo's default
   branch** (see below). An **existing** dev entry is left exactly as it is
   (`global.configuration` uses `{"name":"dev","channel":"dev","prerelease":"dev"}`
   deliberately). Only the `branches` key is touched — per-repo plugin config
   survives (`global.carrier` has `npmPublish:false` plus custom git assets)
7. `package_src/package.json` — published package name and github.com URLs →
   `@crt-platform`. **`@ndcmsl/*` dependencies are never touched** — those are
   real packages still served from ndcmsl GitHub Packages
8. `CODEOWNERS` — `@ndcmsl/` → `@crt-platform/`

### main vs master — the release-branch rebind

The two orgs disagree about the release branch name, and the failure mode is
opaque. `ndcmsl/ecom.catalog` releases from `main`; the crt-platform copy has
only `master`. semantic-release resolves configured branches against the ones
that actually exist on the remote and **silently drops the rest** — so carrying
`"branches": "main"` over left only the `dev` prerelease entry, and since a
prerelease channel needs a release branch to base its versioning on, the run
died with:

```
ERELEASEBRANCHES The release branches are invalid in the `branches` configuration.
Your configuration for the problematic branches is [].
```

(Hit for real on `ecom.catalog`, 2026-07-31. Note this predated promotion —
`crt-platform/ecom.catalog:master` carried the same broken config, so any
release from that repo would have failed the same way. `ndcmsl/global.content`
has it too, so it was not a one-off.)

So `migrate.py` takes `--release-branch <target default branch>`, which
`promote-microservice.yml` derives from the checked-out target repo, and rebinds
any non-existent release branch to it. A release branch that **does** exist is
left byte-identical, and when git can't tell us the remote branches the check is
skipped rather than guessed at. `repo-migration.md` lists "push a `main` branch"
as a prerequisite; in practice every crt-platform service repo defaults to
`master`, so rebinding is the reliable direction.

**Deliberately left alone** (and reported): `crt-release-package.yml`,
`lab-deploy.yml`, `release-ecs.yml`, `sync-main-to-develop.yml`,
`generate-api-doc.yml`. Several point at `crt-platform/workflows` — a
**different, live repo** holding the modern Docker/ECS pipeline
(`_nestjs-ecs.yml`, `_angular-spa.yml`, `_npm-package.yml`). Rewriting those to
`legacy-workflows` would break them.

## What it will not do

- **Dependencies and lock files.** The workflow gates on `npm ci --dry-run`
  before pushing and prints `redirects.json` on failure, but never edits a
  dependency. Publishing a missing `@crt-platform/*` version is a human
  decision — see the `global-carrier` 0.3.3-vs-0.4.0 note in that file. Locks
  must be regenerated with the **system npm 10.9.8**, not npx npm@11.
- **Maverick SPAs.** Refused (exit 3) when `release.yml` calls
  `release-maverick.yml`. Those repos deploy through
  `crt-platform/workflows/_angular-spa.yml` to S3+CloudFront and their legacy
  `release.yml` shim has **never executed** in crt-platform. `maverick-*-bff`
  repos are ordinary microservices and pass.

## Usage

```
python3 migrate.py <repo-name>           # apply, in the repo root
python3 migrate.py <repo-name> --check   # report only
MIGRATE_ROOT=/path/to/repo python3 migrate.py <repo-name>
```

Idempotent — a second run makes no changes. Exit codes: `0` ok, `3` refused
(Maverick SPA), `4` assertion failed (an ndcmsl reference survived in a file
this script owns), `2` usage.

## Flow

```
/ryan <ms-name> [origin-branch] [destination-branch]      defaults: crt-dev, dev
  → ms-promote Lambda (create-shared)
  → workflow_dispatch promote-microservice.yml (this repo, main)
  → fetch ndcmsl/<ms-name>:<origin>
  → migrate.py → one commit
  → npm ci --dry-run gate
  → force-update crt-platform/<ms-name>:<destination>
  → destination != dev ⇒ also open a PR into dev
```

**The destination is always force-updated, `dev` included** — that is the
design. crt-platform's `dev` diverges from upstream precisely because of the
pipeline commits (`Github workflows, modify to deploy on crt-platform`,
`chore: migrate to crt-platform org`), and `migrate.py` regenerates them on
every promotion. The two divergences it does *not* regenerate are caught before
the push: a missing `package-lock.json` or ndcmsl-flavoured dependencies both
fail the `npm ci --dry-run` gate.

`master`, `main`, `lab` and `develop` remain refused as destinations — nothing
here should rewrite a release or onprem-sync branch.

⚠ Residual, accepted: source-level fixes that live only on crt-platform `dev`
(e.g. the `ecom.notification` EmailStyles revert, `a7773176`) are overwritten if
the upstream branch still carries the problem. That surfaces as a red release
build, not a silent break. `chore(release)` commits are also dropped, but tags
are separate refs and survive, so semantic-release keeps numbering correctly.

Reaching `dev` is what produces the artifact. **Promotion never deploys** —
deploying is Heimdall, by hand, and needs the service's config on ms-deploy
`main` plus a `heimdall/<service>/prod` secret.
