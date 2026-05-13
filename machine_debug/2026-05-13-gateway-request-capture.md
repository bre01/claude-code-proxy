# Gateway request capture pattern

> Date: 2026-05-13
> Context: bringing up `gateway-py/` against DeepSeek's Anthropic-compatible
> endpoint for the Claude for Office (Word) Mac add-in.

## Symptom

Word add-in connects to the gateway URL successfully (the "Test connection"
button is green), but the actual conversation fails silently — the user
sends a message and the add-in shows an error toast or just spins.

The gateway log shows requests arriving but nothing else useful — it's a
pure pass-through, so by default it has no clue what was inside the bodies.

## The pattern

Add a one-shot "capture mode" to the proxy: at request entry, dump method,
path, headers, and the first ~1.5 KB of the body; at upstream response,
dump status, headers, and the first ~2 KB of the body. For streaming
responses, capture the first ~4 KB of the SSE stream without breaking
the pipe to the client.

Total addition: ~30 lines of `print(..., flush=True)`. No logging
framework. Pipes straight into the existing `nohup` log file.

```python
# inside async def proxy(request, path) in server.py
raw_body = await request.body()
body = maybe_rewrite_body(raw_body)
streaming = is_streaming(body)
print(
    f"\n[CAPTURE >>>] {request.method} {request.url.path}  stream={streaming}\n"
    f"  inbound headers: { {k:v for k,v in request.headers.items() "
    f"if k.lower() not in ('authorization','x-api-key','cookie')} }\n"
    f"  inbound body[:1500]: {raw_body[:1500]!r}\n"
    f"  forwarded body[:1500]: {body[:1500]!r}\n"
    f"  upstream URL: {url}\n"
    f"  upstream headers: "
    f"{ {k:('Bearer …' if k=='Authorization' else v) for k,v in headers.items()} }",
    flush=True,
)

# ... after upstream response ...
print(
    f"[CAPTURE <<<] upstream status: {resp.status_code}\n"
    f"  upstream resp headers: {dict(resp.headers)}\n"
    f"  upstream body[:2000]: {resp.content[:2000]!r}",
    flush=True,
)
```

For streaming responses, sniff the first N bytes inside the generator
without buffering the entire stream:

```python
async def stream_iter():
    try:
        captured = bytearray()
        async for chunk in resp.aiter_raw():
            if len(captured) < 4000:
                captured.extend(chunk[: 4000 - len(captured)])
            yield chunk
        print(f"[CAPTURE <<<] sse first 4KB: {bytes(captured)!r}", flush=True)
    finally:
        await resp.aclose()
```

## What it caught (this session)

The "Word checked the gateway successfully but can't have a real
conversation" symptom resolved to a single line in the capture output:

```
[CAPTURE >>>] POST /v1/messages  stream=True
[CAPTURE <<<] upstream status: 400
[CAPTURE <<<] error body[:2000]: b'{"error":{"message":"Failed to
  deserialize the JSON body into the target type: tools[0]: unknown
  variant `custom`, expected `web_search_20250305` or
  `web_search_20260209` at line 1 column 131294","type":
  "invalid_request_error","param":null,"code":"invalid_request_error"}}'
```

Diagnosis was immediate: DeepSeek's Anthropic-compatible layer doesn't
understand Anthropic's `"type":"custom"` tool variant, only the
server-side `web_search_*` types. Word's add-in declares all its Word
editing tools with `"type":"custom"`. Fix: have the gateway strip the
`type` field from every tool before forwarding (~5 lines in
`maybe_rewrite_body`). Word conversations worked on the next request.

Without the capture, the only visible signal was "Word says it failed",
which could've been TLS, CORS, PNA, model name, auth, schema, or rate
limit. The dump narrowed it to schema in one round-trip.

## Watching it live

The `nohup` log file gets appended to by the print statements. A long
running monitor with a tight filter shows just the events you care about:

```bash
tail -F -n0 /Users/bre/bin/deepseek-gateway/kimi-gateway.log \
  | grep -E --line-buffered "CAPTURE|ERROR|Traceback|error|fail"
```

Or via the Claude Code `Monitor` tool with the same command — each grep
hit becomes a notification, no polling.

## Cleanup / always-on mode

Verbose body dumps are fine during debugging but unnecessary in steady
state — a 130 KB inbound body times every request bloats the log
quickly. Three sensible modes:

- **off**: ship it. Default.
- **on errors only**: dump body only when upstream returns `status >= 400`.
  Headers/status always logged at one line per request.
- **full (debug)**: what's shown above. Keep the env var `DEBUG_CAPTURE=1`
  guard so you can flip it without redeploying code.

A future cleanup pass should gate the verbose branch on
`os.environ.get("DEBUG_CAPTURE")` and always log a one-liner per request
(method, path, status, latency, token count if available).

## Gotchas

- **Don't log Authorization / x-api-key**. The snippet above filters them
  out. Easy to forget and accidentally leak a key into a chat transcript.
- **`!r` on bytes**: `repr(bytes)` preserves all escapes — useful for
  spotting hidden BOMs, control characters, weird unicode. `.decode()`
  would lose this.
- **First N bytes, not all**: streaming responses can be megabytes. Bounded
  buffer (4 KB here) is enough to see message_start + first content_block_*.
- **`flush=True`**: `nohup` / `uvicorn` buffers stdout, you'll see logs
  in 4 KB chunks otherwise. With `flush=True` each line shows up
  immediately — critical when tailing the file.
- **Race condition in start.sh**: if you `start.sh && curl /healthz`
  immediately, uvicorn may not have bound the port yet. Add a 1 s sleep
  or poll with `until curl -fsS .../healthz; do sleep 0.3; done`.
