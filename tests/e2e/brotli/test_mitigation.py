"""Transport/identity hygiene; actual CLI/container proofs remain acceptance evidence."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
from obs_agent import bun_transport, cache_gate, cache_launch


def test_launch_env_is_uniform_idempotent_and_not_prompt_metadata(monkeypatch):
    monkeypatch.delenv('BUN_OPTIONS', raising=False)
    env = bun_transport.launch_environment({})
    assert set(env) == {'BUN_OPTIONS','OBS_BUN_DECODER_NODE','OBS_BUN_DECODER_SHA256'}
    assert bun_transport.launch_environment(env) == env
    assert env['OBS_BUN_DECODER_SHA256'] == bun_transport.decoder_identity()['sha256']
    with_other = bun_transport.launch_environment({'BUN_OPTIONS':'--preload /old.cjs'})
    assert with_other['BUN_OPTIONS'].endswith('--preload /old.cjs')


def test_runtime_assets_are_discovered_by_markers():
    names = cache_gate.sensitive_files(ROOT)
    for name in ['session.py','bun_transport.py','bun_transport_preload.cjs','bun_decoder_worker.cjs','cache_gate.py','cache_launch.py']:
        assert 'src/obs_agent/'+name in names
    assert 'scripts/cache_regression.py' in names


def test_actual_dependency_bytes_are_not_expected_hash_text(tmp_path):
    src = tmp_path/'src/obs_agent'
    src.mkdir(parents=True)
    dependency = tmp_path/'decoder'
    dependency.write_bytes(b'fixed-dependency-a')
    expected = hashlib.sha256(dependency.read_bytes()).hexdigest()
    (src/'bun_transport.py').write_text(f'from pathlib import Path\nDECODER_NODE=Path({str(dependency)!r})\nDECODER_SHA256={expected!r}\n')
    first = cache_gate.bun_decoder_identity(tmp_path)
    dependency.write_bytes(b'different-dependency-b')
    second = cache_gate.bun_decoder_identity(tmp_path)
    assert first['expected_sha256'] == second['expected_sha256']
    assert first['sha256'] != second['sha256']
    assert second['sha256'] != second['expected_sha256']
    dependency.unlink()
    assert cache_gate.bun_decoder_identity(tmp_path)['sha256'] is None


def test_same_code_rejects_missing_changed_or_removed_decoder():
    code = {'head':'a','dirty':'clean','cliproxy':'proxy','proxy_source_sha256':'source','bun_decoder':{'sha256':'a','expected_sha256':'a'}}
    report = {'code':copy.deepcopy(code),'proxy_source':'source'}
    assert cache_gate._same_code(report, code)[0]
    for value in [None, {'sha256':'b','expected_sha256':'a'}, {'sha256':None,'expected_sha256':'a'}]:
        changed = {**code,'bun_decoder':value}
        assert not cache_gate._same_code(report, changed)[0]
        assert not cache_gate._same_code({'code':changed,'proxy_source':'source'}, code)[0]
    mismatch = {**code,'bun_decoder':{'sha256':'b','expected_sha256':'a'}}
    assert not cache_gate._same_code({'code':mismatch,'proxy_source':'source'}, mismatch)[0]


def test_external_change_cannot_use_no_sensitive_change_escape(tmp_path, monkeypatch):
    target = {'head':'a','dirty':'clean','cliproxy':None,'bun_decoder':{'sha256':'new','expected_sha256':'new'}}
    monkeypatch.setattr(cache_gate,'code_identity',lambda *a,**k:target)
    monkeypatch.setattr(cache_gate,'read_running_record',lambda:{'head':'a','dirty':'clean','bun_decoder':{'sha256':'old','expected_sha256':'old'}})
    monkeypatch.setattr(cache_gate,'sensitive_files',lambda *a:set())
    monkeypatch.setattr(cache_gate,'changed_files',lambda *a:set())
    monkeypatch.setattr(cache_gate,'find_passing_report',lambda *a:(None,[]))
    result=cache_gate.check(tmp_path)
    assert not result.ok
    assert result.touched == ['External Bun decoder (actual binary or configured pin)']


def test_old_snapshot_without_decoder_key_still_verifies(tmp_path,monkeypatch):
    (tmp_path/'src/obs_agent').mkdir(parents=True)
    (tmp_path/'src/obs_agent/telegram_main.py').write_text('')
    code={'head':'a','dirty':'clean','cliproxy':None,'proxy_source_sha256':'s'}
    (tmp_path/'current.json').write_text(json.dumps({'repo':str(tmp_path),'code':code}))
    monkeypatch.setattr(cache_gate,'code_identity',lambda *a,**k:{**code,'bun_decoder':None})
    assert cache_launch._fallback(tmp_path)[0] is not None
    monkeypatch.setattr(cache_gate,'code_identity',lambda *a,**k:{**code,'bun_decoder':{'sha256':'a','expected_sha256':'a'}})
    assert cache_launch._fallback(tmp_path)[0] is None


def test_scripts_help_and_javascript_syntax():
    for name in ['baseline.py']:
        result=subprocess.run([sys.executable,str(Path(__file__).with_name(name)),'--help'],capture_output=True,text=True)
        assert result.returncode == 0 and '--run-id' in result.stdout
    for name in ['bun_transport_preload.cjs','bun_decoder_worker.cjs']:
        result=subprocess.run(['node','--check',str(ROOT/'src/obs_agent'/name)],capture_output=True,text=True)
        assert result.returncode == 0, result.stderr
