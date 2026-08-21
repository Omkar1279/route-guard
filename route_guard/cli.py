"""Hook entry point: stdin JSON -> dispatch -> stdout JSON + exit code.

Every handler swallows its exceptions and exits 0. A governor that crashes a
user's session is worse than a governor that silently stops governing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from route_guard import budget, router, state

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config.json'

_config_cache: Optional[dict[str, Any]] = None


def load_config() -> dict[str, Any]:
    global _config_cache
    if _config_cache is None:
        parsed = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        if not isinstance(parsed, dict):
            raise ValueError(f'{CONFIG_PATH} must contain a JSON object')
        _config_cache = parsed
    return _config_cache


def _emit(event: str, **fields: Any) -> None:
    """Hook output must nest under hookSpecificOutput with a hookEventName."""
    print(json.dumps({'hookSpecificOutput': {'hookEventName': event, **fields}}))


def _bind(data: dict[str, Any]) -> None:
    state.set_context(data.get('cwd'), data.get('session_id'))


def _handle_user_prompt_submit(data: dict[str, Any]) -> int:
    _bind(data)
    prompt = data.get('prompt') or ''
    if not prompt:
        return 0

    _emit('UserPromptSubmit', additionalContext=router.process_prompt(prompt, load_config()))
    return 0


def _handle_pre_tool_use(data: dict[str, Any]) -> int:
    _bind(data)
    config = load_config()
    current = state.read_state()
    result = budget.evaluate_spawn(current, data.get('tool_input') or {}, config)
    notice = budget.commit_spawn(result, config)

    if not result.get('allowed'):
        _emit(
            'PreToolUse',
            permissionDecision='deny',
            permissionDecisionReason=budget.build_block_reason(
                current, result.get('reason', 'Blocked by route-guard.'), config
            ),
        )
    elif notice:
        # Allowed, but the route had to move to pay for it. Say so.
        _emit('PreToolUse', additionalContext=notice)
    return 0


def _handle_post_tool_use(data: dict[str, Any]) -> int:
    _bind(data)
    if data.get('agent_id'):
        # Subagent tool calls bill against their own transcript, not this turn.
        return 0

    notice = budget.record_tool_use(
        data.get('transcript_path'), data.get('tool_name') or '', load_config()
    )
    if notice:
        _emit('PostToolUse', additionalContext=notice)
    return 0


_HANDLERS = {
    'user_prompt_submit': _handle_user_prompt_submit,
    'pre_tool_use': _handle_pre_tool_use,
    'post_tool_use': _handle_post_tool_use,
}


def main(argv: Optional[list[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(f'[route-guard] Missing hook verb. Expected one of: {", ".join(_HANDLERS)}', file=sys.stderr)
        return 0

    verb = args[0]
    handler = _HANDLERS.get(verb)
    if handler is None:
        print(f'[route-guard] Unknown hook verb: {verb}', file=sys.stderr)
        return 0

    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            raise ValueError('hook input must be a JSON object')
    except (json.JSONDecodeError, ValueError) as exc:
        print(f'[route-guard] Invalid input for {verb}: {exc}', file=sys.stderr)
        return 0

    try:
        return handler(data)
    except Exception as exc:
        print(f'[route-guard] Error in {verb}: {exc}', file=sys.stderr)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
