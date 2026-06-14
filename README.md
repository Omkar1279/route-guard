# route-guard

Deterministic, zero-LLM token budget governor for Claude Code. Routes tasks by prompt size, enforces per-route token budgets, and blocks unnecessary subagent delegation.

**Requirements:** Python 3.9+ (stdlib only — no pip, no virtualenv)

## Install

```bash
git clone https://github.com/Omkar1279/route-guard.git
claude --plugin-dir ./route-guard
```

## How it works

Three Claude Code hooks fire on every turn:

| Hook | Trigger | Action |
|---|---|---|
| `user_prompt_submit` | New prompt | Classifies by length → sets route + budget |
| `pre_tool_use` | Task/Agent spawn | Checks spawn cap + budget; exits 2 to block |
| `post_tool_use` | Any tool | Records tokens + file edits; auto-escalates route |

State is persisted at `.claude/.route-guard-state.json` per workspace (written atomically, directory-lock safe for concurrent hooks).

## Routes

Routing is length-based: prompts under 80 chars → `small`, 80+ chars → `medium`. Auto-escalation promotes the route when thresholds are exceeded mid-turn.

| Route | Token budget | Max spawns | Escalates from |
|---|---:|---:|---|
| trivial | 10 000 | 0 | — |
| small | 40 000 | 0 | trivial (>120% budget or >5 file edits) |
| medium | 120 000 | 2 | small (>120% budget) |
| large | 250 000 | 5 | — |

## Spawn gating rules

A `Task` or `Agent` spawn is blocked (exit code 2) when any of these are true:

- Route has `max_spawns: 0`
- Spawn cap for the route is already reached
- Token usage exceeds 85% of route budget
- Token usage exceeds 70% of route budget **and** the subagent prompt is >1000 chars
- Subagent prompt is non-empty but under 200 chars (delegation overkill)

Blocked output:

```json
{
  "blocked": true,
  "route": "small",
  "budget": "28000/40000",
  "spawns": "0/0",
  "reason": "Agents disallowed on \"small\" route.",
  "options": [
    "Do the work inline without spawning a subagent.",
    "If the task genuinely requires delegation, ask the user to bump the route."
  ]
}
```

## Implementation

Pure Python, zero dependencies. Each hook is a 5-line shell shim that calls:

```bash
PYTHONPATH="$PLUGIN_ROOT" python3 -m route_guard.cli <verb>
```

Package layout:

```
route_guard/
├── cli.py         # stdin → dispatch → stdout/stderr + exit code
├── router.py      # prompt classification + state update
├── classifier.py  # length-based route decision
├── budget.py      # spawn gating, token recording, escalation
└── state.py       # atomic JSON read/write with directory lock
```

## Configuration

`config.json` — edit to tune budgets or spawn caps:

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

## Development

```bash
pytest tests/                   # 16 unit + integration tests
claude plugin validate ./       # validate plugin structure
claude --plugin-dir ./          # load locally
```
