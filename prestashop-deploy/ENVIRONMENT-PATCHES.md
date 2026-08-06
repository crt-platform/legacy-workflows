# Environment patches — why `stage-release.sh` rewrites application code

> **Read this before touching Layer 4 of `deploy/stage-release.sh`, and before
> concluding that "the repo says X but the server does Y" is a bug.**
>
> Written 2026-08-06 after a live regression on the PrestaShop dev fleet.

## TL;DR

`/yuta` promotes **`ndcmsl/prestashop` → `crt-platform/prestashop:devN` with a
force-push**. Some fixes exist only in `crt-platform` and were never upstreamed to
`ndcmsl`, so **every promotion silently reverts them**. Until they are upstreamed,
the deploy re-applies them in **Layer 4** of `stage-release.sh`.

Consequence you must internalise: for the files listed below, **what runs on the
box is deliberately not what is in the branch**.

---

## 1. The regression that prompted this

**Symptom.** On `prestashop-dev2` and `prestashop-dev4`, clicking any main-menu
link navigated to `https://prestashop.pvt.create-store.com/...` instead of staying
on the instance's own hostname. `prestashop-dev3` was fine.

**The red herring.** It looked like per-instance configuration: different vhosts,
a missing `led_shop_url` row, a stale cache. It was none of those. All three boxes
had identical Apache config, identical `led_shop_url` rows (structurally), and the
same database.

**What was actually different.** dev3 had simply **never been deployed to** — it was
still running `000-baseline` from the AMI. dev2 and dev4 had each received a
release. The correlation was exact:

| box | active release | `Shop.php` variant | menu links |
|---|---|---|---|
| dev1 | deployed | multi-entorno | ✅ own host |
| dev2 | deployed | 2018 | ❌ `prestashop.pvt` |
| dev3 | `000-baseline` | multi-entorno | ✅ own host |
| dev4 | deployed | 2018 | ❌ `prestashop.pvt` |

**Proof of causation** (not just correlation): on dev4 alone — same box, same
database, same rows, same vhost — activating `000-baseline` moved link generation
from **59 links to `prestashop.pvt` / 0 to its own host** to **0 / 55**. Nothing
else changed.

## 2. Root cause

`override/classes/shop/Shop.php` contains the block that decides which domain
PrestaShop uses when generating absolute URLs. There are two variants.

**Working** (`crt-platform`: `develop`, `master`, `dev`, `dev1`):

```php
// Multi-entorno: usar siempre el hostname del request como dominio de la shop.
if (!empty($_SERVER['HTTP_HOST'])) {
    $row['domain']     = $_SERVER['HTTP_HOST'];
    $row['domain_ssl'] = $_SERVER['HTTP_HOST'];
}
```

Always uses the request host, so every environment sharing the database generates
its own URLs without anyone touching `main=1`.

**Broken** (all of `ndcmsl`, plus `crt-platform:dev2` and `release/fix-feeds-v2`):

```php
// (JF)(25/07/2018) Para poder acceder a la misma BD desde diferentes hosts
if (strpos($_SERVER['HTTP_HOST'], 'dev') !== false) {
    $hostParts = explode('.', $_SERVER['SERVER_NAME']);
    $row['domain']     = str_replace('dev1', $hostParts[0], $row['domain']);
    $row['domain_ssl'] = str_replace('dev1', $hostParts[0], $row['domain_ssl']);
}

// Soporte para dominios bak-*.ndcmsl.xyz como failover de producción
if (Xtras::isBackDomain()) {
    $row['domain']     = $_SERVER['HTTP_HOST'];
    $row['domain_ssl'] = $_SERVER['HTTP_HOST'];
}
```

**Why it fails on the fleet.** The 2018 logic only rewrites the domain if the
matched shop-URL row's domain **literally contains the string `dev1`**. The row
PrestaShop matches for these hosts is id **267**, `prestashop.pvt.create-store.com`
— no `dev1` in it — so `str_replace` is a no-op, the domain stays `prestashop.pvt`,
and every generated link points there. (That the output is exactly `prestashop.pvt`
is what identifies row 267 as the one being matched.)

It happens to work for `prestashop-dev1` only because that box's branch still
carries the fixed variant.

## 3. Why the fix lives here and not upstream

The correct fix is to commit the multi-entorno block to **`ndcmsl/prestashop`**.
That was not available at the time, so the deploy applies it instead.

**Why the deploy stage rather than the promoter:** deploys trigger on **any push**
to a `devN` branch, not only on `/yuta`. Patching during promotion would leave
direct pushes broken. `stage-release.sh` is the last common chokepoint before code
goes live, and it already owns exactly this kind of overlay work (Layers 1–3).

**Why a targeted patch rather than shipping a whole `Shop.php`:** the file has
plenty of other content. A stored full copy would silently clobber legitimate
upstream changes to the rest of it. Layer 4 replaces only the offending block.

**Why not add `Shop.php` to the Layer 1 preserve list** (the tempting option, since
its sibling `override/classes/shop/ShopUrl.php` is already there): Layer 1 copies
from *the currently active release*, so it propagates whatever happens to be on the
box. At the time of writing dev2 and dev4 had the **broken** file active — Layer 1
would have frozen the regression in place permanently, and a fresh instance would
inherit whatever its AMI happened to carry. The fix's canonical home would become
"whatever is on that server today", which is unauditable.

## 4. Rules for anything added to Layer 4

1. **Idempotent.** It must be a no-op when the fix is already present, because it
   runs against both lineages (a baseline that has it, a promoted branch that
   doesn't) and re-runs on every deploy.
2. **Hard-fail on an unknown shape.** If the target no longer matches either known
   variant, `exit` non-zero and stop the deploy. Silently skipping is how a patch
   becomes permanently and invisibly ineffective after an upstream refactor.
3. **Anchor precisely.** The `Shop.php` patch anchors on the block containing
   `$row['domain'] = str_replace('dev1'` specifically, so it cannot touch the
   *second* 2018 block further down that rewrites `$host` for shop lookup — that
   one is still required and must survive untouched.
4. **Lint after patching.** Layer 4 runs `php -l` and fails the deploy if the
   result is not valid PHP.
5. **Log what it did.** Every deploy prints whether each patch was applied or was
   already satisfied.
6. **Give it an expiry.** Each patch below records what has to be true for it to be
   deleted.

## 5. Current patches

### 5.1 `shop-domain` — multi-environment URL generation

| | |
|---|---|
| **File** | `override/classes/shop/Shop.php` |
| **Replaces** | the 2018 `$row['domain']` block **and** the `Xtras::isBackDomain()` block that follows it |
| **With** | the multi-entorno block (always use `$_SERVER['HTTP_HOST']`) |
| **Leaves alone** | the later block that rewrites `$host` for shop lookup |
| **Symptom if missing** | every link on the page points at `prestashop.pvt.create-store.com` |
| **Marker in patched file** | `// [deploy-patch] injected by stage-release.sh` |
| **Remove when** | `ndcmsl/prestashop` carries the multi-entorno block on the branches that get promoted. Then delete the patch, promote, and confirm links still resolve to the instance hostname. |

Tested before shipping against the real files from `prestashop-dev4`: broken → patched
(byte-identical to the known-good variant apart from the marker comment), already-fixed
→ no-op, patched twice → no-op, unrecognised file → exit 1. Result passes `php -l`.

## 6. How to verify on a box

```bash
# which variant is live?
grep -q "Multi-entorno" /var/www/efectoled.com/htdocs/override/classes/shop/Shop.php \
  && echo "multi-entorno (good)" || echo "2018 logic (links will be wrong)"

# what hosts does the page actually generate?
curl -sk -H "Host: prestashop-devN.pvt.create-store.com" https://localhost/es/ \
  | grep -oE 'href="https?://[a-z0-9.-]+' | sed 's|href="https\?://||' \
  | sort | uniq -c | sort -rn | head -3
```

A healthy instance shows ~55 links to its **own** hostname. A broken one shows ~59
to `prestashop.pvt.create-store.com` and none to its own.

## 7. The wider problem this is a symptom of

`crt-platform/prestashop` and `ndcmsl/prestashop` have **diverged substantially** —
comparing `develop` on both showed ~894 differing paths, mostly theme and banner
work, but including several host/URL/config classes. `Shop.php` is the one proven
to break the fleet; **it may not be the only one**.

The durable fixes, in order of preference:

1. **Upstream the fixes to `ndcmsl`** and delete Layer 4 entirely.
2. **Change promotion from force-push to a merge**, so downstream fixes survive.
3. **Audit the divergence** for other host/URL-affecting files and decide, per file,
   whether it needs a Layer 4 patch. Scope the diff to the specific branch actually
   being promoted — a blanket `develop` vs `develop` comparison is too noisy to act on.

Until one of those happens, treat Layer 4 as **technical debt with a name**, not as
the intended architecture.
