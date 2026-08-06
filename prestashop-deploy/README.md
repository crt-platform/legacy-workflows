> ⚠️ **`stage-release.sh` deliberately rewrites some application code at deploy time**
> (Layer 4). Fixes that exist only in `crt-platform` are reverted by every `/yuta`
> promotion, so the deploy re-applies them. For those files, what runs on the box is
> intentionally **not** what is in the branch — see
> [`ENVIRONMENT-PATCHES.md`](./ENVIRONMENT-PATCHES.md) before debugging any
> "the repo says X but the server does Y" discrepancy.

# prestashop-deploy — canonical PrestaShop dev-deploy pipeline

**This folder is the source of truth** for the files that
`promote-prestashop.yml` attaches to every promoted branch of
`crt-platform/prestashop`:

| File here | Lands on the promoted branch as |
|---|---|
| `dev-deploy.yml` | `.github/workflows/dev-deploy.yml` (branch = instance deploy trigger) |
| `deploy/` | `deploy/` (targets map + stage/activate scripts, run on the box) |

They are **not** workflows/scripts *of this repo* — `dev-deploy.yml` lives
outside `.github/workflows/` on purpose so it never runs here.

Edit the pipeline HERE; the next promotion carries the new version. Branch
copies inside the prestashop repo are attached artifacts, not sources.

Flow (triggered by the `/ps-promote` Slack command via the ps-promote Lambda
in create-shared, or manually from the Actions tab):

```
/ps-promote <source> [target]
  → workflow_dispatch promote-prestashop.yml (this repo, main)
  → fetch ndcmsl/prestashop:<source>
  → attach this folder as one commit
  → force-push to crt-platform/prestashop:<target>
  → target dev1 ⇒ dev-deploy.yml auto-deploys to prestashop-dev1
```

Server-side prerequisites per instance (releases/ layout etc.):
`crt-agents/prestashop/new-instance-setup.md` + `deployment.md` §3.
