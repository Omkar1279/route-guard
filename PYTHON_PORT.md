# Route-Guard — Python Implementation

> Stdlib-only Python 3.9+ rewrite of the original Node/JS plugin. Hook contract with Claude Code is unchanged — only the implementation language changed.

---

## Layout

```
route-guard/
├── .claude-plugin/plugin.json
├── hooks/
│   ├── hooks.json
│   ├── user_prompt_submit.sh    # shim → python3 -m route_guard.cli user_prompt_submit
│   ├── pre_tool_use.sh          # shim → python3 -m route_guard.cli pre_tool_use
│   └── post_tool_use.sh         # shim → python3 -m route_guard.cli post_tool_use
├── route_guard/
│   ├── __init__.py
│   ├── cli.py        # stdin → dispatch → stdout/stderr + exit code
│   ├── router.py     # prompt classification + state update
│   ├── classifier.py # length-based route decision
│   ├── budget.py     # spawn gating, token recording, escalation
│   └── state.py      # atomic JSON read/write with directory lock
├── tests/
│   ├── __init__.py
│   └── test_governor.py   # 16 pytest cases
├── config.json
├── README.md
├── LICENSE
└── .gitignore
```

**Note on structure:** The plan originally specified a single flat `route_guard.py`. The actual implementation uses a package. At ~250 LOC the split does add `PYTHONPATH` in each shim and an `__init__.py`, but it makes individual modules independently testable and keeps each concern in one place.

---

## Hook shims

All three collapse to the same 6-line pattern:

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PLUGIN_ROOT="$( dirname "$DIR" )"
PYTHONPATH="$PLUGIN_ROOT" exec python3 -m route_guard.cli <verb>
```

Verbs: `user_prompt_submit`, `pre_tool_use`, `post_tool_use`.

`hooks/hooks.json` is unchanged — still points at the three shims; `PreToolUse` matcher stays `"Task|Agent"`.

---

## Modules

### `state.py`

- `set_workspace_root(cwd)` / `state_path()` → `.claude/.route-guard-state.json`
- `read_state()` — JSON parse + merge with `DEFAULT_STATE` to tolerate schema drift
- `write_state(state)` — atomic via temp file + `Path.replace()`
- `with_state(fn)` — RMW with directory lock
- `reset_turn(state)` — bumps `turn_number`, zeros per-turn counters
- `acquire_lock()` / `release_lock()` — mkdir-based with PID file and 5 s stale detection

```python
DEFAULT_STATE = {
    "current_route": None,
    "route_reason": None,
    "route_budget": None,
    "turn_number": 0,
    "cumulative_tokens_this_turn": 0,
    "route_tokens_used": 0,
    "spawns_attempted": 0,
    "file_edits_this_turn": 0,
    "escalations": [],
}
```

### `classifier.py`

Length-only routing — keyword matching dropped entirely:

```python
_LENGTH_THRESHOLD = 80

def classify(prompt: str) -> dict[str, str]:
    if len(prompt) < _LENGTH_THRESHOLD:
        return {"route": "small", "reason": f"Prompt under {_LENGTH_THRESHOLD} chars."}
    return {"route": "medium", "reason": f"Prompt {_LENGTH_THRESHOLD}+ chars."}
```

### `budget.py`

Constants (formerly config knobs):

```python
ESCALATION_THRESHOLD = 1.2
BUDGET_WARN_THRESHOLD = 0.85
BUDGET_DELEGATION_THRESHOLD = 0.70
MIN_SUBAGENT_PROMPT_LENGTH = 200
ROUTE_ORDER = ['trivial', 'small', 'medium', 'large']
```

`evaluate_spawn(state, tool_input, config)` — blocks a spawn when any of these hold:
- `max_spawns == 0`
- `spawns_attempted >= max_spawns`
- tokens used > 85% of budget
- tokens used > 70% of budget **and** subagent prompt > 1000 chars
- subagent prompt is non-empty but < 200 chars

`check_escalation(state, config)` — fires after every tool call:
- `trivial` or `small`: escalate to next route when tokens > 120% of budget
- `trivial`: also escalate to `small` when `file_edits_this_turn > 5`
- `medium` and above: no token-based auto-escalation

`increment_file_edits(tool_name)` — increments `file_edits_this_turn` for write tools (`Write`, `Edit`, `NotebookEdit`, etc.).

### `router.py`

`process_prompt(prompt, config)` — resets turn, classifies, writes state, returns:

```
<route-guard>
[Route: SMALL | Budget: 40000 tokens | Agents: blocked]
</route-guard>
```

### `cli.py`

Reads stdin once, parses JSON, dispatches on `argv[1]`. Any unhandled exception logs to stderr and exits 0 (degrade gracefully, never block Claude Code).

---

## Config

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

**Removed from original config:**

| Key | Reason |
|---|---|
| `$schema` / `config.schema.json` | No runtime validation needed |
| `routes.*.allow_agents` | Derived from `max_spawns > 0` |
| `routes.*.description` | Unused in code |
| `routes.exploratory` | Orphaned — no escalation path |
| `agent_tools` | `hooks.json` matcher is single source of truth |
| `route_order` | Hardcoded as `ROUTE_ORDER` in `budget.py` |
| `escalation_threshold` | Module constant |
| `budget_warn_threshold` | Module constant |
| `budget_delegation_threshold` | Module constant |
| `min_subagent_prompt_length` | Module constant |

**`trivial` route kept** — the original plan called for removing it, but the file-edits escalation rule (`trivial` → `small` on >5 file edits) was kept, so `trivial` remains a valid route.

---

## What was cut vs. the JS original

| Cut | Applied |
|---|---|
| `bin/` CLI integration tests | Yes — `test/integration.test.js` deleted |
| `force_route` machinery | Yes — no escape hatch |
| `strict_mode` | Yes |
| `<cc-route>` self-classification protocol | Yes — `parseRouteTag`, `scrape*`, `markFallbackIfNeeded`, directive prose, SKILL.md |
| `exploratory` route | Yes |
| `session_budget` / `session_tokens_used` | Yes |
| `spawns_blocked` counter | Yes |
| `VALID_ROUTES` | Yes — `validate.js` deleted entirely |
| Lock busy-spin | Yes — replaced with `time.sleep` |
| `config.schema.json` | Yes |
| Config slimmed to budget + max_spawns | Yes |
| Length-only classifier | Yes |
| `sanitizeString` | Yes |
| SKILL.md | Yes |
| Keywords list trimmed (15 → 5) | Yes |
| `trivial` route removal | **No** — kept; file-edits escalation rule retained |
| `file_edits_this_turn` removal | **No** — kept; still drives trivial→small escalation |
| Single flat script (vs. package) | **No** — implemented as `route_guard/` package |

---

## Tests

```bash
pytest tests/ -v    # 16 cases, all pass
```

Coverage: classifier, all spawn-gating rules, escalation (token + file-edits), block message shape, escalation from/to correctness, turn reset, thread-safety of `with_state`, `process_prompt`, and end-to-end `pre_tool_use.sh` subprocess.
