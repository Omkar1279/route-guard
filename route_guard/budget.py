from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from route_guard import state

ESCALATION_THRESHOLD = 1.2
BUDGET_WARN_THRESHOLD = 0.85
BUDGET_DELEGATION_THRESHOLD = 0.70
MIN_SUBAGENT_PROMPT_LENGTH = 200
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


def evaluate_spawn(current_state: dict[str, Any], tool_input: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    route = current_state.get('current_route')
    route_config = config.get('routes', {}).get(route)
    if not route_config:
        return {'allowed': True, 'reason': 'No route config found.'}

    prompt_len = len((tool_input or {}).get('prompt') or '')
    tokens_used = _as_int(current_state.get('cumulative_tokens_this_turn'))
    budget = _as_int(route_config.get('budget'))
    max_spawns = _as_int(route_config.get('max_spawns'))

    if max_spawns == 0:
        return {'allowed': False, 'reason': f'Agents disallowed on "{route}" route.'}

    if _as_int(current_state.get('spawns_attempted')) >= max_spawns:
        return {
            'allowed': False,
            'reason': f'Spawn cap reached ({current_state.get("spawns_attempted", 0)}/{max_spawns}).',
        }

    if budget > 0 and tokens_used > BUDGET_WARN_THRESHOLD * budget:
        pct = round(BUDGET_WARN_THRESHOLD * 100)
        return {
            'allowed': False,
            'reason': f'Near budget ceiling ({tokens_used}/{budget} tokens used, >{pct}%).',
        }

    if budget > 0 and tokens_used > BUDGET_DELEGATION_THRESHOLD * budget and prompt_len > 1000:
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


def build_block_message(current_state: dict[str, Any], reason: str, config: dict[str, Any]) -> str:
    route = current_state.get('current_route')
    route_config = config.get('routes', {}).get(route, {})
    payload = {
        'blocked': True,
        'route': route,
        'budget': f"{current_state.get('cumulative_tokens_this_turn', 0)}/{route_config.get('budget', '?')}",
        'spawns': f"{current_state.get('spawns_attempted', 0)}/{route_config.get('max_spawns', 0)}",
        'reason': reason,
        'options': [
            'Do the work inline without spawning a subagent.',
            'If the task genuinely requires delegation, ask the user to bump the route.',
        ],
    }
    return json.dumps(payload, indent=2)


def record_tokens(input_tokens: Any, output_tokens: Any) -> dict[str, Any]:
    total = _as_int(input_tokens) + _as_int(output_tokens)

    def _update(current_state: dict[str, Any]) -> dict[str, Any]:
        current_state['route_tokens_used'] = _as_int(current_state.get('route_tokens_used')) + total
        current_state['cumulative_tokens_this_turn'] = _as_int(current_state.get('cumulative_tokens_this_turn')) + total
        return current_state

    return state.with_state(_update)


def increment_file_edits(tool_name: str) -> dict[str, Any] | None:
    if tool_name not in _FILE_EDIT_TOOLS:
        return None

    def _update(current_state: dict[str, Any]) -> dict[str, Any]:
        current_state['file_edits_this_turn'] = _as_int(current_state.get('file_edits_this_turn')) + 1
        return current_state

    return state.with_state(_update)


def _escalate(current_state: dict[str, Any], new_route: str, reason: str, config: dict[str, Any]) -> str:
    from_route = current_state.get('current_route')
    new_config = config.get('routes', {}).get(new_route)
    if not new_config:
        return ''

    current_state['current_route'] = new_route
    current_state['route_budget'] = _as_int(new_config.get('budget'))
    current_state['route_reason'] = f'Escalated: {reason}'
    current_state.setdefault('escalations', []).append(
        {
            'from': from_route,
            'to': new_route,
            'reason': reason,
            'at_tokens': _as_int(current_state.get('cumulative_tokens_this_turn')),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    )
    state.write_state(current_state)

    agents = 'allowed' if _as_int(new_config.get('max_spawns')) > 0 else 'blocked'
    return (
        '<system-reminder>\n'
        f'[route-guard ESCALATION] Route escalated from "{from_route}" to "{new_route}".\n'
        f'Reason: {reason}\n'
        f'New budget: {new_config.get("budget")} tokens. Agents: {agents}.\n'
        '</system-reminder>'
    )


def check_escalation(current_state: dict[str, Any], config: dict[str, Any]) -> str | None:
    route = current_state.get('current_route')
    route_config = config.get('routes', {}).get(route)
    if not route_config:
        return None

    budget = _as_int(route_config.get('budget'))
    tokens_used = _as_int(current_state.get('cumulative_tokens_this_turn'))

    if route in {'trivial', 'small'} and budget > 0 and tokens_used > ESCALATION_THRESHOLD * budget:
        try:
            current_idx = ROUTE_ORDER.index(route)
        except ValueError:
            current_idx = 0
        next_route = ROUTE_ORDER[min(current_idx + 1, len(ROUTE_ORDER) - 1)]
        reason = (
            f'Token usage ({tokens_used}) exceeded '
            f'{round(ESCALATION_THRESHOLD * 100)}% of {route} budget ({budget}).'
        )
        return _escalate(current_state, next_route, reason, config)

    file_edits = _as_int(current_state.get('file_edits_this_turn'))
    if route == 'trivial' and file_edits > 5:
        reason = f'File edits ({file_edits}) exceeded threshold for trivial route.'
        return _escalate(current_state, 'small', reason, config)

    return None
