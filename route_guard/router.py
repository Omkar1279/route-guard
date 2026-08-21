from __future__ import annotations

from typing import Any

from route_guard import state
from route_guard.budget import route_config
from route_guard.classifier import classify


def process_prompt(prompt: str, config: dict[str, Any]) -> str:
    """Start a new turn, assign a route, and return the directive for Claude."""
    classified = classify(prompt)
    route = classified['route']
    settings = route_config(config, route)

    def _update(current_state: dict[str, Any]) -> dict[str, Any]:
        updated = state.start_turn(current_state)
        updated['current_route'] = route
        updated['route_reason'] = classified['reason']
        updated['route_budget'] = int(settings.get('budget') or 0)
        return updated

    state.with_state(_update)

    max_spawns = int(settings.get('max_spawns') or 0)
    agents = f'allowed (max {max_spawns})' if max_spawns > 0 else 'blocked'

    return (
        '[route-guard] '
        f'Route: {route.upper()} | Budget: {settings.get("budget", "?")} tokens | Agents: {agents}. '
        f'{classified["reason"]} '
        'Size the work to this budget; override with a [route:trivial|small|medium|large] tag.'
    )


__all__ = ['process_prompt']
