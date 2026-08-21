from __future__ import annotations

import json
import os
import random
import re
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_workspace_root: Optional[Path] = None
_session_id: str = 'default'

_UNSAFE_SESSION_CHARS = re.compile(r'[^A-Za-z0-9_-]')

DEFAULT_STATE: dict[str, Any] = {
    'session_id': None,
    'turn_number': 0,
    'turn_started_at': None,
    'current_route': None,
    'route_reason': None,
    'route_budget': 0,
    'tokens_this_turn': 0,
    'context_tokens': 0,
    'spawns_used': 0,
    'spawns_blocked': 0,
    'file_edits_this_turn': 0,
    'escalations': [],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_context(cwd: Optional[str], session_id: Optional[str] = None) -> None:
    """Bind state to a workspace and a session. Called once per hook invocation."""
    global _workspace_root, _session_id
    _workspace_root = Path(cwd).resolve() if cwd else None
    safe = _UNSAFE_SESSION_CHARS.sub('_', session_id or '')
    _session_id = safe or 'default'


def session_id() -> str:
    return _session_id


def _workspace() -> Path:
    return _workspace_root or Path.cwd()


def state_path() -> Path:
    return _workspace() / '.claude' / 'route-guard' / f'{_session_id}.json'


def _lock_path() -> Path:
    return Path(str(state_path()) + '.lock')


def acquire_lock(max_retries: int = 10, retry_delay_ms: int = 20) -> bool:
    lock = _lock_path()
    stale_lock_seconds = 5

    for _ in range(max_retries):
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.mkdir()
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_lock_seconds:
                    release_lock()
            except OSError:
                pass
            jitter_ms = random.randint(0, retry_delay_ms)
            time.sleep((retry_delay_ms + jitter_ms) / 1000.0)
        except OSError:
            return False

    return False


def release_lock() -> None:
    try:
        _lock_path().rmdir()
    except OSError:
        pass


def _default_state_copy() -> dict[str, Any]:
    return deepcopy(DEFAULT_STATE)


def read_state() -> dict[str, Any]:
    path = state_path()
    merged = _default_state_copy()

    try:
        parsed = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        parsed = None

    if isinstance(parsed, dict):
        merged.update(parsed)
    if not isinstance(merged.get('escalations'), list):
        merged['escalations'] = []

    merged['session_id'] = _session_id
    return merged


def write_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = Path(f'{path}.tmp.{os.getpid()}')
    tmp_path.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
    tmp_path.replace(path)


def with_state(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Read-modify-write the session state under a lock."""
    lock_acquired = acquire_lock()
    try:
        updated = fn(read_state())
        write_state(updated)
        return updated
    finally:
        if lock_acquired:
            release_lock()


def start_turn(state: dict[str, Any]) -> dict[str, Any]:
    state['turn_number'] = int(state.get('turn_number') or 0) + 1
    state['turn_started_at'] = now_iso()
    state['tokens_this_turn'] = 0
    state['spawns_used'] = 0
    state['spawns_blocked'] = 0
    state['file_edits_this_turn'] = 0
    return state


__all__ = [
    'DEFAULT_STATE',
    'now_iso',
    'read_state',
    'session_id',
    'set_context',
    'start_turn',
    'state_path',
    'with_state',
    'write_state',
]
