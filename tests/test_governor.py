from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from route_guard import budget, classifier, cli, router, state, transcript

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def config() -> dict:
    return json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    state.set_context(str(tmp_path), 'test-session')
    return tmp_path


def _assistant_line(message_id: str, when: datetime, **usage: int) -> str:
    return json.dumps(
        {
            'type': 'assistant',
            'uuid': f'{message_id}-{when.timestamp()}',
            'timestamp': when.isoformat(),
            'isSidechain': False,
            'message': {'id': message_id, 'role': 'assistant', 'usage': usage},
        }
    )


def _write_transcript(path: Path, lines: list[str]) -> str:
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(path)


# --- classifier ------------------------------------------------------------


def test_classifier_covers_all_four_routes() -> None:
    assert classifier.classify('fix typo')['route'] == 'trivial'
    assert classifier.classify('x' * 200)['route'] == 'small'
    assert classifier.classify('x' * 800)['route'] == 'medium'
    assert classifier.classify('x' * 2000)['route'] == 'large'


def test_classifier_honours_explicit_override() -> None:
    result = classifier.classify('fix typo [route:large]')
    assert result['route'] == 'large'
    assert 'override' in result['reason'].lower()


def test_classifier_override_is_case_insensitive() -> None:
    assert classifier.classify('[ROUTE: Medium] hi')['route'] == 'medium'


# --- transcript accounting -------------------------------------------------


def test_transcript_deduplicates_repeated_usage(tmp_path: Path) -> None:
    """One API response spans several transcript lines that repeat the same usage."""
    when = datetime.now(timezone.utc)
    path = _write_transcript(
        tmp_path / 't.jsonl',
        [
            _assistant_line('msg_1', when, input_tokens=2, cache_creation_input_tokens=1000, output_tokens=500),
            _assistant_line('msg_1', when, input_tokens=2, cache_creation_input_tokens=1000, output_tokens=500),
            _assistant_line('msg_1', when, input_tokens=2, cache_creation_input_tokens=1000, output_tokens=500),
        ],
    )
    usage = transcript.read_usage(path, when - timedelta(minutes=1))
    assert usage['responses'] == 1
    assert usage['tokens'] == 1502


def test_transcript_excludes_cache_reads_from_turn_cost(tmp_path: Path) -> None:
    when = datetime.now(timezone.utc)
    path = _write_transcript(
        tmp_path / 't.jsonl',
        [
            _assistant_line(
                'msg_1',
                when,
                input_tokens=10,
                cache_creation_input_tokens=100,
                cache_read_input_tokens=90000,
                output_tokens=40,
            )
        ],
    )
    usage = transcript.read_usage(path, when - timedelta(minutes=1))
    assert usage['tokens'] == 150
    assert usage['context'] == 90110


def test_transcript_ignores_responses_before_the_turn(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    path = _write_transcript(
        tmp_path / 't.jsonl',
        [
            _assistant_line('old', now - timedelta(hours=1), output_tokens=9999),
            _assistant_line('new', now, output_tokens=25),
        ],
    )
    assert transcript.read_usage(path, now - timedelta(minutes=1))['tokens'] == 25


def test_transcript_skips_sidechain_entries(tmp_path: Path) -> None:
    when = datetime.now(timezone.utc)
    entry = json.loads(_assistant_line('sub', when, output_tokens=5000))
    entry['isSidechain'] = True
    path = _write_transcript(tmp_path / 't.jsonl', [json.dumps(entry)])
    assert transcript.read_usage(path, when - timedelta(minutes=1))['tokens'] == 0


def test_transcript_without_turn_marker_counts_nothing(tmp_path: Path) -> None:
    """Fail closed: an unknown turn boundary must not charge the whole transcript."""
    path = _write_transcript(
        tmp_path / 't.jsonl', [_assistant_line('msg_1', datetime.now(timezone.utc), output_tokens=50000)]
    )
    assert transcript.read_usage(path, None)['tokens'] == 0


def test_transcript_missing_file_is_not_fatal() -> None:
    assert transcript.read_usage('/nonexistent/path.jsonl', datetime.now(timezone.utc))['tokens'] == 0


def test_record_tool_use_is_idempotent(workspace: Path, config: dict) -> None:
    state.with_state(state.start_turn)
    when = datetime.now(timezone.utc) + timedelta(seconds=1)
    path = _write_transcript(
        workspace / 't.jsonl', [_assistant_line('msg_1', when, output_tokens=700)]
    )

    budget.record_tool_use(path, 'Read', config)
    first = state.read_state()['tokens_this_turn']
    budget.record_tool_use(path, 'Read', config)
    assert state.read_state()['tokens_this_turn'] == first == 700


# --- spawn gating ----------------------------------------------------------


def test_legitimate_spawn_promotes_a_route_that_budgets_no_agents(
    workspace: Path, config: dict
) -> None:
    """A short prompt guessed 'small' must not permanently forbid real delegation."""
    current = {**state.DEFAULT_STATE, 'current_route': 'small'}
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)

    assert result['allowed'] is True
    assert result['promote_to'] == 'medium'


def test_promotion_is_recorded_and_announced(workspace: Path, config: dict) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'trivial'})
    result = budget.evaluate_spawn(dict(state.read_state()), {'prompt': 'a' * 250}, config)
    notice = budget.commit_spawn(result, config)

    assert notice and 'escalated from "trivial" to "medium"' in notice

    updated = state.read_state()
    assert updated['current_route'] == 'medium'
    assert updated['route_budget'] == 120000
    assert updated['spawns_used'] == 1
    assert updated['escalations'][-1]['to'] == 'medium'


def test_throwaway_spawn_cannot_buy_a_promotion(workspace: Path, config: dict) -> None:
    """The overkill rule is judged before promotion, so it can't be escaped."""
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'trivial'})
    result = budget.evaluate_spawn(dict(state.read_state()), {'prompt': 'list files'}, config)
    budget.commit_spawn(result, config)

    assert result['allowed'] is False
    assert 'overkill' in result['reason'].lower()
    assert state.read_state()['current_route'] == 'trivial'


def test_promotion_does_not_lift_an_exhausted_cap(workspace: Path, config: dict) -> None:
    """Asking for a third agent on medium is a cap denial, not a route to large."""
    current = {**state.DEFAULT_STATE, 'current_route': 'medium', 'spawns_used': 2}
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)

    assert result['allowed'] is False
    assert 'spawn cap' in result['reason'].lower()
    assert 'promote_to' not in result


def test_promotion_respects_a_config_that_forbids_all_agents(workspace: Path) -> None:
    """Zeroing every max_spawns is a real decision and must be honoured."""
    no_agents = {'routes': {name: {'budget': 10000, 'max_spawns': 0} for name in budget.ROUTE_ORDER}}
    current = {**state.DEFAULT_STATE, 'current_route': 'trivial'}
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 250}, no_agents)

    assert result['allowed'] is False
    assert 'no configured route permits agents' in result['reason'].lower()


def test_promotion_judges_budget_at_the_promoted_route(workspace: Path, config: dict) -> None:
    """55k tokens blows the small budget but sits well inside medium's."""
    current = {**state.DEFAULT_STATE, 'current_route': 'small', 'tokens_this_turn': 55000}
    assert budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)['allowed'] is True


def test_evaluate_spawn_allows_medium_within_limits(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'medium', 'spawns_used': 1}
    assert budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)['allowed'] is True


def test_evaluate_spawn_blocks_at_spawn_cap(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'medium', 'spawns_used': 2}
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)
    assert result['allowed'] is False
    assert 'spawn cap' in result['reason'].lower()


def test_blocked_spawns_do_not_consume_the_cap(workspace: Path, config: dict) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'medium'})
    budget.commit_spawn({'allowed': False}, config)
    budget.commit_spawn({'allowed': False}, config)

    current = state.read_state()
    assert current['spawns_blocked'] == 2
    assert budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)['allowed'] is True


def test_evaluate_spawn_blocks_near_budget(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'medium', 'tokens_this_turn': 110000}
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 250}, config)
    assert result['allowed'] is False
    assert 'budget ceiling' in result['reason'].lower()


def test_evaluate_spawn_blocks_expensive_delegation(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'large', 'tokens_this_turn': 200000}
    result = budget.evaluate_spawn(current, {'prompt': 'a' * 1200}, config)
    assert result['allowed'] is False
    assert 'delegation expensive' in result['reason'].lower()


def test_evaluate_spawn_blocks_short_prompt(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'medium'}
    result = budget.evaluate_spawn(current, {'prompt': 'do it'}, config)
    assert result['allowed'] is False
    assert 'overkill' in result['reason'].lower()


def test_evaluate_spawn_allows_when_no_route_assigned(workspace: Path, config: dict) -> None:
    result = budget.evaluate_spawn(dict(state.DEFAULT_STATE), {'prompt': 'a' * 250}, config)
    assert result['allowed'] is True


def test_build_block_reason_shape(workspace: Path, config: dict) -> None:
    current = {**state.DEFAULT_STATE, 'current_route': 'small', 'tokens_this_turn': 30000}
    parsed = json.loads(budget.build_block_reason(current, 'blocked for test', config))
    assert parsed['blocked'] is True
    assert parsed['route'] == 'small'
    assert parsed['budget'] == '30000/40000'
    assert len(parsed['options']) == 2


# --- escalation ------------------------------------------------------------


def test_escalation_on_token_overrun(workspace: Path, config: dict) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'trivial', 'tokens_this_turn': 13000})
    notice = budget.check_escalation(config)

    assert notice and 'escalated' in notice.lower()
    updated = state.read_state()
    assert updated['current_route'] == 'small'
    assert updated['route_budget'] == 40000
    assert updated['escalations'][-1]['from'] == 'trivial'
    assert updated['escalations'][-1]['to'] == 'small'


def test_escalation_walks_past_multiple_routes(workspace: Path, config: dict) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'trivial', 'tokens_this_turn': 200000})
    budget.check_escalation(config)
    assert state.read_state()['current_route'] == 'large'


def test_escalation_on_file_edits(workspace: Path, config: dict) -> None:
    state.write_state(
        {**state.DEFAULT_STATE, 'current_route': 'trivial', 'tokens_this_turn': 100, 'file_edits_this_turn': 6}
    )
    notice = budget.check_escalation(config)
    assert notice and 'file edits' in notice.lower()
    assert state.read_state()['current_route'] == 'small'


def test_no_escalation_within_budget(workspace: Path, config: dict) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'medium', 'tokens_this_turn': 100})
    assert budget.check_escalation(config) is None


def test_large_route_never_escalates(workspace: Path, config: dict) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'large', 'tokens_this_turn': 999999})
    assert budget.check_escalation(config) is None
    assert state.read_state()['current_route'] == 'large'


def test_record_tool_use_only_counts_write_tools(workspace: Path, config: dict) -> None:
    state.write_state(dict(state.DEFAULT_STATE))
    for tool in ('Read', 'Edit', 'Write', 'Bash'):
        budget.record_tool_use(None, tool, config)
    assert state.read_state()['file_edits_this_turn'] == 2


# --- state -----------------------------------------------------------------


def test_start_turn_resets_per_turn_counters(workspace: Path) -> None:
    state.write_state(
        {
            **state.DEFAULT_STATE,
            'turn_number': 2,
            'tokens_this_turn': 500,
            'spawns_used': 3,
            'file_edits_this_turn': 4,
        }
    )
    updated = state.with_state(state.start_turn)

    assert updated['turn_number'] == 3
    assert updated['tokens_this_turn'] == 0
    assert updated['spawns_used'] == 0
    assert updated['file_edits_this_turn'] == 0
    assert updated['turn_started_at'] is not None


def test_state_is_isolated_per_session(tmp_path: Path) -> None:
    state.set_context(str(tmp_path), 'session-a')
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'large'})

    state.set_context(str(tmp_path), 'session-b')
    assert state.read_state()['current_route'] is None

    state.set_context(str(tmp_path), 'session-a')
    assert state.read_state()['current_route'] == 'large'


def test_with_state_is_safe_under_threads(workspace: Path) -> None:
    state.write_state(dict(state.DEFAULT_STATE))

    def worker() -> None:
        for _ in range(10):
            state.with_state(lambda s: {**s, 'spawns_used': int(s.get('spawns_used', 0)) + 1})

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state.read_state()['spawns_used'] == 50


def test_corrupt_state_falls_back_to_defaults(workspace: Path) -> None:
    path = state.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{not json', encoding='utf-8')
    assert state.read_state()['current_route'] is None


# --- router ----------------------------------------------------------------


def test_router_assigns_route_and_starts_turn(workspace: Path, config: dict) -> None:
    directive = router.process_prompt('x' * 200, config)
    assert 'SMALL' in directive

    current = state.read_state()
    assert current['current_route'] == 'small'
    assert current['route_budget'] == 40000
    assert current['turn_started_at'] is not None
    assert current['turn_number'] == 1


# --- hook contract ---------------------------------------------------------


def _run_hook(verb: str, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['bash', str(ROOT / 'hooks' / f'{verb}.sh')],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_hook_user_prompt_submit_emits_valid_contract(workspace: Path) -> None:
    result = _run_hook(
        'user_prompt_submit',
        {'session_id': 'test-session', 'cwd': str(workspace), 'prompt': 'x' * 200},
    )
    assert result.returncode == 0, result.stderr

    emitted = json.loads(result.stdout)['hookSpecificOutput']
    assert emitted['hookEventName'] == 'UserPromptSubmit'
    assert 'SMALL' in emitted['additionalContext']


def test_hook_pre_tool_use_denies_with_valid_contract(workspace: Path) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'small', 'route_budget': 40000})

    result = _run_hook(
        'pre_tool_use',
        {
            'session_id': 'test-session',
            'cwd': str(workspace),
            'tool_name': 'Task',
            'tool_input': {'prompt': 'list files'},
        },
    )
    assert result.returncode == 0, result.stderr

    emitted = json.loads(result.stdout)['hookSpecificOutput']
    assert emitted['hookEventName'] == 'PreToolUse'
    assert emitted['permissionDecision'] == 'deny'

    reason = json.loads(emitted['permissionDecisionReason'])
    assert reason['blocked'] is True
    assert reason['route'] == 'small'
    assert 'overkill' in reason['reason'].lower()


def test_hook_pre_tool_use_announces_promotion(workspace: Path) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'small', 'route_budget': 40000})

    result = _run_hook(
        'pre_tool_use',
        {
            'session_id': 'test-session',
            'cwd': str(workspace),
            'tool_name': 'Task',
            'tool_input': {'prompt': 'x' * 300},
        },
    )
    assert result.returncode == 0, result.stderr

    emitted = json.loads(result.stdout)['hookSpecificOutput']
    assert emitted['hookEventName'] == 'PreToolUse'
    assert 'permissionDecision' not in emitted
    assert 'escalated from "small" to "medium"' in emitted['additionalContext']
    assert state.read_state()['current_route'] == 'medium'


def test_hook_pre_tool_use_stays_silent_when_allowed(workspace: Path) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'medium'})
    result = _run_hook(
        'pre_tool_use',
        {
            'session_id': 'test-session',
            'cwd': str(workspace),
            'tool_name': 'Task',
            'tool_input': {'prompt': 'x' * 300},
        },
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ''
    assert state.read_state()['spawns_used'] == 1


def test_hook_post_tool_use_escalates_end_to_end(workspace: Path) -> None:
    """Full path: assign a route, burn the budget, expect an escalation notice."""
    prompt_result = _run_hook(
        'user_prompt_submit',
        {'session_id': 'test-session', 'cwd': str(workspace), 'prompt': 'fix a typo'},
    )
    assert prompt_result.returncode == 0
    assert state.read_state()['current_route'] == 'trivial'

    transcript_path = _write_transcript(
        workspace / 'transcript.jsonl',
        [
            _assistant_line(
                'msg_1',
                datetime.now(timezone.utc) + timedelta(seconds=1),
                input_tokens=5,
                cache_creation_input_tokens=20000,
                cache_read_input_tokens=400000,
                output_tokens=3000,
            )
        ],
    )

    result = _run_hook(
        'post_tool_use',
        {
            'session_id': 'test-session',
            'cwd': str(workspace),
            'transcript_path': transcript_path,
            'tool_name': 'Edit',
            'tool_input': {},
            'tool_response': {},
        },
    )
    assert result.returncode == 0, result.stderr

    emitted = json.loads(result.stdout)['hookSpecificOutput']
    assert emitted['hookEventName'] == 'PostToolUse'
    assert 'escalated' in emitted['additionalContext'].lower()

    current = state.read_state()
    assert current['tokens_this_turn'] == 23005
    assert current['context_tokens'] == 420005
    assert current['current_route'] == 'small'
    assert current['file_edits_this_turn'] == 1


def test_hook_post_tool_use_ignores_subagent_calls(workspace: Path) -> None:
    state.write_state({**state.DEFAULT_STATE, 'current_route': 'trivial', 'turn_started_at': state.now_iso()})
    transcript_path = _write_transcript(
        workspace / 'transcript.jsonl',
        [_assistant_line('msg_1', datetime.now(timezone.utc) + timedelta(seconds=1), output_tokens=50000)],
    )

    result = _run_hook(
        'post_tool_use',
        {
            'session_id': 'test-session',
            'cwd': str(workspace),
            'agent_id': 'agent-1',
            'transcript_path': transcript_path,
            'tool_name': 'Edit',
        },
    )
    assert result.returncode == 0
    assert state.read_state()['tokens_this_turn'] == 0


@pytest.mark.parametrize('verb', ['user_prompt_submit', 'pre_tool_use', 'post_tool_use'])
def test_hooks_never_fail_the_session_on_bad_input(verb: str) -> None:
    result = _run_hook(verb, {})
    assert result.returncode == 0

    garbage = subprocess.run(
        ['bash', str(ROOT / 'hooks' / f'{verb}.sh')],
        input='not json',
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert garbage.returncode == 0


def test_shipped_config_defines_every_route(config: dict) -> None:
    assert set(config['routes']) == set(budget.ROUTE_ORDER)
    for settings in config['routes'].values():
        assert settings['budget'] > 0
        assert settings['max_spawns'] >= 0


def test_config_loads_from_plugin_root() -> None:
    assert set(cli.load_config()['routes']) == set(budget.ROUTE_ORDER)
