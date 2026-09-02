# Deploying the dashboard to production

Runbook for putting `dashboard.py` on a real, always-on URL. Written for the
2026/27 season cut-over.

---

## 0. Which host — and why not the other three

You have four accounts. Only one of them can run this app.

| Host | Verdict |
|---|---|
| **Fly.io** | ✅ **Use this.** Runs a real container with a persistent disk, which is exactly what a Streamlit app that shells out to scrapers and writes SQLite needs. |
| **Netlify** | ❌ Static hosting + short-lived serverless functions. Streamlit is a long-lived Python process holding a websocket per browser tab. There is no configuration that makes this work. |
| **Cloudflare** | ❌ as a host — Workers don't run CPython with pandas/lightgbm/Chromium. ✅ **as the front door**: DNS, TLS, and Cloudflare Access for the login gate. Use it for that. |
| **Streamlit Community Cloud** | Works (it's what the `_is_streamlit_cloud()` / GitHub-sync machinery in `dashboard.py` was built for) but you've ruled it out. Worth keeping as a fallback URL. |

So: **app on Fly.io, hostname and auth on Cloudflare, Netlify unused.**

---

## 1. Do these before the app is public

### 1a. The repo is public and contains your betting identity

`https://github.com/tbh-brett/hk_race_dashboard` is **public**. Currently
committed and world-readable:

- `acctstmt (22 April).txt`, `acctstmt (26 April).txt` — HKJC **account number
  40067995** and running balance.
- `reports/user_bets_log.jsonl` — your real wager log.
- `blackbook.json` — your entire private horse watchlist, the thing the model's
  BB flag is built on.
- `dashboard.py:17771` — the My Bets password, `mighty_commander`, in cleartext.

Anyone reading the repo can open the My Bets page on any deployment you make.
Do this first:

1. **Make the repo private** (Settings → General → Danger Zone). The GitHub
   Contents API sync in `dashboard.py` keeps working unchanged — a PAT with
   `repo` scope reads and writes private repos fine.
2. **Rotate the My Bets password** out of the source and into a secret. It
   should read from `st.secrets` / env, not a string literal. Making the repo
   private is *not* a fix on its own: the password is in git history, and the
   history is already public.
3. **Delete the account statements from the repo.** They're parser fixtures for
   `parse_acct_statement.py`, not runtime inputs — `.dockerignore` already
   keeps them out of the image. Scrub them from history with
   `git filter-repo` if you care about the already-published copies.
4. Assume any `GITHUB_TOKEN` that has ever sat in a public commit is burned.
   Issue a fresh fine-grained PAT scoped to this one repo.

### 1b. There is no app-level login

Only the My Bets page is gated. Every other page — model output, blackbook,
edges, P/L charts — is open to anyone with the URL. A `*.fly.dev` hostname is
public and enumerable. Section 4 puts Cloudflare Access in front, which is the
real fix; don't skip it.

### 1c. Python floor

`dashboard.py` uses PEP 701 f-strings (backslashes inside the expression part,
~30 sites). **It is a `SyntaxError` on Python 3.11 and below.** The Dockerfile
pins `python:3.12-slim-bookworm`. Verified:

```
python3.11 -m py_compile dashboard.py   # SyntaxError at line 14073
python3.12 -m py_compile dashboard.py   # OK
```

Don't move the base image without re-running that check. The upper bound is
real too — the sqlite fast paths in `_load_form_db` carry comments about
segfaults under Python 3.14 / pandas 3.0, which is why `constraints.txt`
holds pandas below 3.

---

## 2. First deploy

```bash
fly auth login
fly launch --no-deploy          # names the app, writes nothing we don't already have
```

Accept the app name or edit `app = "hkjc-dashboard"` in `fly.toml` to match.

Create the volume — **one machine, one volume**, in the same region as
`primary_region`:

```bash
fly volumes create hkjc_data --region hkg --size 3
```

3 GB is comfortable: the committed data set (`hkjc.db` 17 MB, `reports/` 83 MB,
`cache/` 42 MB, `running_position_photos/` 94 MB) is ~240 MB, and it grows by a
few MB per meeting.

Set secrets (these never go in the repo):

```bash
fly secrets set GITHUB_TOKEN="github_pat_..."     # fine-grained, this repo, contents:write
fly secrets set NTFY_TOPIC="your-private-uuid"    # push notifications
```

Deploy:

```bash
fly deploy
fly logs                                          # watch for "[entrypoint] volume ready"
fly open
```

First boot copies the image's committed data into the empty volume, then
symlinks `/app/reports`, `/app/hkjc.db`, etc. to `/data/...`. Every later boot
leaves the volume alone — **the volume is the source of truth**, so a deploy
carrying an older committed `hkjc.db` can't roll back live data. That's the
regression from commit `b14d292`, handled structurally.

To deliberately re-seed from a newly committed snapshot:

```bash
fly ssh console -C "rm -rf /data/hkjc.db"   # then: fly apps restart hkjc-dashboard
```

---

## 3. Sanity checks on the live URL

- Sidebar **Cloud Persistence** panel reports `OK (push permission verified)`.
  If it says `no token configured`, the secret didn't land.
- **Race Day Insight** → readiness strip shows SARR ✓ / ET ✓.
- **Model Analysis** → `[ RUN ANALYSIS ]` completes all three steps. This is
  the heaviest thing the box does — a Streamlit process holding form-guide
  DataFrames *plus* a subprocess spawning its own Python and Chromium. If it
  dies, `fly logs` will show an OOM kill; raise `memory` in `fly.toml`.
- **Results** → *Scrape Results* writes and the file survives
  `fly apps restart`.

---

## 4. Put Cloudflare Access in front

This is what turns "a URL on the internet" into production.

1. Cloudflare dashboard → **Zero Trust** → Access → Applications → *Add a
   self-hosted application*.
2. Domain: a subdomain you own, e.g. `racing.yourdomain.com`.
3. Policy: *Allow* → **Emails** → your address. That's the whole allowlist.
4. Point the DNS record at the Fly app and register the hostname with Fly:
   ```bash
   fly certs add racing.yourdomain.com
   ```
   Follow the `fly certs show` instructions for the CNAME / A records, and set
   the Cloudflare record to **Proxied** (orange cloud) so Access actually sits
   in the path.
5. Close the back door: with the app also reachable at `hkjc-dashboard.fly.dev`,
   Access is trivially bypassed. Either restrict Fly to Cloudflare's IP ranges,
   or run the machine on Flycast private networking with a `cloudflared` tunnel
   as the only ingress. Until one of those is done, treat the `.fly.dev`
   hostname as the real security boundary — which is to say, treat it as none.

Cloudflare's proxy must allow websockets for Streamlit. It does by default;
don't disable it.

---

## 5. Ongoing

**Deploys.** `git push` does nothing on its own — there are no GitHub Actions
workflows in this repo. Deploying is `fly deploy` from your machine. To
automate: add `.github/workflows/deploy.yml` running `superfly/flyctl-actions`
with a `FLY_API_TOKEN` secret from `fly tokens create deploy`. Note that the
app itself commits to `main` (`blackbook: auto-sync ...`), so gate any
auto-deploy on `[skip ci]` or it will redeploy itself several times a meeting.

**Backups.** Two independent layers, keep both: Fly volume snapshots (14-day
retention, set in `fly.toml`) and the app's own GitHub Contents API sync. The
GitHub sync is the one that has actually saved you before — check the sidebar
panel occasionally.

**Cost.** One always-on `shared-cpu-2x` / 4 GB machine plus a 3 GB volume is
roughly US$25–30/month at current Fly pricing. Dropping to 2 GB roughly halves
the machine cost but risks OOM during `RUN ANALYSIS`. Do not "save money" with
`auto_stop_machines` — a machine that suspends mid-scrape loses the run.

**The `_is_streamlit_cloud()` name is now a misnomer.** On Fly it returns
`True` (via `STREAMLIT_SERVER_HEADLESS`), which is the behaviour you want — it
selects the sqlite fast path and keeps the GitHub sync on. Worth renaming to
`_is_container()` at some point so the next person isn't misled.
