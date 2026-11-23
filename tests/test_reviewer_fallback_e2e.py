"""Reviewer failover through registered events and isolated persistent stores."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('HERMES_REVIEWER_E2E_CORE', Path.home() / '.hermes/hermes-agent'))


@pytest.mark.parametrize('scenario,plugin',
                         [(scenario, 'hindsight-reviewer') for scenario in ['fallback', 'healthy', 'exhausted', 'malformed',
                                                                            'malformed_primary', 'rejected', 'trust_denied', 'duplicates', 'noise']])
@pytest.mark.parametrize('surface', ['telegram', 'desktop'])
def test_reviewer_provider_failover_at_its_registered_event(plugin, scenario, surface, tmp_path, lifecycle='steady'):
    if not (CORE / 'agent/plugin_llm.py').is_file():
        pytest.skip('Requires an installed Hermes core; see README E2E setup')
    env = {name: os.environ[name] for name in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SYSTEMROOT') if name in os.environ}
    env.update(HERMES_REVIEWER_E2E_CORE=str(CORE), TMPDIR=str(tmp_path), PYTHONDONTWRITEBYTECODE='1', HERMES_REVIEWER_E2E_SURFACE=surface, HERMES_REVIEWER_E2E_LIFECYCLE=lifecycle)
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
    assert receipt['surface'] == surface
    assert receipt['lifecycle'] == lifecycle
    assert receipt['write_verified'] == (scenario in {'fallback', 'healthy', 'duplicates', 'noise'})
    assert receipt['notice_count'] == int(receipt['write_verified'])


@pytest.mark.parametrize('plugin', ['hindsight-reviewer'])
@pytest.mark.parametrize('lifecycle', ['rotated', 'overlap'])
def test_desktop_lifecycle_keeps_registered_review_and_native_notice(plugin, lifecycle, tmp_path):
    test_reviewer_provider_failover_at_its_registered_event(plugin, 'healthy', 'desktop', tmp_path, lifecycle)
