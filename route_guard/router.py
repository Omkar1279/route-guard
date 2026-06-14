from __future__ import annotations

from typing import Any

from route_guard.classifier import classify
from route_guard.state import reset_turn, with_state


def process_prompt(prompt: str, config: dict[str, Any]) -> str:
    def _update(current_state: dict[str, Any]) -> dict[str, Any]:
        updated = reset_turn(current_state)
        classified = classify(prompt)
        route = classified['route']
        route_config = config.get('routes', {}).get(route, {})

        updated['current_route'] = route
        updated['route_reason'] = classified['reason']
        updated['route_budget'] = route_config.get('budget')
        updated['route_tokens_used'] = 0
        return updated

    new_state = with_state(_update)
    route = new_state.get('current_route')
    route_config = config.get('routes', {}).get(route, {})
    max_spawns = route_config.get('max_spawns', 0)
    agents = f'allowed (max {max_spawns})' if int(max_spawns) > 0 else 'blocked'

    return (
        '<route-guard>\n'
        f'[Route: {str(route).upper()} | Budget: {route_config.get("budget")} tokens | Agents: {agents}]\n'
        '</route-guard>'
    )
