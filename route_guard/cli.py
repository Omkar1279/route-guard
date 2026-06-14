from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from route_guard import budget, router, state

_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config.json'
_CONFIG_ERROR: Exception | None = None
CONFIG: dict[str, Any] | None = None

try:
    CONFIG = json.loads(_CONFIG_PATH.read_text(encoding='utf-8'))
except Exception as exc:  # pragma: no cover
    _CONFIG_ERROR = exc


def _config() -> dict[str, Any]:
    if _CONFIG_ERROR is not None:
        raise _CONFIG_ERROR
    if CONFIG is None:
        raise RuntimeError('Config was not loaded.')
    return CONFIG


def _read_input() -> dict[str, Any] | None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('input must be a JSON object')
    except (json.JSONDecodeError, ValueError) as exc:
        print(f'[route-guard] Invalid input: {exc}', file=sys.stderr)
        return None
    return data


def _hook_output(message: str) -> str:
    return json.dumps({'hookSpecificOutput': {'additionalContext': message}})


def _handle_user_prompt_submit(data: dict[str, Any]) -> int:
    try:
        state.set_workspace_root(data.get('cwd'))
        prompt = data.get('prompt') or ''
        if not prompt:
            return 0

        directive = router.process_prompt(prompt, _config())
        print(_hook_output(directive))
        return 0
    except Exception as exc:
        print(f'[route-guard] Error in user_prompt_submit: {exc}', file=sys.stderr)
        return 0


def _handle_pre_tool_use(data: dict[str, Any]) -> int:
    try:
        state.set_workspace_root(data.get('cwd'))
        current_state = state.read_state()
        result = budget.evaluate_spawn(current_state, data.get('tool_input') or {}, _config())

        if result.get('allowed'):
            state.with_state(
                lambda s: {
                    **s,
                    'spawns_attempted': int(s.get('spawns_attempted', 0)) + 1,
                }
            )
            return 0

        sys.stderr.write(budget.build_block_message(current_state, result.get('reason', 'Blocked'), _config()))
        return 2
    except Exception as exc:
        print(f'[route-guard] Error in pre_tool_use: {exc}', file=sys.stderr)
        return 0


def _handle_post_tool_use(data: dict[str, Any]) -> int:
    try:
        state.set_workspace_root(data.get('cwd'))

        usage = data.get('usage') or {}
        budget.record_tokens(usage.get('input_tokens', 0), usage.get('output_tokens', 0))
        budget.increment_file_edits(data.get('tool_name') or '')

        state_after = state.read_state()
        message = budget.check_escalation(state_after, _config())
        if message:
            print(_hook_output(message))
        return 0
    except Exception as exc:
        print(f'[route-guard] Error in post_tool_use: {exc}', file=sys.stderr)
        return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print('[route-guard] Missing hook verb.', file=sys.stderr)
        return 0

    verb = args[0]
    data = _read_input()
    if data is None:
        return 0

    if verb == 'user_prompt_submit':
        return _handle_user_prompt_submit(data)
    if verb == 'pre_tool_use':
        return _handle_pre_tool_use(data)
    if verb == 'post_tool_use':
        return _handle_post_tool_use(data)

    print(f'[route-guard] Unknown hook verb: {verb}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
