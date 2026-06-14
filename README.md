# route-guard

Deterministic, zero-LLM token budget governor for Claude Code. It routes tasks by prompt size, enforces route budgets, and blocks unnecessary delegation.

## Install

```bash
git clone https://github.com/Omkar1279/route-guard.git
claude --plugin-dir ./route-guard
```

Requires: Python 3.9+ (stdlib only).

## Blocked spawn output

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

## Routes

| Level | Budget | Max spawns |
|---|---:|---:|
| trivial | 10000 | 0 |
| small | 40000 | 0 |
| medium | 120000 | 2 |
| large | 250000 | 5 |

## How it works

1. UserPromptSubmit: resets turn, classifies prompt by length, writes route state, returns route context.
2. PreToolUse (Task|Agent): applies deterministic spawn gating rules and blocks with exit code 2 when needed.
3. PostToolUse: records token usage and file edits, escalates route when thresholds are exceeded.

State is stored at .claude/.route-guard-state.json per workspace.

## Configuration

`config.json`:

```json
{
  "routes": {
    "trivial": { "budget": 10000, "max_spawns": 0 },
    "small": { "budget": 40000, "max_spawns": 0 },
    "medium": { "budget": 120000, "max_spawns": 2 },
    "large": { "budget": 250000, "max_spawns": 5 }
  }
}
```

## Development

```bash
pytest tests/                      # Unit tests
claude plugin validate ./          # Validate plugin structure
claude --plugin-dir ./             # Load locally
```
