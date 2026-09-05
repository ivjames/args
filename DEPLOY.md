# Deploying args.lab980.com

The Argument Analyzer is a small Flask app streaming SSE from the Anthropic API,
run by **gunicorn** under **pm2**, proxied by **nginx**, on the lab980 droplet.
It follows the standard lab980 site shape: everything lives in `/var/www/args`,
the app listens on local port **3004** (the CLI's `ARGS_PORT` override changes
both the probe and the port baked into a *first* `pm2 start`; a registered
process keeps its port until `pm2 delete` + `args deploy`).

The pm2 process is named **`argument-analyzer`**, not `args` — `pm2 ls` shows
that name, and so does `args status`.

## 1. Provision the subdomain

Infra scaffolding (DNS, app dir, clone, nginx vhost, TLS) is scripted on the droplet:

```bash
provision-site args ivjames/args --port 3004
```

`--port 3004` is not optional: without it `provision-site` picks the next free
port from 8060 and writes *that* into the vhost, while this repo's CLI and the
gunicorn bind use 3004 — nginx then proxies to a port nothing listens on.

## 2. nginx: make sure SSE isn't buffered

The `/analyze` endpoint streams Server-Sent Events. The nginx location block for
this site must disable proxy buffering or the stream arrives in one lump at the
end. The full server block (pre-certbot, HTTP only) should look like:

```nginx
server {
    listen 80;
    server_name args.lab980.com;

    location / {
        proxy_pass http://127.0.0.1:3004;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # SSE-specific
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding on;
    }
}
```

If the provision script generated a plainer block, add the SSE lines, then
`nginx -t && systemctl reload nginx`. (The app also sends `X-Accel-Buffering: no`,
which disables buffering per-response, but keep the config explicit.)

## 3. `.env`, then the first deploy

The app reads `ANTHROPIC_API_KEY` from `os.environ` at import time (it fails
fast with a `KeyError` without it). The **only** place that key lives is
`/var/www/args/.env` (gitignored, mode 600) — there is no box-level key store:
the shell keys were removed from `/etc/environment` on 2026-09-05 and a root
login shell carries no API key. `bin/args` refuses to start or restart the app
while `.env` lacks the key, and `args status` reports whether it is present
(names only, never values).

```bash
cd /var/www/args
install -m 600 /dev/null .env
$EDITOR .env                      # ANTHROPIC_API_KEY=sk-ant-…  (see .env.example)
ln -sf /var/www/args/bin/args /usr/local/bin/args
args deploy                       # venv + pip, first pm2 start, probe, save
```

`args deploy` creates `venv/` if it is missing, installs `requirements.txt`,
and — seeing nothing named `argument-analyzer` registered with pm2 — runs the
`pm2 start` in `START_CMD` at the top of `bin/args`, which reproduces the live
registration on the droplet:

```
pm2 start /usr/bin/bash --name argument-analyzer --interpreter none -- \
  -c 'set -a; . ./.env; set +a; exec venv/bin/gunicorn -w 1 --threads 8 --timeout 120 -b 127.0.0.1:3004 app:app'
```

That is: pm2 runs bash, bash sources `.env` and execs gunicorn. The sourcing
is what gets `ANTHROPIC_API_KEY` to the app, because every pm2 call the CLI
makes is **scrubbed** — `env -i` plus `PATH`, `HOME`, `LANG`, `PM2_HOME`/`TERM`
if set, and `PORT` — so nothing from the shell that ran `deploy` reaches pm2,
the process, or `~/.pm2/dump.pm2`. Never `pm2 start` or `pm2 restart
--update-env` by hand from a login shell; use the CLI.

Notes on the gunicorn flags:

- `-w 1` — single worker, per the SSE guidance (multiple workers can misbehave
  with long-lived connections).
- `--threads 8` — **required.** One sync worker with no threads serializes every
  request: a single in-flight analysis would block even page loads. Threads let
  concurrent streams and page requests coexist in the one worker.
- `--timeout 120` — analyses stream for a while; don't let gunicorn kill them.
- Bound to `127.0.0.1`, not `0.0.0.0` — only nginx needs to reach it.

`pm2 save` runs only after the local probe passes and only when every
registered pm2 process is `online` (the box rule); otherwise the CLI warns and
leaves the previous dump alone. Reboot survival additionally needs the
once-per-droplet boot hook (`pm2 startup systemd -u root --hp /root`, then the
line it prints; check `systemctl is-enabled pm2-root`).

### Persistence & rate limiting (Phase 2)

- Analyses are saved to **SQLite** at `/var/www/args/data/analyses.db` (created
  automatically on first run; the `data/` dir is git-ignored). Each analysis gets
  a short slug served back at `https://args.lab980.com/a/<slug>`. Back it up with
  `cp data/analyses.db data/analyses.db.bak` (WAL mode, so also copy `-wal`/`-shm`
  if present, or checkpoint first).
- `/analyze` is **rate-limited** to 6/min and 40/day per client IP (Flask-Limiter,
  in-memory — counts reset on `pm2 restart`). The `X-Forwarded-For` header added
  to the nginx block above is required so the limiter sees the real client IP
  instead of `127.0.0.1`; `ProxyFix` in the app reads it. Confirm after deploy
  that limits key per-visitor, not globally.
  - Note: the limiter's in-memory store is **unrelated** to the analyses SQLite
    DB above. Flask-Limiter stores counters via the `limits` library, which only
    supports memory / redis / valkey / memcached / mongodb — there is no SQLite
    backend, so the DB file can't serve as its store. In-memory is the right
    choice for this single-worker deployment. To make limits survive restarts or
    span multiple workers, point `storage_uri` at a Redis/valkey instance (a new
    service to run) or replace Flask-Limiter with a hand-rolled check against the
    existing SQLite DB. Neither is worth it at current scale.

### Cost tracking

- Every analysis records token usage and cost to the `usage_stats` table in the
  same `data/analyses.db` (linked to the analysis by `slug`). The page shows a
  rough client-side estimate (≈4 chars/token, no API call) for both input and the
  streaming output as it arrives; the **actual** input/output token counts come
  from the streamed message's `usage` and replace the estimates on completion.
  Only the actuals are recorded to the DB.
- **Pricing** is set by two env vars, defaulting to claude-sonnet-5's **standard**
  rate (`$3` in / `$15` out per million tokens): `PRICE_IN_PER_MTOK` and
  `PRICE_OUT_PER_MTOK`. (The intro rate of `$2` / `$10` expired 2026-08-31.) If
  pricing changes again, set these in `.env` and `args restart` (the bash
  wrapper sources `.env` on every start, so the new values reach gunicorn).
- `GET /stats` returns aggregate JSON (analysis count, total tokens, total cost,
  and a per-mode breakdown). It's **unauthenticated** — if you don't want site
  usage/cost public, restrict it in nginx (e.g. `location = /stats { deny all; }`
  or an `auth_basic`). No per-analysis content is exposed, only totals.

## 4. Operate CLI

```bash
ln -sf /var/www/args/bin/args /usr/local/bin/args

args deploy [--no-install]   # sync to origin/main, venv + pip, pm2 start/restart, probe, save
args redeploy                # alias of deploy (the old name, kept for muscle memory)
args restart                 # pm2 restart + probe (re-reads .env via the wrapper)
args logs [-n N]             # tail this app's pm2 logs
args status                  # HEAD, pm2 state, .env key presence, local + public probe, cert days
```

**How `deploy` syncs:** `git fetch` then `git reset --hard origin/main`. A
tracked file edited on the droplet is destroyed silently on the next deploy —
fix it in the repo. The gitignored state survives and is meant to be edited
on the box: `.env`, `venv/`, `data/`.

`deploy` exits non-zero when nothing answers HTTP on `127.0.0.1:3004`
afterwards (any status code counts; up to `ARGS_PROBE_TRIES`, default 10,
tries a second apart) — a dead app is a failed deploy, and nothing is saved.
The public probe is printed alongside but does not decide the result, since it
also depends on DNS and TLS.

Overrides: `ARGS_FQDN` (default `args.lab980.com`), `ARGS_BRANCH` (default
`main`), `ARGS_PORT` (default `3004`), `ARGS_PROBE_TRIES` (default `10`).

## 5. Smoke test

```bash
# Page loads
curl -sS http://127.0.0.1:3004/ | head -5

# Validation
curl -sS -X POST http://127.0.0.1:3004/analyze \
  -H 'Content-Type: application/json' -d '{"argument_a": "", "argument_b": ""}'
# -> {"error": "Both arguments are required."} with HTTP 400

# Streaming (watch chunks arrive incrementally, ending with data: [DONE])
curl -N -X POST http://127.0.0.1:3004/analyze \
  -H 'Content-Type: application/json' \
  -d '{"argument_a": "Everyone I know likes it, so it must be good.", "argument_b": "It has a 4.8 rating across 10k reviews, so most users like it."}'
```

Then the same via `https://args.lab980.com` once DNS + certbot are done —
confirm the stream renders progressively in the browser, not all at once
(if it lumps, revisit step 2).
