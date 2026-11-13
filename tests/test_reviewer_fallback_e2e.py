"""Reviewer failover through registered events and isolated persistent stores."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('HERMES_REVIEWER_E2E_CORE', Path.home() / '.hermes/hermes-agent'))
JOT = Path(os.environ.get('HERMES_REVIEWER_E2E_JOT', Path.home() / '.hermes/plugins/jot'))
NODE = shutil.which('node')


@pytest.mark.parametrize('scenario,plugin',
                         [(scenario, plugin) for scenario in ['fallback', 'healthy', 'exhausted', 'malformed',
                                                              'malformed_primary', 'rejected', 'trust_denied', 'duplicates']
                          for plugin in ['hindsight-reviewer', 'jot-reviewer']]
                         + [('noise', 'hindsight-reviewer'), ('runtime_missing', 'jot-reviewer'), ('collaboration_disabled', 'jot-reviewer')])
@pytest.mark.parametrize('surface', ['telegram', 'desktop'])
def test_reviewer_provider_failover_at_its_registered_event(plugin, scenario, surface, tmp_path):
    if not (CORE / 'agent/plugin_llm.py').is_file() or not (JOT / 'runtime/worker.cjs').is_file():
        pytest.skip('Requires an installed Hermes core and Jot runtime; see README E2E setup')
    env = {name: os.environ[name] for name in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SYSTEMROOT') if name in os.environ}
    env.update(HERMES_REVIEWER_E2E_CORE=str(CORE), HERMES_REVIEWER_E2E_JOT=str(JOT), TMPDIR=str(tmp_path), PYTHONDONTWRITEBYTECODE='1', HERMES_REVIEWER_E2E_SURFACE=surface)
    assert NODE, 'An installed Node runtime is required for Jot E2E'
    env['PATH'] = str(Path(NODE).parent) + os.pathsep + env.get('PATH', '')
    root = Path(os.environ.get('HERMES_REVIEWER_E2E_ROOT', ROOT))
    if scenario == 'runtime_missing':
        isolated = tmp_path / 'source'
        shutil.copytree(root / plugin, isolated / plugin, ignore=shutil.ignore_patterns('node_modules', '.venv', '__pycache__', 'tests'))
        root = isolated
        binary = tmp_path / 'bin'
        binary.mkdir()
        (binary / 'node').symlink_to(NODE)
        env['PATH'] = str(binary)
    result = subprocess.run([sys.executable, '-I', str(ROOT / 'tests/reviewer_fallback_runtime.py'),
                             plugin, scenario, str(tmp_path / 'home'), str(root)],
                            capture_output=True, text=True, env=env, timeout=100)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    prefix = 'REVIEWER_E2E_RESULT='
    receipts = [json.loads(line[len(prefix):]) for line in (result.stdout + '\n' + result.stderr).splitlines()
                if line.startswith(prefix)]
    assert len(receipts) == 1, result.stdout + '\n' + result.stderr
    receipt = receipts[0]
    assert receipt['plugin'] == plugin and receipt['scenario'] == scenario
    assert receipt['surface'] == surface
    assert receipt['write_verified'] == (scenario in {'fallback', 'healthy', 'duplicates', 'noise'})
    assert receipt['notice_count'] == int(receipt['write_verified'])
