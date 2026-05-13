# machine_debug

Notes from real debug sessions of `gateway-py/` against upstream
Anthropic-compatible vendors (Kimi, DeepSeek). Practices, gotchas, and
vendor-specific behaviors that were paid for in confusion the first time.

Each entry is dated and stands alone — read whichever is relevant when you
hit a similar symptom.

## Index

- [2026-05-13 — Gateway request capture pattern](./2026-05-13-gateway-request-capture.md)
  When the Word add-in or any third-party client says "connection failed"
  but you've already verified TLS / DNS / CORS, the next step is *seeing
  exactly what bytes the client sent and the upstream returned*. This is the
  minimal `print`-based capture mode we inserted into `gateway-py/server.py`
  and the kinds of upstream errors it surfaces.
- [2026-05-13 — Kimi for-coding tool-call loop](./2026-05-13-kimi-coding-tool-loop.md)
  Why `kimi-for-coding` keeps hallucinating `web_search` / `SearchWeb`
  calls inside Word add-in (a non-coding harness), why detailed
  Anthropic-written guidance prompts don't fix it, and the gateway-side
  deterministic mitigation.
