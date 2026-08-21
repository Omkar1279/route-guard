# route-guard

Deterministic, zero-LLM token budget governor for Claude Code. It sizes each turn
into a route, tracks what that turn actually spends, promotes the route when the
work outgrows it, and blocks subagent delegation that isn't worth its cost.

No API calls, no dependencies — just three hooks and a JSON state file.

**Requirements:** Python 3.9+ (stdlib only)

## Install

```bash
git clone https://github.com/Omkar1279/route-guard.git
claude --plugin-dir ./route-guard
```

## How it works

| Hook | Fires on | Action |
|---|---|---|
| `UserPromptSubmit` | Every prompt | Assigns a route, starts a new turn, injects the budget directive |
| `PreToolUse` | `Task` / `Agent` | Allows, denies, or promotes the route to pay for the spawn |
| `PostToolUse` | Every tool | Recomputes turn cost from the transcript; escalates the route if needed |

State lives at `.claude/route-guard/<session-id>.json`, one file per session, written
atomically under a directory lock.

## Routes

Prompt length picks the starting route. All four are reachable:

| Route | Prompt length | Token budget | Budgeted spawns |
|---|---|---:|---:|
| trivial | < 120 chars | 10 000 | 0 |
| small | < 400 chars | 40 000 | 0 |
| medium | < 1 200 chars | 120 000 | 2 |
| large | 1 200+ chars | 250 000 | 5 |

Length is a weak proxy for task size, so it is not the last word. A prompt can name
its route explicitly, which overrides the length tier:

```
[route:large] Audit every handler for missing auth checks.
```

## Escalation

The starting route is a guess; escalation is what corrects it, from observed
behaviour rather than from a better guess. Three signals promote a route:

- **token spend** over 120 % of budget — walking up as many tiers as the overrun
  requires, so a runaway turn lands on `large` in one step rather than crawling
- **more than 5 file edits** on a `trivial` turn
- **a legitimate delegation** on a route that budgets no agents (see below)

Each promotion is recorded in `escalations` and announced to Claude:

```
[route-guard] Route escalated from "trivial" to "small". Token usage (15674)
exceeded 120% of the trivial budget (10000). New budget: 40000 tokens.
```

`large` is the ceiling and never escalates.

## Spawn gating

Whether the delegation is worth doing at all is judged first, independent of route:
a subagent prompt that is non-empty but under 200 chars is **denied as overkill**.

`max_spawns: 0` then does not mean *"agents are forbidden here"* — it means *"this
route doesn't budget for agents."* Since the route came from a guess about prompt
length, a delegation that clears the bar above is treated as evidence the guess was
wrong: the route is promoted to the cheapest one that does budget for agents, and
the spawn is judged there. Setting `max_spawns: 0` on **every** route is a real
decision and is honoured — nothing is promoted and all delegation is refused.

The remaining rules apply at that effective route:

- the spawn cap is used up → denied, and never promoted (asking for a third agent
  on `medium` does not buy you `large`)
- turn cost is over 85 % of budget → denied
- turn cost is over 70 % of budget **and** the subagent prompt is > 1 000 chars → denied

Denials return a structured reason, which Claude receives as the tool result:

```json
{
  "blocked": true,
  "route": "medium",
  "budget": "0/120000",
  "spawns": "0/2",
  "reason": "Delegation overkill: subagent prompt is only 10 chars (min 200).",
  "options": [
    "Do the work inline without spawning a subagent.",
    "If the task genuinely requires delegation, ask the user to resubmit with a [route:large] tag."
  ]
}
```

Denied attempts count toward `spawns_blocked`, never toward the cap — being blocked
for a short prompt doesn't burn a legitimate spawn.

## Token accounting

Hook payloads carry no token counts, so usage is read from the session transcript
that every hook receives as `transcript_path`. Two details matter:

- **One API response is written as several transcript lines**, each repeating the
  same `message.usage`. Entries are deduplicated by `message.id`; summing lines
  naively inflates counts 2–3×.
- **`cache_read_input_tokens` re-counts the whole conversation every turn.** Turn
  cost sums `input + cache_creation + output` only. Cache reads are tracked
  separately as `context_tokens`, the live context size.

Usage is *recomputed* on every `PostToolUse` rather than accumulated, so a
duplicated or replayed hook event cannot inflate the count. If the turn boundary
is unknown, the count stays at 0 rather than charging an entire transcript to the
current turn.

Subagent tool calls (`agent_id` present) bill against their own transcript and are
skipped; their spawns are still gated.

## Configuration

`config.json` — budgets and spawn caps:

```json
{
  "routes": {
    "trivial": { "budget": 10000, "max_spawns": 0 },
    "small":   { "budget": 40000, "max_spawns": 0 },
    "medium":  { "budget": 120000, "max_spawns": 2 },
    "large":   { "budget": 250000, "max_spawns": 5 }
  }
}
```

Thresholds (85 %, 70 %, 120 %, 200-char minimum) are constants in
`route_guard/budget.py`.

## Layout

```
route_guard/
├── cli.py         # stdin -> dispatch -> hookSpecificOutput on stdout
├── router.py      # turn start + route assignment
├── classifier.py  # length tiers + [route:...] override
├── budget.py      # spawn gating, escalation
├── transcript.py  # token accounting from the session JSONL
└── state.py       # per-session atomic JSON state
```

Each hook is a shell shim that runs `python3 -m route_guard.cli <verb>`. Every
handler swallows its own exceptions and exits 0 — a governor that crashes a
session is worse than one that stops governing.

## Development

```bash
pytest tests/                 # 46 unit, contract, and end-to-end tests
claude plugin validate ./     # validate the plugin manifest
claude --plugin-dir ./        # load locally
```
