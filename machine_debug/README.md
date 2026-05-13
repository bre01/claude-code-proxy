# machine_debug

Notes from real debug sessions of `gateway-py/` against upstream
Anthropic-compatible vendors (Kimi, DeepSeek). Practices, gotchas, and
vendor-specific behaviors that were paid for in confusion the first time.

Each entry is dated and stands alone — read whichever is relevant when you
hit a similar symptom.

## Index

- [2026-05-13 — Gateway request capture pattern](./2026-05-13-gateway-request-capture.md) — minimal `print`-based dump in `gateway-py/server.py` for diagnosing vendor errors in one round trip.
- [2026-05-13 — Kimi for-coding tool-call loop in non-coding harnesses](./2026-05-13-kimi-coding-tool-loop.md) — why `kimi-for-coding` hallucinates `web_search` calls in Word add-in (and why prompt mitigation can't fix it).
