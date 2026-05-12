# gateway-py — Python pass-through proxy for Office add-ons

A standalone, Python-based variant of this proxy. It exists alongside the
TypeScript implementation in `src/` and is **not** a rewrite — it targets a
narrower use case: serving an HTTPS-with-system-cert, CORS+PNA-aware
Anthropic gateway for Office for Mac add-ons (Claude for Word, etc.).

The TypeScript proxy (`src/`) remains the canonical implementation for Claude
Code CLI with ChatGPT Plus and Kimi Code. Use whichever fits.

## What's different vs `src/`

| Concern                         | `src/` (TypeScript)                       | `gateway-py/` (Python)                                                |
|---------------------------------|--------------------------------------------|------------------------------------------------------------------------|
| Backends                        | Codex (ChatGPT) + Kimi                     | Kimi only                                                              |
| Kimi upstream                   | `api.kimi.com/coding/v1` (OpenAI fmt)      | `api.kimi.com/coding` (Anthropic fmt, pure pass-through)              |
| Upstream User-Agent             | `KimiCLI/1.37.0` by default                | `claude-cli/2.1.139` by default                                        |
| Reasoning translation           | OpenAI `reasoning_content` -> `thinking`    | None needed (upstream already emits `thinking` blocks)                |
| `GET /v1/models`                | not implemented                            | returns `claude-*` aliases so Claude Code discovery picks them up      |
| HTTPS                           | HTTP only                                  | HTTPS with mkcert-trusted local CA                                     |
| CORS                            | not applicable (CLI-only)                  | full CORS + Private Network Access middleware (browser/WebView fetch)  |
| Model rewrite                   | accepts `kimi-for-coding`/`k2.6` ids only   | rewrites any `model` (e.g. `claude-sonnet-4-5`) to `kimi-for-coding`   |
| Auth flow                       | OAuth (PKCE) + Keychain                    | API-key only                                                           |
| Daemon helpers                  | bun start / system service                 | `start.sh` / `stop.sh` with PID file                                   |

## Why HTTPS + CORS + PNA?

Office for Mac add-ons run inside WKWebView with App Transport Security
enforced -- plain `http://localhost` is silently refused before the request
leaves the app. Once you switch to HTTPS, the request reaches the gateway,
which then needs to satisfy:

1. **CORS** -- the add-on is hosted at a Microsoft origin, so any
   `fetch('https://localhost:.../')` triggers a cross-origin preflight.
2. **Private Network Access** (Chromium-based WebViews) -- a public-origin
   page fetching a private/loopback target requires
   `Access-Control-Allow-Private-Network: true` on the preflight response.

The Python implementation handles both via Starlette's `CORSMiddleware` and a
small custom PNA middleware.

## Quick start

```bash
cd gateway-py

# 1. install deps
uv sync

# 2. install a locally-trusted CA + a cert for localhost (one-time)
brew install mkcert
mkcert -install
mkdir -p certs && cd certs
mkcert -cert-file cert.pem -key-file key.pem localhost 127.0.0.1 ::1
cd ..

# 3. set your Kimi Code key
cp .env.example .env
# edit .env, set KIMI_KEY=sk-kimi-...

# 4. start as a detached daemon
./start.sh
```

You should see something like `kimi-gateway started: pid 12345, https://127.0.0.1:8765`.

Smoke test:

```bash
curl https://localhost:8765/healthz
curl https://localhost:8765/v1/models | jq .
```

## Pointing clients at it

### Claude Code CLI

```bash
ANTHROPIC_BASE_URL=https://localhost:8765 ANTHROPIC_AUTH_TOKEN=any claude
```

Or in `~/.claude/settings.json` (or a project-level `.claude/settings.json`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://localhost:8765",
    "ANTHROPIC_AUTH_TOKEN": "any-string-the-gateway-overrides-this",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"
  }
}
```

### Claude for Word (Office add-on)

In the add-on's gateway config:

| Field        | Value                            |
|--------------|----------------------------------|
| URL          | `https://localhost:8765`         |
| Token        | anything (gateway ignores it)    |
| Auth header  | `x-api-key`                      |
| API format   | `anthropic`                      |

Use `localhost`, not `127.0.0.1` -- Office tends to be hostname-strict on
ATS-evaluated certificates.

### Anything else (curl, SDK, Zed, ...)

Treat it as a plain Anthropic Messages endpoint at `https://localhost:8765`.
Any auth token is accepted from the client; the gateway swaps in the real
Kimi key before forwarding.

## Endpoints

| Method   | Path                              | Notes                                                           |
|----------|-----------------------------------|-----------------------------------------------------------------|
| `POST`   | `/v1/messages`                    | Streaming + non-streaming. Passes through to Kimi unchanged.    |
| `POST`   | `/v1/messages/count_tokens`       | Proxied to upstream.                                            |
| `GET`    | `/v1/models`                      | Returns `claude-*` aliases + `kimi-for-coding`.                 |
| `GET`    | `/healthz`, `/health`, `/`        | Liveness.                                                       |
| `OPTIONS`| `*`                               | CORS preflight + Private Network Access.                        |

## Configuration

| Env var               | Default                          | Purpose                                                     |
|-----------------------|----------------------------------|-------------------------------------------------------------|
| `KIMI_KEY`            | (required)                       | Kimi Code API key (Kimi Code Console, not Moonshot).        |
| `PORT`                | `8765`                           | Listen port.                                                |
| `BIND_HOST`           | `127.0.0.1`                      | Listen host -- keep loopback unless you trust the LAN.      |
| `KIMI_BASE`           | `https://api.kimi.com/coding`    | Upstream base URL (no trailing slash).                      |
| `UPSTREAM_USER_AGENT` | `claude-cli/2.1.139`             | UA sent to Kimi. Required to be on Kimi's allowlist.        |
| `UPSTREAM_MODEL`      | `kimi-for-coding`                | Wire model id Kimi expects.                                 |
| `REWRITE_MODEL`       | `1`                              | If set, rewrite client's `model` to `UPSTREAM_MODEL`.       |
| `SSL_CERTFILE`        | auto: `./certs/cert.pem`         | TLS cert. If unset and no cert in `./certs/`, serves HTTP.  |
| `SSL_KEYFILE`         | auto: `./certs/key.pem`          | TLS key. Should be `chmod 600`.                             |

## Notes

- **Kimi's client allowlist** -- the `/coding` endpoint enforces an
  `access_terminated_error` for unrecognized User-Agents. The `claude-cli/*`
  UA is on the allowlist. Kimi's TOS asks you to maintain the real client
  identifier; you're acting as Claude Code, which is itself allowlisted.
- **Loopback only** -- the gateway accepts any client-side auth token and
  forwards using the gateway's own `KIMI_KEY`. Don't expose this on a LAN
  without adding real auth.
- **Architecture** -- pure HTTP pass-through: no Anthropic <-> OpenAI
  translation, no reasoning_content remapping, no streaming re-encoding. The
  proxy mostly just rewrites two headers (auth, UA) and one field (`model`).
