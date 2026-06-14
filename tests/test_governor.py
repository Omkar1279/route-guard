from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from route_guard import budget, classifier, router, state


@pytest.fixture
def config() -> dict:
    root = Path(__file__).resolve().parent.parent
    return json.loads((root / 'config.json').read_text(encoding='utf-8'))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    state.set_workspace_root(str(tmp_path))
    return tmp_path


def test_classifier_is_length_only() -> None:
    """The old keyword classifier is removed; routing is only by prompt length now."""
    assert classifier.classify('fix typo') == {
        'route': 'small',
        'reason': 'Prompt under 80 chars.',
    }
    assert classifier.classify('x' * 79)['route'] == 'small'
    assert classifier.classify('x' * 80)['route'] == 'medium'


def test_evaluate_spawn_blocks_on_small(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'small', 'spawns_attempted': 0}
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)
    assert result['allowed'] is False
    assert 'disallowed' in result['reason'].lower()


def test_evaluate_spawn_allows_medium_within_limits(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'medium', 'spawns_attempted': 1}
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)
    assert result['allowed'] is True


def test_evaluate_spawn_blocks_at_spawn_cap(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'medium', 'spawns_attempted': 2}
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)
    assert result['allowed'] is False
    assert 'spawn cap' in result['reason'].lower()


def test_evaluate_spawn_blocks_near_budget(workspace: Path, config: dict) -> None:
    current = {
        **state.DEFAULT_STATE,
        'current_route': 'medium',
        'spawns_attempted': 0,
        'cumulative_tokens_this_turn': 110000,
    }
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)
    assert result['allowed'] is False
    assert 'budget ceiling' in result['reason'].lower()


def test_evaluate_spawn_blocks_expensive_delegation(workspace: Path, config: dict) -> None:
    current = {
        **state.DEFAULT_STATE,
        'current_route': 'large',
        'spawns_attempted': 0,
        'cumulative_tokens_this_turn': 200000,
    }
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 1200}, config)
    assert result['allowed'] is False
    assert 'delegation expensive' in result['reason'].lower()


def test_evaluate_spawn_blocks_short_prompt(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'medium', 'spawns_attempted': 0}
    result = budget.evaluate_spawn(current, {'prompt': 'do it'}, config)
    assert result['allowed'] is False
    assert 'overkill' in result['reason'].lower()


def test_check_escalation_tokens_trivial_to_small(workspace: Path, config: dict) -> None:
    current = {
        **state.DEFAULT_STATE,
        'current_route': 'trivial',
        'route_budget': 10000,
        'cumulative_tokens_this_turn': 13000,
    }
    state.write_state(current)
    msg = budget.check_escalation(current, config)
    assert msg is not None
    assert 'ESCALATION' in msg

    updated = state.read_state()
    assert updated['current_route'] == 'small'


def test_check_escalation_file_edits_trivial_to_small(workspace: Path, config: dict) -> None:
    current = {
        **state.DEFAULT_STATE,
        'current_route': 'trivial',
        'route_budget': 10000,
        'cumulative_tokens_this_turn': 100,
        'file_edits_this_turn': 6,
    }
    state.write_state(current)
    msg = budget.check_escalation(current, config)
    assert msg is not None
    assert 'file edits' in msg.lower()


def test_check_escalation_does_not_escalate_medium(workspace: Path, config: dict) -> None:
    current = {
        **state.DEFAULT_STATE,
        'current_route': 'medium',
        'route_budget': 120000,
        'cumulative_tokens_this_turn': 300000,
    }
    msg = budget.check_escalation(current, config)
    assert msg is None


def test_build_block_message_shape(workspace: Path, config: dict) -> None:
    current = {
        **state.DEFAULT_STATE,
        'current_route': 'small',
        'cumulative_tokens_this_turn': 30000,
        'spawns_attempted': 0,
    }
    msg = budget.build_block_message(current, 'blocked for test', config)
    parsed = json.loads(msg)
    assert parsed['blocked'] is True
    assert parsed['route'] == 'small'
    assert len(parsed['options']) == 2


def test_escalation_records_correct_from_to(workspace: Path, config: dict) -> None:
    current = {
        **state.DEFAULT_STATE,
        'current_route': 'trivial',
        'route_budget': 10000,
        'cumulative_tokens_this_turn': 13000,
    }
    state.write_state(current)
    budget.check_escalation(current, config)

    updated = state.read_state()
    assert updated['escalations']
    latest = updated['escalations'][-1]
    assert latest['from'] == 'trivial'
    assert latest['to'] == 'small'


def test_route_tokens_used_reset(workspace: Path) -> None:
    current = {
        **state.DEFAULT_STATE,
        'turn_number': 2,
        'route_tokens_used': 500,
    }
    updated = state.reset_turn(current)
    assert updated['turn_number'] == 3
    assert updated['route_tokens_used'] == 0


def test_with_state_is_safe_under_threads(workspace: Path) -> None:
    state.write_state({**state.DEFAULT_STATE, 'spawns_attempted': 0})

    def worker() -> None:
        for _ in range(10):
            state.with_state(
                lambda s: {
                    **s,
                    'spawns_attempted': int(s.get('spawns_attempted', 0)) + 1,
                }
            )

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state.read_state()['spawns_attempted'] == 50


def test_router_process_prompt_sets_route_and_budget(workspace: Path, config: dict) -> None:
    msg = router.process_prompt('x' * 40, config)
    assert '<route-guard>' in msg

    current = state.read_state()
    assert current['current_route'] == 'small'
    assert current['route_budget'] == 40000


def test_hook_integration_pre_tool_use_block(workspace: Path, config: dict) -> None:
    root = Path(__file__).resolve().parent.parent

    state.write_state(
        {
            **state.DEFAULT_STATE,
            'current_route': 'small',
            'route_budget': 40000,
            'route_reason': 'test',
        }
    )

    payload = {
        'tool_name': 'Task',
        'tool_input': {'prompt': 'x' * 300},
        'cwd': str(workspace),
    }
    result = subprocess.run(
        ['bash', str(root / 'hooks' / 'pre_tool_use.sh')],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=root,
    )

    assert result.returncode == 2
    blocked = json.loads(result.stderr)
    assert blocked['blocked'] is True
    assert blocked['route'] == 'small'
