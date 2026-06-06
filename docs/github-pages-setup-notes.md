# GitHub Pages + Custom Domain Setup Notes

> How `https://snarky-squirrel.hemantkumar.dev` was set up for this repo.

---

## What was done

The repo `hkcodebase/snarky-squirrel` is published via GitHub Pages with a custom
subdomain hosted on AWS Route 53.

---

## Files added to the repo

| File | Purpose |
|------|---------|
| `index.html` | Landing page served by GitHub Pages |
| `CNAME` | Tells GitHub Pages which custom domain to serve on |

Both files live at the **root of the `gh-pages` branch**.

### `CNAME` contents

```
snarky-squirrel.hemantkumar.dev
```

---

## DNS — AWS Route 53

A single CNAME record was added to the `hemantkumar.dev` hosted zone:

| Field | Value |
|-------|-------|
| Record type | `CNAME` |
| Name | `snarky-squirrel` |
| Value | `hkcodebase.github.io.` (trailing dot matters) |
| TTL | `300` |

Verify it's working at any time:

```bash
dig snarky-squirrel.hemantkumar.dev
# Should show CNAME → hkcodebase.github.io → 185.199.x.x
```

---

## GitHub Pages settings

In the repo: **Settings → Pages**

| Setting | Value |
|---------|-------|
| Source | Deploy from branch |
| Branch | `gh-pages` / `/ (root)` |
| Custom domain | `snarky-squirrel.hemantkumar.dev` |
| Enforce HTTPS | ✅ Enabled |

GitHub automatically provisions a TLS certificate via Let's Encrypt once DNS
is verified. If the cert is slow to appear, remove and re-add the custom domain
in Settings → Pages to trigger a fresh DNS check.

---

## Deployment workflow

To update the landing page:

```bash
git checkout gh-pages
# edit index.html
git add index.html
git commit -m "docs: update landing page"
git push origin gh-pages
```

Changes go live within ~1 minute.

To set up from scratch on a new machine:

```bash
git clone https://github.com/hkcodebase/snarky-squirrel.git
cd snarky-squirrel
git checkout gh-pages
# CNAME and index.html should already be here
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Connection not secure" / cert warning | DNS is correct but cert not yet issued — wait 5–15 min after GitHub shows DNS as verified |
| GitHub DNS check stuck | Remove custom domain in Settings → Pages, save, re-enter it, save again |
| Page not found (404) | Check the branch is `gh-pages` and `index.html` is at the root |
| Old content still showing | Hard-refresh (`Cmd/Ctrl + Shift + R`) or wait for CDN cache to clear |

---

## How GitHub Pages URLs work

| Repo | Pages URL |
|------|-----------|
| `hkcodebase/hkcodebase.github.io` | `https://hkcodebase.github.io` |
| `hkcodebase/snarky-squirrel` | `https://hkcodebase.github.io/snarky-squirrel/` |
| Any repo + CNAME + DNS | `https://your-custom-domain.com` |

A custom domain at `https://snarky-squirrel.github.io` would require a GitHub
account or org named `snarky-squirrel` with a repo named `snarky-squirrel.github.io`.
