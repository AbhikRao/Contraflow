# Public demo deployment

The live demo is served from the developer's machine through **Tailscale Funnel**: a single HTTPS URL forwards to one
local port. FalkorDB and the local model (Ollama) are never exposed.

```
internet ──HTTPS──▶ Tailscale Funnel ──▶ 127.0.0.1:8008  deploy_public/gateway.py  (container)
                                                              │
                                     dedicated FalkorDB (container, password, no host port)
                                     local Ollama (loopback, reached via one fixed endpoint)
```

## What the gateway enforces (`deploy_public/gateway.py`)

- **Isolation per visitor.** Each visitor gets a signed HttpOnly session cookie (`Secure` over HTTPS) and **private
  copies** of the road graph and of the investigation-memory graph (`pub_<sid>_road`, `pub_<sid>_memory`). Visitors never
  see each other's investigations, case files or decisions. Copies are deleted when the session expires.
- **Route allowlist.** Only the UI and the API routes it uses are served. `/docs`, `/openapi.json`, MCP and model
  endpoints return 404.
- **Request safety.** Origin check on every POST (CSRF), host check, 32 KiB body limit, bounded scenario sizes, and
  per-visitor and global quotas.
- **One model job at a time.** A concurrent request gets HTTP 429 with a retry hint.
- **Security headers.** CSP, HSTS, `X-Frame-Options: DENY`. The health endpoint reports nothing internal.
- **Crawler-safe sessions.** Public hostnames get crawled. When the session table is full, sessions that never used the
  API are evicted first, then the least recently seen idle one, so crawlers cannot lock real visitors out.
- **Read-only with respect to everyone else.** Visitors change only their own scenario, which the client holds, and
  their own memory copy. There is no endpoint that administers, deletes or reaches developer data.

## Run it yourself

Requirements: Docker, Ollama with `qwen2.5:7b`, and (for public exposure) a Tailscale account with Funnel enabled.

```bash
mkdir -p "$HOME/Library/Application Support/ContraflowPublic"
CFG="$HOME/Library/Application Support/ContraflowPublic/deployment.env"
printf 'PUBLIC_DB_PASSWORD=%s\nPUBLIC_ORIGIN=http://127.0.0.1:8008\n' "$(openssl rand -hex 24)" > "$CFG"; chmod 600 "$CFG"
docker compose --env-file "$CFG" -f deploy_public/compose.yml up --build -d
python deploy_public/verify.py --live          # local checks against http://127.0.0.1:8008
```

`deploy_public/compose.yml` sets `CONTRAFLOW_LLM_BASE_URL=http://host.lima.internal:11434/v1`, which is the host
address under Colima. With Docker Desktop, change it to `http://host.docker.internal:11434/v1`.

To publish it, set `PUBLIC_ORIGIN` to your Funnel URL (`https://<machine>.<tailnet>.ts.net`), recreate the app
container, then run `tailscale funnel --bg 8008`. Turn it off with `tailscale funnel --https=443 off`.
`deploy_public/manage.sh` wraps the status, logs and restart commands.

## Verification records

- `deploy_public/verification.json`: 59/59 local checks.
- `deploy_public/verification_public.json`: 62/62 checks over the public HTTPS URL. These include real Qwen streaming,
  case files, Test assumptions, cross-visitor isolation and crawler-flood resilience.

## Limitations

- It is a laptop. Availability depends on the machine staying awake and online.
- Quotas are per session and global, not per IP. Heavy traffic can exhaust the model allowance and slow the demo,
  but cannot reach other visitors' data or any developer state.
