"""Project MCP through a real OBS child in an isolated E2E lane."""
from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

import pytest

from tests.e2e.harness.suites.test_inc11_stability import _jsonl_rows, _tool_results

pytestmark = [pytest.mark.suite('project-mcp'), pytest.mark.needs(all_lanes=False, credential=('dev', 'sub'))]
PYTHON = '/workspace/obs/.venv/bin/python'


async def test_project_http_tool(lane):
    helper = str(Path(lane.info['code_dir']) / 'tests/e2e/harness/mcp_http_stub.py')
    control = await lane.exec(PYTHON, helper, 'serve', env={'OBS_E2E_LANE': ''}, timeout=10)
    assert control['rc'] != 0 and 'requires an isolated E2E lane' in control['stderr'], control
    serving = asyncio.create_task(lane.exec(PYTHON, helper, 'serve', timeout=300))
    try:
        ready = await lane.exec(PYTHON, helper, 'ready', timeout=30)
        assert ready['rc'] == 0, ready
        topic = await lane.new_topic('WP02 project MCP')
        parent = await lane.turn(topic, 'Reply exactly READY; use no tools.')
        assert 'READY' in parent.text and parent.end.get('session_id'), parent.end
        candidate = lane.info.get('cli_path')
        if candidate:
            canary = lane.lane_dir / 'state' / 'obs-cli-canary.json'
            canary.write_text(json.dumps({'cli_path': candidate, 'agents': [], 'teams': [parent.end['team']],
                                         'new_sessions_only': True}))
        mark = lane.events.mark()
        result = await lane.control('--json', '--as', 'wp02', 'launch', '--parent-team', parent.end['team'],
                                    '--parent', parent.end['agent_name'], '--name', 'WP02 MCP child', '--fresh',
                                    '--prompt', 'Call mcp__wp02__probe exactly once with empty arguments. '
                                    'Then reply with the exact text the tool returns. Do not use any other tool.')
        assert result['rc'] == 0, result
        start = await lane.wait('turn_start', after=mark,
                                match=lambda event: event.get('team') == parent.end['team']
                                and bool(event.get('agent_name'))
                                and event['agent_name'] != parent.end['agent_name'], timeout=120)
        build = await lane.wait('session_build', agent=start['agent_name'], after=start, timeout=120)
        end = await lane.wait('turn_end', agent=start['agent_name'], after=build, timeout=180)
        processes = await lane.procs()
        argv0 = [row['args'][0] for row in processes if row.get('args') and '--output-format' in row['args']]
        rows = _jsonl_rows(lane, end['session_id'])
        calls = _tool_results(rows)
        messages = await lane.telegram.messages(start['topic_id'], from_bot=True)
        before = [row for row in lane.reqlog(agent=start['agent_name']) if row['_source'] == 'proxy']
        assert before, 'missing pre-restart proxy requests'
        wire_root = lane.lane_dir / 'logs/cache-proxy/requests'
        wire_path = next(wire_root.glob('**/' + before[-1]['req_id'] + '.wire.json.gz'))
        with gzip.open(wire_path, 'rt') as stream:
            before_wire = json.load(stream)
        tool_names = [tool['name'] for tool in before_wire.get('tools', [])]
        await lane.restart_obs('maintenance')
        resumed = await lane.turn(start['topic_id'], 'Reply exactly CACHE-CONTINUED; do not call tools.', timeout=180)
        after = [row for row in lane.reqlog(agent=start['agent_name']) if row['_source'] == 'proxy' and row['ts'] > before[-1]['ts']]
        assert after, 'missing post-restart proxy request'
        proof = {'start': start, 'build': build, 'end': end, 'argv0': argv0, 'tool_results': calls,
                 'messages': messages, 'tool_names': tool_names, 'resumed': resumed.end,
                 'before_cache': {'usage': before[-1]['usage'], 'ttl': before[-1]['norm']['ttl_pin']},
                 'after_cache': {'usage': after[0]['usage'], 'ttl': after[0]['norm']['ttl_pin']}}
        target = lane.run_dir / 'tests' / lane.name / 'project-mcp.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(proof, indent=2, default=str) + '\n')
        assert resumed.end.get('session_id') == end['session_id'] and not resumed.end.get('failed'), resumed.end
        assert resumed.end.get('cli_runtime') == (f'cli:{Path(candidate).name}' if candidate else 'cc-2.1.59'), resumed.end
        assert 'mcp__wp02__probe' in tool_names and not any('wp02-disabled' in name for name in tool_names), tool_names
        assert before[-1]['norm']['ttl_pin']['effective'] == after[0]['norm']['ttl_pin']['effective'], proof
        pre_usage, post_usage = before[-1]['usage'], after[0]['usage']
        assert post_usage['cache_read'] >= 0.9 * (pre_usage['cache_read'] + pre_usage['cache_creation']), proof
        assert post_usage['cache_creation'] < 0.1 * post_usage['cache_read'], proof
        assert not end.get('failed'), end
        assert end.get('cli_runtime') == (f'cli:{Path(candidate).name}' if candidate else 'cc-2.1.59'), end
        # rc2 emits session_build before runtime selection; keep it as evidence but judge argv0 and turn_end.
        assert (candidate in argv0) if candidate else any('claude_agent_sdk/_bundled/claude' in arg for arg in argv0), argv0
        assert any(row['tool'] == 'mcp__wp02__probe' and not row['is_error']
                   and 'WP02-FIXED-MCP-RESULT' in row['head'] for row in calls), calls
        assert any('WP02-FIXED-MCP-RESULT' in (item.get('text') or '') for item in messages), messages
    finally:
        await lane.exec(PYTHON, helper, 'stop', timeout=10)
        await serving
