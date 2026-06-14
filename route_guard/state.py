from __future__ import annotations

import json
import os
import random
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Optional

_workspace_root: Optional[Path] = None

DEFAULT_STATE: dict[str, Any] = {
    'current_route': None,
    'route_reason': None,
    'route_budget': None,
    'turn_number': 0,
    'cumulative_tokens_this_turn': 0,
    'route_tokens_used': 0,
    'spawns_attempted': 0,
    'file_edits_this_turn': 0,
    'escalations': [],
}


def set_workspace_root(cwd: Optional[str]) -> None:
    global _workspace_root
    _workspace_root = Path(cwd).resolve() if cwd else None


def _workspace() -> Path:
    return _workspace_root or Path.cwd()


def state_path() -> Path:
    return _workspace() / '.claude' / '.route-guard-state.json'


def _lock_path() -> Path:
    return Path(str(state_path()) + '.lock')


def acquire_lock(max_retries: int = 10, retry_delay_ms: int = 20) -> bool:
    lock = _lock_path()
    stale_lock_seconds = 5

    for _ in range(max_retries):
        try:
            lock.mkdir()
            (lock / 'pid').write_text(str(os.getpid()), encoding='utf-8')
            return True
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age > stale_lock_seconds:
                    release_lock()
                    continue
            except FileNotFoundError:
                continue

            jitter_ms = random.randint(0, retry_delay_ms)
            time.sleep((retry_delay_ms + jitter_ms) / 1000.0)
        except OSError:
            return False

    return False


def release_lock() -> None:
    lock = _lock_path()
    try:
        pid_file = lock / 'pid'
        if pid_file.exists():
            pid_file.unlink()
        lock.rmdir()
    except OSError:
        pass


def _default_state_copy() -> dict[str, Any]:
    return deepcopy(DEFAULT_STATE)


def read_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return _default_state_copy()

    try:
        parsed = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(parsed, dict):
            return _default_state_copy()
    except (json.JSONDecodeError, OSError):
        return _default_state_copy()

    merged = _default_state_copy()
    merged.update(parsed)
    if not isinstance(merged.get('escalations'), list):
        merged['escalations'] = []
    return merged


def write_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = Path(f'{path}.tmp.{os.getpid()}')
    tmp_path.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
    tmp_path.replace(path)


def with_state(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    lock_acquired = acquire_lock()
    try:
        current = read_state()
        updated = fn(current)
        write_state(updated)
        return updated
    finally:
        if lock_acquired:
            release_lock()


def reset_turn(state: dict[str, Any]) -> dict[str, Any]:
    state['turn_number'] = int(state.get('turn_number', 0)) + 1
    state['cumulative_tokens_this_turn'] = 0
    state['route_tokens_used'] = 0
    state['spawns_attempted'] = 0
    state['file_edits_this_turn'] = 0
    return state


__all__ = [
    'DEFAULT_STATE',
    'set_workspace_root',
    'state_path',
    'read_state',
    'write_state',
    'with_state',
    'reset_turn',
]
