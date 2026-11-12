"""Exercise the plugin through real Hermes discovery, tool dispatch and lifecycle consumers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = Path.home() / '.hermes' / 'hermes-agent'


@pytest.mark.parametrize('scenario', ['json', 'opaque', 'composition', 'observer', 'observer_mutation', 'observer_error', 'observer_off'])
def test_real_runtime_consumer(tmp_path, scenario):
    home = tmp_path / 'home'
    home.mkdir()
    target = home / 'plugins' / 'hermes-agent-guards' / 'token-efficiency'
    source = ROOT / 'token-efficiency'
    if source.exists():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns('node_modules', '__pycache__'))
    (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-agent-guards/token-efficiency]\n  entries:\n    hermes-agent-guards/token-efficiency:\n      enabled: true\n      settings:\n        observe_requests: true\n        request_budget: 2\n')
    if scenario == 'observer_off':
        (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-agent-guards/token-efficiency]\n')
    if scenario == 'composition':
        shutil.copytree(ROOT / 'workspace-guards', home / 'plugins/hermes-agent-guards/workspace-guards', ignore=shutil.ignore_patterns('tests', '__pycache__'))
        (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-agent-guards/token-efficiency, hermes-agent-guards/workspace-guards]\n')
    env = {name: os.environ[name] for name in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SYSTEMROOT') if name in os.environ}
    env.update(HERMES_HOME=str(home), NO_PROXY='*', PYTHONPATH=str(CORE), HERMES_NONINTERACTIVE='1')
    result = subprocess.run([sys.executable, str(ROOT / 'tests' / 'token_efficiency_consumer.py'), str(CORE), str(home), scenario], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads((home / 'consumer-result.json').read_text())
    assert data['scenario'] == scenario and data['passed'] is True
    if scenario == 'observer':
        resumed = subprocess.run([sys.executable, str(ROOT / 'tests' / 'token_efficiency_consumer.py'), str(CORE), str(home), 'observer_resume'], env=env, capture_output=True, text=True, timeout=60)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
