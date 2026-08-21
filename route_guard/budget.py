from __future__ import annotations

import json
from typing import Any, Optional

from route_guard import state, transcript

ESCALATION_THRESHOLD = 1.2
BUDGET_WARN_THRESHOLD = 0.85
BUDGET_DELEGATION_THRESHOLD = 0.70
LARGE_SUBAGENT_PROMPT_LENGTH = 1000
MIN_SUBAGENT_PROMPT_LENGTH = 200
TRIVIAL_FILE_EDIT_LIMIT = 5
ROUTE_ORDER = ['trivial', 'small', 'medium', 'large']

_FILE_EDIT_TOOLS = {
    'Write',
    'Edit',
    'NotebookEdit',
    'write_file',
    'edit_file',
    'replace_string_in_file',
    'create_file',
    'multi_replace_string_in_file',
}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def route_config(config: dict[str, Any], route: Any) -> dict[str, Any]:
    routes = config.get('routes')
    if not isinstance(routes, dict):
        return {}
    found = routes.get(route)
    return found if isinstance(found, dict) else {}


def evaluate_spawn(
    current_state: dict[str, Any], tool_input: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    route = current_state.get('current_route')
    settings = route_config(config, route)
    if not settings:
        return {'allowed': True, 'reason': 'No route assigned yet.'}

    prompt_len = len((tool_input or {}).get('prompt') or '')
    tokens_used = _as_int(current_state.get('tokens_this_turn'))
    budget = _as_int(settings.get('budget'))
    max_spawns = _as_int(settings.get('max_spawns'))
    spawns_used = _as_int(current_state.get('spawns_used'))

    if max_spawns == 0:
        return {'allowed': False, 'reason': f'Agents disallowed on "{route}" route.'}

    if spawns_used >= max_spawns:
        return {'allowed': False, 'reason': f'Spawn cap reached ({spawns_used}/{max_spawns}).'}

    if budget > 0 and tokens_used > BUDGET_WARN_THRESHOLD * budget:
        pct = round(BUDGET_WARN_THRESHOLD * 100)
        return {
            'allowed': False,
            'reason': f'Near budget ceiling ({tokens_used}/{budget} tokens used, >{pct}%).',
        }

    if (
        budget > 0
        and tokens_used > BUDGET_DELEGATION_THRESHOLD * budget
        and prompt_len > LARGE_SUBAGENT_PROMPT_LENGTH
    ):
        pct = round((tokens_used / budget) * 100)
        return {
            'allowed': False,
            'reason': f'Delegation expensive at {pct}% budget with {prompt_len}-char prompt; do inline.',
        }

    if 0 < prompt_len < MIN_SUBAGENT_PROMPT_LENGTH:
        return {
            'allowed': False,
            'reason': (
                f'Delegation overkill: subagent prompt is only {prompt_len} chars '
                f'(min {MIN_SUBAGENT_PROMPT_LENGTH}).'
            ),
        }

    return {'allowed': True, 'reason': 'Within budget and spawn limits.'}


def build_block_reason(current_state: dict[str, Any], reason: str, config: dict[str, Any]) -> str:
    route = current_state.get('current_route')
    settings = route_config(config, route)
    payload = {
        'blocked': True,
        'route': route,
        'budget': f"{_as_int(current_state.get('tokens_this_turn'))}/{settings.get('budget', '?')}",
        'spawns': f"{_as_int(current_state.get('spawns_used'))}/{settings.get('max_spawns', 0)}",
        'reason': reason,
        'options': [
            'Do the work inline without spawning a subagent.',
            'If the task genuinely requires delegation, ask the user to resubmit '
            'with a [route:large] tag.',
        ],
    }
    return json.dumps(payload, indent=2)


def record_spawn(allowed: bool) -> dict[str, Any]:
    field = 'spawns_used' if allowed else 'spawns_blocked'

    def _update(current_state: dict[str, Any]) -> dict[str, Any]:
        current_state[field] = _as_int(current_state.get(field)) + 1
        return current_state

    return state.with_state(_update)


def record_tool_use(
    transcript_path: Optional[str], tool_name: str, config: dict[str, Any]
) -> Optional[str]:
    """Fold a completed tool call into the turn, in one state transaction.

    Recomputing usage rather than accumulating makes this idempotent: a replayed
    or duplicated PostToolUse event cannot inflate the count. Reading the turn
    boundary inside the lock keeps a finished turn's count from landing on a
    freshly started one.
    """
    notice: dict[str, str] = {}

    def _update(current_state: dict[str, Any]) -> dict[str, Any]:
        since = transcript.parse_timestamp(current_state.get('turn_started_at'))
        usage = transcript.read_usage(transcript_path, since)
        current_state['tokens_this_turn'] = usage['tokens']
        current_state['context_tokens'] = usage['context']

        if tool_name in _FILE_EDIT_TOOLS:
            current_state['file_edits_this_turn'] = _as_int(current_state.get('file_edits_this_turn')) + 1

        escalated = _apply_escalation(current_state, config)
        if escalated:
            notice['text'] = escalated
        return current_state

    state.with_state(_update)
    return notice.get('text')


def _token_escalation_target(route: str, tokens: int, config: dict[str, Any]) -> Optional[str]:
    """Walk up the route ladder until the budget accommodates ``tokens``."""
    index = ROUTE_ORDER.index(route)
    target = index
    while target < len(ROUTE_ORDER) - 1:
        budget = _as_int(route_config(config, ROUTE_ORDER[target]).get('budget'))
        if budget <= 0 or tokens <= ESCALATION_THRESHOLD * budget:
            break
        target += 1
    return ROUTE_ORDER[target] if target != index else None


def _pending_escalation(
    current_state: dict[str, Any], config: dict[str, Any]
) -> Optional[tuple[str, str]]:
    route = current_state.get('current_route')
    if route not in ROUTE_ORDER or not route_config(config, route):
        return None

    tokens = _as_int(current_state.get('tokens_this_turn'))
    budget = _as_int(route_config(config, route).get('budget'))
    target = _token_escalation_target(route, tokens, config)
    if target:
        pct = round(ESCALATION_THRESHOLD * 100)
        return target, f'Token usage ({tokens}) exceeded {pct}% of the {route} budget ({budget}).'

    edits = _as_int(current_state.get('file_edits_this_turn'))
    if route == 'trivial' and edits > TRIVIAL_FILE_EDIT_LIMIT:
        return 'small', f'File edits ({edits}) exceeded the trivial-route limit of {TRIVIAL_FILE_EDIT_LIMIT}.'

    return None


def _apply_escalation(current_state: dict[str, Any], config: dict[str, Any]) -> Optional[str]:
    """Promote the route in place if this turn outgrew it. Returns a notice."""
    pending = _pending_escalation(current_state, config)
    if not pending:
        return None

    new_route, reason = pending
    settings = route_config(config, new_route)
    if not settings:
        return None

    from_route = current_state.get('current_route')
    current_state['current_route'] = new_route
    current_state['route_budget'] = _as_int(settings.get('budget'))
    current_state['route_reason'] = f'Escalated: {reason}'
    current_state['escalations'].append(
        {
            'from': from_route,
            'to': new_route,
            'reason': reason,
            'at_tokens': _as_int(current_state.get('tokens_this_turn')),
            'timestamp': state.now_iso(),
        }
    )

    max_spawns = _as_int(settings.get('max_spawns'))
    agents = f'allowed (max {max_spawns})' if max_spawns > 0 else 'blocked'
    return (
        f'[route-guard] Route escalated from "{from_route}" to "{new_route}". '
        f'{reason} New budget: {settings.get("budget")} tokens. Agents: {agents}.'
    )


def check_escalation(config: dict[str, Any]) -> Optional[str]:
    """Standalone escalation check against the stored state."""
    notice: dict[str, str] = {}

    def _update(current_state: dict[str, Any]) -> dict[str, Any]:
        escalated = _apply_escalation(current_state, config)
        if escalated:
            notice['text'] = escalated
        return current_state

    state.with_state(_update)
    return notice.get('text')


__all__ = [
    'build_block_reason',
    'check_escalation',
    'evaluate_spawn',
    'record_spawn',
    'record_tool_use',
    'route_config',
]
