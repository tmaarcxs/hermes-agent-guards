"""Reviewer failover through registered events and isolated persistent stores."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('HERMES_REVIEWER_E2E_CORE', Path.home() / '.hermes/hermes-agent'))
JOT = Path(os.environ.get('HERMES_REVIEWER_E2E_JOT', Path.home() / '.hermes/plugins/jot'))


@pytest.mark.parametrize('scenario,plugin',
                         [(scenario, plugin) for scenario in ['fallback', 'healthy', 'exhausted', 'malformed',
                                                              'malformed_primary', 'rejected', 'trust_denied', 'duplicates']
                          for plugin in ['hindsight-reviewer', 'jot-reviewer']]
                         + [('noise', 'hindsight-reviewer')])
def test_reviewer_provider_failover_at_its_registered_event(plugin, scenario, tmp_path):
    if not (CORE / 'agent/plugin_llm.py').is_file() or not (JOT / 'runtime/worker.cjs').is_file():
        pytest.skip('Requires an installed Hermes core and Jot runtime; see README E2E setup')
    env = dict(os.environ, HERMES_REVIEWER_E2E_CORE=str(CORE), HERMES_REVIEWER_E2E_JOT=str(JOT),
               TMPDIR=str(tmp_path), PYTHONDONTWRITEBYTECODE='1')
    root = Path(os.environ.get('HERMES_REVIEWER_E2E_ROOT', ROOT))
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
    assert receipt['write_verified'] == (scenario in {'fallback', 'healthy', 'duplicates', 'noise'})
    assert receipt['notice_count'] == int(receipt['write_verified'])
