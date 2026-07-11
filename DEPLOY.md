# Deploying args.lab980.com

The Argument Analyzer is a small Flask app streaming SSE from the Anthropic API,
run by **gunicorn** under **pm2**, proxied by **nginx**, on the lab980 droplet.
It follows the standard lab980 site shape: everything lives in `/var/www/args`,
the app listens on local port **3004** (override with the `PORT` env var).

## 1. Provision the subdomain

Infra scaffolding (DNS, app dir, clone, nginx vhost, TLS) is scripted on the droplet:

```bash
lab980-provision args ivjames/args --port 3004
```

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

## 3. Install and start the app

```bash
cd /var/www/args
python3 -m venv venv
venv/bin/pip install -r requirements.txt

pm2 start "venv/bin/gunicorn -w 1 --threads 8 --timeout 120 -b 127.0.0.1:3004 app:app" \
  --name argument-analyzer
pm2 save
```

Notes on the gunicorn flags:

- `-w 1` — single worker, per the SSE guidance (multiple workers can misbehave
  with long-lived connections).
- `--threads 8` — **required.** One sync worker with no threads serializes every
  request: a single in-flight analysis would block even page loads. Threads let
  concurrent streams and page requests coexist in the one worker.
- `--timeout 120` — analyses stream for a while; don't let gunicorn kill them.
- Bound to `127.0.0.1`, not `0.0.0.0` — only nginx needs to reach it.

`ANTHROPIC_API_KEY` is already in `/etc/environment` on the droplet; gunicorn
inherits it via pm2. If it were ever missing, the app fails fast at import time
with a `KeyError`.

## 4. Operate CLI

```bash
ln -s /var/www/args/bin/args /usr/local/bin/args

args redeploy   # git pull -> pip install -> pm2 restart
args restart
args logs
args status
```

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
