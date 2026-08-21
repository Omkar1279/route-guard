"""Deterministic route selection from the submitted prompt."""

from __future__ import annotations

import re

# Ascending length ceilings. A prompt shorter than a ceiling takes that route;
# anything longer falls through to the last route.
_TIERS = (
    (120, 'trivial'),
    (400, 'small'),
    (1200, 'medium'),
)
_FALLBACK = 'large'

_OVERRIDE = re.compile(r'\[route:\s*(trivial|small|medium|large)\s*\]', re.IGNORECASE)


def classify(prompt: str) -> dict[str, str]:
    override = _OVERRIDE.search(prompt or '')
    if override:
        route = override.group(1).lower()
        return {'route': route, 'reason': f'Explicit [route:{route}] override in prompt.'}

    length = len(prompt or '')
    for ceiling, route in _TIERS:
        if length < ceiling:
            return {'route': route, 'reason': f'Prompt is {length} chars (under {ceiling}).'}

    return {'route': _FALLBACK, 'reason': f'Prompt is {length} chars (over {_TIERS[-1][0]}).'}


__all__ = ['classify']
