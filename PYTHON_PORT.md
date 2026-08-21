# Route-Guard — Implementation Notes

> Design reference for the Python implementation. Records the Claude Code hook
> contract the plugin depends on, and the decisions behind the current design.
> User-facing behaviour lives in [README.md](README.md).

---

## The hook contract

These were verified against the zod schemas embedded in the Claude Code binary
(v2.1.220), not from documentation — the published docs disagreed with the binary
on three of the four points below, and the binary won each time.

### Input

Every hook receives a common envelope:

```
session_id, transcript_path, cwd, prompt_id?, permission_mode?, agent_id?, agent_type?
```

Plus per-event fields:

| Event | Adds |
|---|---|
| `UserPromptSubmit` | `prompt`, `session_title?` |
| `PreToolUse` | `tool_name`, `tool_input`, `tool_use_id` |
| `PostToolUse` | `tool_name`, `tool_input`, `tool_response`, `tool_use_id`, `duration_ms?` |

**There is no `usage` field on any hook event.** Token counts must come from the
transcript. `agent_id` is present only when the hook fires inside a subagent.

### Output

Output nests under `hookSpecificOutput`, and **`hookEventName` is required** — it
is the discriminator of a union type, so an entry without it is dropped silently
rather than rejected loudly:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "..."
  }
}
```

| Event | Accepts |
|---|---|
| `UserPromptSubmit` | `additionalContext`, `sessionTitle`, `suppressOriginalPrompt` |
| `PreToolUse` | `permissionDecision`, `permissionDecisionReason`, `updatedInput`, `additionalContext` |
| `PostToolUse` | `additionalContext`, `updatedToolOutput`, `updatedMCPToolOutput` |

There is **no top-level `additionalContext`** in the output schema. The top level
accepts only `continue`, `suppressOutput`, `stopReason`, `decision`,
`systemMessage`, `terminalSequence`, `reason`, and `hookSpecificOutput`.

Denials use `permissionDecision: "deny"` with exit code 0, which delivers a
structured reason to Claude. Exit code 2 also blocks but only surfaces raw stderr.

### Plugin hook registration

`hooks/hooks.json` is auto-discovered at the plugin root. In a hook entry
`command` is a **string**, never an array. Exec form is `command` plus an `args`
array, which spawns the executable directly with no shell — the safe way to pass
`${CLAUDE_PLUGIN_ROOT}`, since placeholders are substituted per element:

```json
{ "type": "command", "command": "bash", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/pre_tool_use.sh"] }
```

---

## Token accounting

`transcript.py` reads the session JSONL at `transcript_path`. Three properties of
that file shape the implementation:

1. **A single API response spans several lines** — one per content block — and each
   line repeats the *same* `message.usage`. Observed at 2–3 lines per response in
   practice. Entries are deduplicated by `message.id` (falling back to `requestId`,
   then `uuid`); without this, counts inflate 2–3×.
2. **`cache_read_input_tokens` re-counts the whole conversation** on every turn, so
   it is excluded from turn cost. Turn cost is `input + cache_creation + output`.
   Cache reads are reported separately as `context_tokens`.
3. **Subagent work is not in this file** — it has its own transcript. Sidechain
   entries are skipped, and `PostToolUse` accounting is skipped entirely when
   `agent_id` is present.

Spawn gating deliberately does *not* skip on `agent_id`, because subagents share
the parent's `session_id` and therefore the same state file. Confirmed live: a
subagent that spawned a further subagent incremented the parent's counter to
`spawns_used: 2`, so nested delegation is capped by the same ceiling rather than
escaping it.

Usage is **recomputed** from scratch on each `PostToolUse` rather than accumulated.
This makes the hook idempotent: a replayed or duplicated event cannot inflate the
count, and a missed event self-heals on the next tool call.

The turn boundary is `turn_started_at`, an ISO timestamp stamped at
`UserPromptSubmit`. When it is absent — a resumed session, or a `PostToolUse` that
arrives before any prompt — accounting **fails closed at zero** rather than
charging an entire transcript to the current turn.

Everything a completed tool call touches — token counts, the file-edit counter and
the escalation check — happens in **one** `with_state` transaction, so each tool
costs one lock cycle and one write rather than three. Reading `turn_started_at`
inside that lock also stops a finished turn's count from landing on a freshly
started one.

Parsing is a full-file scan with a substring prefilter before `json.loads`.
`PostToolUse` has no matcher, so this runs on every tool call and the cost was
measured rather than assumed:

| Transcript size | Scan |
|---|---:|
| 6.4 MB (largest real transcript on the dev machine) | 15 ms |
| 49 MB (synthetic) | 59 ms |
| 245 MB (synthetic) | 278 ms |

The prefilter keeps this cheap enough that seeking to a stored byte offset would
add a `/compact`-invalidation edge case for no useful gain. Tail-seeking was
rejected outright: it buys a partial-first-line bug and undercounts long turns.

---

## State

One file per session at `.claude/route-guard/<session-id>.json`. Session scoping
keeps two Claude Code sessions in the same repo from clobbering each other, and
stops stale state from a previous session leaking into a new one.

```python
DEFAULT_STATE = {
    'session_id': None,
    'turn_number': 0,
    'turn_started_at': None,   # ISO8601; the turn boundary for token accounting
    'current_route': None,
    'route_reason': None,
    'route_budget': 0,
    'tokens_this_turn': 0,     # input + cache_creation + output, this turn
    'context_tokens': 0,       # live context size, including cache reads
    'spawns_used': 0,          # allowed spawns; counts against max_spawns
    'spawns_blocked': 0,       # denied spawns; does not count against the cap
    'file_edits_this_turn': 0,
    'escalations': [],
}
```

Writes go through `with_state(fn)`: read-modify-write under a mkdir-based
directory lock, then an atomic `Path.replace()`. Escalation runs inside that same
transaction rather than writing a stale snapshot back over concurrent updates.

---

## Design decisions

**Four length tiers, not two.** Length-only routing previously used a single
80-char threshold, which made `trivial` and `large` unreachable — two of the four
configured routes were dead. Tiers at 120 / 400 / 1200 chars make all four live.

**An explicit `[route:...]` override — a deliberate reversal.** An earlier revision
removed the `force_route` escape hatch on purpose. It is back, because length
cannot express intent: a short "audit the whole repo with subagents" lands in
`trivial`, whose `max_spawns: 0` blocks exactly the work being asked for. No
threshold tuning fixes an inverted signal. The override is the correction
mechanism, which is why it is preferred over reintroducing keyword heuristics —
those were already tried, trimmed 15 → 5, then dropped as untestable.

**Escalation walks multiple tiers.** Previously only `trivial` and `small` escalated,
one step at a time. Now any route below `large` promotes as far as the overrun
requires, so a turn that blows through 200 k tokens lands on `large` immediately
instead of crawling one tier per tool call. Removing the `medium`-and-above special
case also removed a rule with no stated rationale.

**Blocked spawns don't consume the cap.** `spawns_attempted` previously incremented
only on success, making the name a lie. It is now split: `spawns_used` gates the
cap, `spawns_blocked` is reporting only. Otherwise two rejected short prompts would
permanently exhaust a `max_spawns: 2` route.

**Hooks never fail the session.** Every handler catches its own exceptions and
exits 0. A governor that crashes a user's session is worse than one that silently
stops governing.

---

## Tests

```bash
pytest tests/ -v    # 40 cases
```

Three layers:

- **Unit** — classifier tiers and override, every spawn-gating rule, escalation
  paths, state isolation per session, concurrent `with_state`, corrupt-state
  recovery.
- **Transcript** — `message.id` deduplication, cache-read exclusion, sidechain
  skipping, the fail-closed path when the turn boundary is missing.
- **Contract** — each hook shim is run as a subprocess and its stdout asserted to
  carry the correct `hookSpecificOutput.hookEventName`. This layer exists because
  the previous suite passed while encoding a hook contract that Claude Code was
  silently discarding; green unit tests are not evidence the contract is right.

Verified end-to-end against a live `claude --plugin-dir` session on Python 3.9 and
3.12: directive injection, transcript-based token accounting, route escalation, and
subagent denial all confirmed in a real session.
