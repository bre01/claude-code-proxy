# Kimi for-coding tool-call loop in non-coding harnesses

> Date: 2026-05-13
> Context: trying to use `kimi-for-coding` (via the
> `https://api.kimi.com/coding/` Anthropic endpoint) inside the Claude for
> Word Office add-in. The add-in only defines Word editing tools — no
> `web_search`, no `SearchWeb`, no `FetchURL`.

## TL;DR

`kimi-for-coding` is **explicitly designed for, and benchmarked under,
a small set of Moonshot-blessed coding harnesses** (Kimi Code CLI,
Claude Code, Roo Code, Kilo Code, OpenCode). Dropping it into any other
harness — Word add-in, Claude Desktop with a custom system prompt,
arbitrary chat UIs — produces measurable degradation that Moonshot
itself has publicly documented. The most visible symptom is unstoppable
hallucinated tool calls (mostly to `web_search` / `SearchWeb`) that no
amount of system-prompt instructions will silence, because the relevant
prior is in the RL-tuned weights, not in any prompt.

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

## Root cause — Moonshot themselves filed the bug

The behavior is **baked into the model weights via RL**, not into a
prompt that can be overridden. The cleanest evidence comes from
Moonshot AI's own bug report against the OpenCode harness:

> **[OpenCode issue #20258](https://github.com/anomalyco/opencode/issues/20258)** — "Default system prompt degrades `kimi-k2.5` performance on coding benchmarks" — filed 2026-03-31 by `Yuxin-Dong`, who states they are *"reporting this on behalf of Moonshot AI"*.

Direct quotes from the issue (Moonshot's own characterization of the
problem):

> *"the current default system prompt appears to degrade `kimi-k2.5` performance on coding- and reasoning-oriented benchmarks."*

> *"the default prompt is not neutral for Kimi. It appears to reduce both average performance and result stability."*

> *"These constraints bias the model toward underspecified or shallow responses and may suppress useful planning, explanation, and intermediate reasoning behavior."*

> *"competing instructions likely create instability in response style and behavior."*

Quantitative claim from the same issue (fine-tuned-for-Kimi prompt vs.
generic default prompt, with reported standard deviations):

| Benchmark   | Fine-tuned prompt | Default prompt | Δ      | σ change |
|-------------|-------------------|----------------|--------|----------|
| Benchmark A | 58.0 ± 2.4        | 54.1 ± 3.8     | −3.9   | +1.4     |
| Benchmark B | 67.1 ± 1.0        | 60.0 ± 2.4     | −7.1   | +1.4     |

So in Moonshot's own evaluation: under a generic prompt structure,
average performance drops several points **and** result variance
roughly doubles. The specific benchmarks aren't named publicly (the
poster: *"the underlying evaluation datasets and benchmark setup are
internal only"*), but the direction and magnitude are explicit.

Implication for our Word use case: Word add-in's system prompt is
**designed for Anthropic Claude**, not for Kimi. By Moonshot's own
admission, the effect is reduced performance and increased instability
— in our case that instability manifests as the hallucinated-tool-call
loop. The model isn't malfunctioning; it's operating *outside* the
prompt envelope it was tuned for.

Supporting evidence:

- [Kimi Code Docs](https://www.kimi.com/code/docs/en/) — Moonshot
  explicitly lists the supported third-party harnesses (Claude Code,
  Roo Code, Kilo Code, OpenCode, Hermes, OpenClaw). Word add-in is
  not on this list. Kimi's own SWE-Bench evaluations use *"an
  internally developed evaluation framework that includes a minimal
  set of tools — bash, createfile, insert, view, strreplace, and
  submit — along with tailored system prompts designed for the tasks."*
- [Kimi K2 Thinking model card](https://huggingface.co/moonshotai/Kimi-K2-Thinking)
  — the model is RL-trained to chain *"200–300 sequential tool calls
  without human interference"*. That trained behavior is what we
  observe persisting in non-agent contexts.
- [HKUDS/nanobot#354](https://github.com/HKUDS/nanobot/issues/354) —
  Moonshot's API server actively rejects non-whitelisted client
  User-Agents (`access_terminated_error: "Kimi For Coding is currently
  only available for Coding Agents such as Kimi CLI, Claude Code, Roo
  Code, Kilo Code, etc."`). The whitelist is the production-side
  manifestation of the same assumption — the model expects a known
  harness.
- [Trilogy AI — "Taming Tool Calling with Kimi K2.5"](https://trilogyai.substack.com/p/taming-tool-calling-with-kimi-k25)
  — independent reproduction: *"On its own, adding instructions to
  the system prompt doesn't reliably override Kimi K2.5's confused
  tool selection."*
- Tool-choice control: Kimi's OpenAI-compatible layer does not
  support `tool_choice: "none"` or `"required"`. The
  application can't force-disable hallucinated tool calls at the API
  level — only declare what tools exist and hope.

The combination — RL-trained agent prior + no `tool_choice` lever +
Moonshot's own documented prompt-sensitivity — means there is no
prompt-engineering or API-flag mitigation. The model *will* try to
call agent tools whenever the conversation looks like a task, and
Word document editing definitely qualifies.

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
doesn't exhibit the same hallucinated-tool-loop behavior in our
session — across ~15 multi-turn Word conversations all returned
`status: 200` and produced sensible edits, with prompt cache hit rate
approaching 100% after the first turn. The Word add-in now works
without any tool filtering on the gateway.

The filter implementation remains the deterministic mitigation for
anyone routing `kimi-for-coding` through a non-coding harness, but
isn't on the current critical path. If you hit the loop on a different
provider's API key (Anthropic API → custom proxy → Kimi, for example),
the filter is the answer.

## K2.6 improvements (partial mitigation)

The [Kimi K2.6 Tech Blog](https://www.kimi.com/blog/kimi-k2-6) and
[Verdent's K2.6 review](https://www.verdent.ai/guides/what-is-kimi-k2-6)
mention better stuck-detection and improved tool-call stability over
K2.5 — "within OpenCode, Kimi K2.6 proves to be exceptionally reliable,
with steady and consistent task decomposition and tool calling." This
helps but doesn't eliminate the fundamental tension: the model is still
RL-trained for agentic harnesses, and `kimi-for-coding` will
automatically upgrade to K2.6 (and K2.7, etc.) without changing the
prompt-envelope assumption. As long as the served model is descended
from the agentic-RL line, dropping it into a non-coding harness will
under-deliver.

## Bottom line for anyone considering this integration

- ✅ Kimi Code CLI + `kimi-for-coding` — designed for this, works well.
- ✅ Claude Code CLI + `kimi-for-coding` via gateway — Moonshot's own
  supported configuration; UA `claude-cli/*` is on the whitelist.
- ✅ OpenCode / Roo Code / Kilo Code + `kimi-for-coding` — supported.
- ⚠️ Generic chat UIs + `kimi-for-coding` — works but expect tool-call
  hallucinations and prompt sensitivity. Mitigation is gateway-side
  filtering, not prompts.
- ❌ Word / Excel / PowerPoint Office add-ins + `kimi-for-coding` —
  not recommended. The add-in's prompts are tuned for Claude, the
  tools are Office editing tools (no web search), and Kimi's
  hallucinated `web_search` calls will loop. Either switch to a
  non-agentic upstream (DeepSeek V4, generic Moonshot API,
  gpt-4o-mini) or invest in the gateway-side filter.

## Related notes

- Kimi's `/coding/` endpoint enforces a client User-Agent whitelist.
  The default `gateway-py` UA `claude-cli/2.1.139` passes. See
  [HKUDS/nanobot#354](https://github.com/HKUDS/nanobot/issues/354) for
  the rejection message from non-whitelisted UAs.
- Kimi's TOS says client identifier tampering may result in suspension.
  In practice impersonating `claude-cli` (which is itself a whitelisted
  coding agent) is the documented path the Kimi Code docs themselves
  describe. Different from impersonating a user-facing app.
- A separate but related bug — [OpenCode #10996](https://github.com/anomalyco/opencode/issues/10996)
  — documents `kimi-for-coding` with `thinking: enabled` failing every
  tool call with `HTTP 400 "thinking is enabled but reasoning_content
  is missing in assistant tool call message at index N"`. Workaround is
  to disable thinking. This is a separate API-level bug from the
  RL-prior issue described above, but worth knowing about when
  integrating.
