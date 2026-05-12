# Fork changes

This document records the improvements this fork (`bre01/claude-code-proxy`)
adds on top of the upstream [`raine/claude-code-proxy`][upstream].

[upstream]: https://github.com/raine/claude-code-proxy

The upstream TypeScript proxy in [`src/`](src/) is **untouched**. All
additions live under [`gateway-py/`](gateway-py/) plus a small note in the
root README and a few `.gitignore` entries. Pulling future upstream changes
should remain a clean fast-forward.

## Motivation

The Office for Mac Word add-on "Claude for Office" exposes a "Gateway"
configuration field that takes an Anthropic-compatible URL. Pointing it at
the upstream proxy (`http://localhost:<port>`) failed with the generic
WKWebView error `Load failed (127.0.0.1:8765)` and never even opened a TCP
connection to the proxy.

Root cause investigation showed three independent blockers, each of which
this fork addresses:

1. **App Transport Security** — Office for Mac's WKWebView silently refuses
   any `http://` URL before the request leaves the app, regardless of
   whether the target is loopback.
2. **CORS preflight** — once on HTTPS, the add-on (hosted at a
   `*.officeapps.live.com` origin) sends a cross-origin preflight that the
   upstream proxy doesn't answer because it was built for CLI clients.
3. **Private Network Access** — Chromium-based WebViews additionally
   require `Access-Control-Allow-Private-Network: true` on the preflight
   when a public-origin page targets a private/loopback host.

## What this fork adds

### 1. A standalone Python gateway: `gateway-py/`

A new Python implementation that runs alongside the TypeScript one. ~300
lines, built on FastAPI + httpx. Not a port — it targets the narrower Office
add-on use case and **only** speaks to Kimi Code; Codex/ChatGPT support is
deliberately out of scope.

### 2. HTTPS with a locally-trusted cert

The proxy serves TLS using a cert signed by mkcert's local CA, which is
installed in the macOS system trust store. The script `start.sh` auto-detects
`./certs/cert.pem` + `./certs/key.pem` and switches uvicorn to HTTPS mode
when both are present. Falls back to HTTP transparently otherwise (useful
for non-Office clients that don't need ATS-grade trust).

### 3. CORS + Private Network Access middleware

Two layered middlewares:

- Starlette's `CORSMiddleware` with wildcard origin/methods/headers and a
  24-hour max-age for actual cross-origin requests.
- A custom `PrivateNetworkAccessMiddleware` that runs **outside**
  `CORSMiddleware` (Starlette runs middleware in reverse registration
  order — the PNA-handling middleware must register last) so it can short-
  circuit PNA preflights before `CORSMiddleware`'s default-reject behavior
  kicks in. Responds 204 with the PNA header echoed and the request's
  `Origin` mirrored back.

### 4. Pure Anthropic pass-through

The upstream proxy targets `https://api.kimi.com/coding/v1` (Kimi's
OpenAI-compatible endpoint) and does Anthropic ↔ OpenAI translation for both
request and response, including converting `reasoning_content` deltas into
Anthropic `thinking` content blocks.

This fork talks to `https://api.kimi.com/coding/` (Kimi's
**Anthropic-compatible** endpoint), which means:

- No format translation. Request body forwarded mostly verbatim; response
  bytes streamed straight back via `aiter_raw()`.
- `thinking` blocks are produced by Kimi directly, eliminating one entire
  class of "format drift" bugs.
- Implementation surface is much smaller (~280 lines vs upstream's
  multi-module TS package).

To pass Kimi's client allowlist (`access_terminated_error` otherwise), the
proxy sets `User-Agent: claude-cli/2.1.139` upstream, matching what real
Claude Code CLI sends. The upstream proxy sends `KimiCLI/1.37.0` instead,
which is also on the allowlist but a different code path on Kimi's side.

### 5. `GET /v1/models` endpoint

Returns a list of `claude-*` aliases (opus, sonnet, haiku) plus
`kimi-for-coding`. The upstream proxy doesn't implement `/v1/models`.

Claude Code's model-discovery feature (enabled with
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`) calls this endpoint at
startup and filters to ids beginning with `claude` or `anthropic`. Without
matching aliases, the gateway's model wouldn't show up in the `/model`
picker.

### 6. Model field rewriting

Any incoming `model` value (e.g. `claude-sonnet-4-5`, `claude-opus-4-1`,
even raw `kimi-for-coding`) is rewritten to the configured
`UPSTREAM_MODEL` (default `kimi-for-coding`) before forwarding. The
upstream proxy accepts only the three exact wire ids (`kimi-for-coding`,
`kimi-k2.6`, `k2.6`) and returns HTTP 400 for anything else.

This lets unaware clients (Claude for Word, Zed, Claude Desktop via base-url
override) send their default `claude-sonnet-4-5` / `claude-opus-4-1` and
have the gateway transparently route to Kimi.

### 7. Daemon helpers

`start.sh` and `stop.sh` shell scripts with PID-file management. `start.sh`:

- Sources `.env` if present.
- Auto-detects TLS certs.
- Refuses to start if a previous PID is alive.
- Probes `/healthz` to confirm the server came up, prints the listen URL.

`stop.sh`:

- Sends SIGTERM, polls for graceful shutdown, escalates to SIGKILL after 2 s.
- Cleans up the PID file even when the PID isn't alive.

## What this fork does **not** do

- Touch any file under `src/` or the upstream's `package.json` / `bun.lock`.
- Add Codex / ChatGPT Plus support to the Python implementation.
- Provide a local tokenizer for `/v1/messages/count_tokens` (upstream does;
  the Python implementation round-trips to Kimi).
- Implement OAuth flows or Keychain-backed token storage (upstream does;
  the Python implementation is API-key-only).
- Change the upstream's behavior on `kimi-for-coding`. Use whichever
  implementation fits.

## File map

```
.
├── FORK-CHANGES.md         (this file)
├── README.md               (intro note prepended; rest is upstream)
├── .gitignore              (added entries for gateway-py)
├── src/                    (upstream TypeScript proxy — untouched)
├── gateway-py/             (new — Python proxy)
│   ├── README.md           (setup, configuration, comparison table)
│   ├── server.py           (~280 lines)
│   ├── pyproject.toml      (httpx + fastapi + uvicorn)
│   ├── start.sh / stop.sh  (daemon helpers)
│   └── .env.example
└── ...rest unchanged from upstream
```

## Pulling future upstream changes

```bash
git fetch upstream
git checkout main
git merge upstream/main      # should be conflict-free; touched files are
                              # additive only and don't overlap upstream's
git push origin main
```

If upstream adds files under any of the names this fork uses
(`gateway-py/`, `FORK-CHANGES.md`), the merge will conflict on those paths
and need a manual resolution. Otherwise the merge should fast-forward
cleanly.
