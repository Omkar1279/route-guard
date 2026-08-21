"""Token accounting from the Claude Code session transcript.

Hook payloads carry no token counts, so usage is read from the JSONL transcript
whose path every hook receives as ``transcript_path``.

Two details drive the implementation:

* A single API response is written as several transcript lines (one per content
  block), each repeating the *same* ``message.usage``. Summing lines inflates
  counts 2-3x, so entries are deduplicated by ``message.id``.
* ``cache_read_input_tokens`` re-counts the whole conversation on every turn.
  Turn cost therefore sums input + cache creation + output only; cache reads are
  reported separately as the live context size.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + '+00:00' if value.endswith('Z') else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _turn_tokens(usage: dict[str, Any]) -> int:
    return (
        _as_int(usage.get('input_tokens'))
        + _as_int(usage.get('cache_creation_input_tokens'))
        + _as_int(usage.get('output_tokens'))
    )


def _context_tokens(usage: dict[str, Any]) -> int:
    return (
        _as_int(usage.get('input_tokens'))
        + _as_int(usage.get('cache_creation_input_tokens'))
        + _as_int(usage.get('cache_read_input_tokens'))
    )


def _message_key(entry: dict[str, Any], message: dict[str, Any]) -> Optional[str]:
    for candidate in (message.get('id'), entry.get('requestId'), entry.get('uuid')):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def read_usage(transcript_path: Optional[str], since: Optional[datetime]) -> dict[str, int]:
    """Sum assistant token usage.

    ``tokens`` counts responses at or after ``since``; when ``since`` is None the
    turn boundary is unknown and the count stays 0 rather than charging the whole
    transcript to the current turn. ``context`` is the newest response's context
    size and is independent of ``since``.
    """
    result = {'tokens': 0, 'context': 0, 'responses': 0}
    if not transcript_path:
        return result

    try:
        handle = Path(transcript_path).open('r', encoding='utf-8')
    except OSError:
        return result

    seen: set[str] = set()
    latest: Optional[datetime] = None

    with handle:
        for line in handle:
            if '"assistant"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get('type') != 'assistant':
                continue
            if entry.get('isSidechain'):
                continue

            message = entry.get('message')
            if not isinstance(message, dict):
                continue
            usage = message.get('usage')
            if not isinstance(usage, dict):
                continue

            timestamp = parse_timestamp(entry.get('timestamp'))
            if timestamp is not None and (latest is None or timestamp >= latest):
                latest = timestamp
                result['context'] = _context_tokens(usage)

            key = _message_key(entry, message)
            if key is None or key in seen:
                continue
            seen.add(key)

            if since is None or timestamp is None or timestamp < since:
                continue
            result['tokens'] += _turn_tokens(usage)
            result['responses'] += 1

    return result


__all__ = ['parse_timestamp', 'read_usage']
