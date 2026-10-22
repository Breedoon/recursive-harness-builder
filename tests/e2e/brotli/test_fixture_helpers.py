"""Fixture/script hygiene, not acceptance evidence for the mitigation."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('bun_baseline_fixture', ROOT/'baseline.py')
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def test_sse_tool_input_is_delivered_as_json_delta():
    block = {'type':'tool_use','id':'t','name':'WebFetch','input':{'url':'https://fixture.obs.test/page','prompt':'marker'}}
    raw = baseline.sse_message(block)
    rows = [json.loads(l[6:]) for l in raw.decode().splitlines() if l.startswith('data: ')]
    assert rows[1]['content_block']['input'] == {}
    assert json.loads(rows[2]['delta']['partial_json']) == block['input']
    assert rows[-2]['delta']['stop_reason'] == 'tool_use'


def test_sse_text_ends_turn():
    raw = baseline.sse_message({'type':'text','text':'BUN_WEBFETCH_OK'})
    assert b'BUN_WEBFETCH_OK' in raw
    assert b'end_turn' in raw
    assert baseline.digest(b'body') == '230d8358dc8e8890b4c58deeb62912ee2f20357ae92a5cc861b98e68fe31acb5'


def test_help_starts_no_fixture():
    p = subprocess.run([sys.executable,str(ROOT/'baseline.py'),'--help'],capture_output=True,text=True)
    assert p.returncode == 0
    assert '--run-id' in p.stdout
    assert not p.stderr


def test_preload_assets_parse_without_running_them():
    for name in ('trace_preload.cjs','attachment_probe.cjs','module_probe.cjs'):
        p = subprocess.run(['node','--check',str(ROOT/name)],capture_output=True,text=True)
        assert p.returncode == 0, p.stderr
