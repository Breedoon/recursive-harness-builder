"""Deterministic repro of the CLI's system-breakpoint TTL choice.
Runs the bundled CLI (2.1.59) in an isolated HOME with a FAKE (never-refreshed) OAuth credential,
against a local capture server, with ~/.claude.json either containing or lacking the cached
GrowthBook feature tengu_prompt_cache_1h_config. Prints the system cache_control seen per case."""
import json, os, shutil, subprocess, sys, tempfile, time, socket
CLI = '/workspace/obs/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude'
HERE = os.path.dirname(os.path.abspath(__file__))
real = json.load(open('/home/agent/.claude.json'))
def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p
def run_case(name, cfg_mutator):
    home = tempfile.mkdtemp(prefix=f'ttlrepro-{name}-')
    os.makedirs(os.path.join(home, '.claude'))
    cfg = json.loads(json.dumps(real))
    cfg_mutator(cfg)
    json.dump(cfg, open(os.path.join(home, '.claude.json'), 'w'))
    creds = {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-FAKE-repro-not-a-real-token',
             'refreshToken': 'sk-ant-ort01-FAKE', 'expiresAt': int(time.time()*1000) + 86400000,
             'scopes': ['user:inference', 'user:mcp_servers', 'user:profile', 'user:sessions:claude_code'],
             'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_20x'}}
    json.dump(creds, open(os.path.join(home, '.claude', '.credentials.json'), 'w'))
    port = free_port(); cap = os.path.join(home, 'captured.jsonl')
    srv = subprocess.Popen([sys.executable, os.path.join(HERE, 'capture_server.py'), str(port), cap])
    time.sleep(0.5)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC', 'CLAUDE'))}
    env.update(HOME=home, ANTHROPIC_BASE_URL=f'http://127.0.0.1:{port}',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_ENTRYPOINT='sdk-py')
    try:
        subprocess.run([CLI, '-p', 'say hi', '--model', 'claude-opus-4-6', '--max-turns', '1',
                        '--output-format', 'json'], env=env, cwd=home, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        pass
    srv.terminate()
    ttls = []
    if os.path.exists(cap):
        for line in open(cap, 'rb'):
            b = json.loads(line)
            if 'opus' not in b.get('model', ''):
                continue
            ttls.append([blk.get('cache_control') for blk in b.get('system', []) if blk.get('cache_control')])
    shutil.rmtree(home, ignore_errors=True)
    return ttls[:1]
def keep(cfg): pass
def drop_feature(cfg):
    cfg.get('cachedGrowthBookFeatures', {}).pop('tengu_prompt_cache_1h_config', None)
def drop_all(cfg):
    cfg.pop('cachedGrowthBookFeatures', None)
for name, m in [('with-cached-allowlist', keep), ('feature-missing', drop_feature), ('no-growthbook-cache', drop_all)]:
    print(name, '->', run_case(name, m))
