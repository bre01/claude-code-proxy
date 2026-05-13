# Kimi for-coding tool-call loop in non-coding harnesses

> Date: 2026-05-13
> Context: trying to use `kimi-for-coding` (via the
> `https://api.kimi.com/coding/` Anthropic endpoint) inside the Claude for
> Word Office add-in. The add-in only defines Word editing tools — no
> `web_search`, no `SearchWeb`, no `FetchURL`.

## Symptom

In Word, every user turn ends with `kimi-for-coding` attempting to call
`web_search` (or `SearchWeb` / `FetchURL`). The tool isn't defined in
the request's `tools` array, so the add-in errors back to the model with
"tool not found" — and the model immediately tries again, with a slight
keyword variation, then again, then again. Conversation never moves
forward. Even after explicit user instructions ("don't use web search,
it's not available"), the loop continues.

Claude Code CLI doesn't have this problem because its built-in tool
suite *does* include web search, so Kimi's hallucinated calls happen to
resolve. The bug only surfaces in non-coding harnesses (Word,
Claude Desktop with custom MCP, etc.).

## What it isn't

Not a prompt injection from the document. Not a server-side system
prompt being added by Kimi's endpoint (we inspected the wire — no
hidden system message). Not a configuration / `tool_choice` issue —
`kimi-for-coding` simply emits `tool_use` blocks for tools that don't
exist in the request.

A user-supplied prompt that explicitly enumerated environment
constraints and forbade non-existent tool calls **did not** suppress
the loop. Even prompts written by an Anthropic model (giving precise
rules like "web_search returns empty = permanently stop calling
web_search") were ignored.

## Root cause

The behavior is **baked into the model weights via RL**, not into a
prompt that can be overridden.

- `kimi-for-coding` is the Kimi K2.5 / K2.6 line, RL-trained to chain
  hundreds of tool calls in agent harnesses
  (https://huggingface.co/moonshotai/Kimi-K2-Thinking — "200–300
  sequential tool calls without human interference").
- Moonshot themselves filed [opencode#20258](https://github.com/anomalyco/opencode/issues/20258)
  acknowledging that K2.5's coding/reasoning benchmark performance
  degrades and becomes less stable under non-Kimi-optimized system
  prompts. So *any* harness whose prompt structure isn't what Moonshot
  tuned for sees worse behavior — Word add-in is a textbook example.
- Kimi's API doesn't support `tool_choice: "none"` (only `"auto"`,
  `"none"` documented but disabled in practice, and `null`). So you
  can't force-disable tool calling at the API level.
- Independent analysis: ["On its own, adding instructions to the
  system prompt doesn't reliably override Kimi K2.5's confused tool
  selection."](https://trilogyai.substack.com/p/taming-tool-calling-with-kimi-k25)

The model's prior, learned during agentic RL, is: *"I have a coding
harness; I should try to acquire information by calling search tools."*
That prior fires whenever Kimi looks at any conversation that smells
like a task — Word document editing definitely qualifies — regardless
of what the system prompt or `tools` array says.

## Deterministic fix (gateway-side)

Since the model's hallucinated tool calls reference names that **aren't
in the request's `tools` array**, the gateway can filter them out
unilaterally. The model never gets a "tool not found" failure, so the
loop never starts.

Pseudo-implementation in `gateway-py/server.py`:

```python
# at request entry, capture the allowed tool name set
allowed_tools = set()
if isinstance(req_body.get("tools"), list):
    for t in req_body["tools"]:
        if isinstance(t, dict) and t.get("name"):
            allowed_tools.add(t["name"])

# at response (non-streaming): walk content blocks, drop tool_use
# whose name is not in allowed_tools. If the message becomes pure
# tool_use → empty, append a synthetic text block so the client
# doesn't get a content-less message.

# at response (streaming SSE): track indexes of `content_block_start`
# events whose tool_use.name is not in allowed_tools, then suppress
# the matching content_block_delta / content_block_stop events for
# those indexes. Rewrite message_delta.stop_reason from "tool_use"
# to "end_turn" if all surviving content blocks are text.
```

Both branches need state tracking. The streaming case is the harder
one — the gateway must parse SSE event boundaries instead of
`aiter_raw`-passing-through. Estimated 100–150 lines.

## Why we didn't ship this fix

The user switched the Word add-in upstream from Kimi to DeepSeek-V4-Pro
mid-debugging. DeepSeek's RL was tuned with different priors and
doesn't exhibit the same hallucinated-tool-loop behavior. The Word
add-in now works without any tool filtering on the gateway. The
filter remains valuable for anyone routing Kimi through a non-coding
harness, but isn't on the current critical path.

If/when needed, the filter implementation is the cleanest deterministic
mitigation. Prompt-only approaches are not viable for `kimi-for-coding`.

## Related notes

- Kimi's `/coding/` endpoint enforces a client User-Agent whitelist.
  The default `gateway-py` UA `claude-cli/2.1.139` passes. See
  [HKUDS/nanobot#354](https://github.com/HKUDS/nanobot/issues/354) for
  the rejection message from non-whitelisted UAs.
- Kimi's TOS says client identifier tampering may result in suspension.
  In practice impersonating `claude-cli` (which is itself a whitelisted
  coding agent) is the documented path the Kimi Code docs themselves
  describe. Different from impersonating a user-facing app.
- K2.6 reportedly improves agentic-loop stuck-detection over K2.5 but
  doesn't eliminate the hallucination prior — the fundamental tension
  between an RL-tuned agent model and a non-agent harness remains.
