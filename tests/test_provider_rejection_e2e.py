"""Copilot rejection recovery through the host's real title-generation workflow."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('HERMES_PROVIDER_E2E_CORE', Path.home() / '.hermes/hermes-agent'))
COPILOT = Path(os.environ.get('HERMES_PROVIDER_E2E_COPILOT', Path.home() / '.hermes/plugins/github-copilot'))


@pytest.mark.parametrize('scenario', ['recovery', 'healthy', 'exhausted', 'unrelated',
                                     'policy', 'different_message', 'other_provider'])
def test_title_generation_rejection_recovery(scenario, tmp_path):
    if not (CORE / 'agent/auxiliary_error_policy.py').is_file() or not (COPILOT / '__init__.py').is_file():
        pytest.skip('Requires the installed Hermes title-generation runtime and Copilot provider plugin')
    env = dict(os.environ, HERMES_PROVIDER_E2E_CORE=str(CORE), HERMES_PROVIDER_E2E_COPILOT=str(COPILOT),
               TMPDIR=str(tmp_path), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-I', str(ROOT / 'tests/provider_rejection_runtime.py'),
                             scenario, str(tmp_path / 'home'), str(ROOT)],
                            capture_output=True, text=True, env=env, timeout=40)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    prefix = 'PROVIDER_E2E_RESULT='
    receipts = [json.loads(line[len(prefix):]) for line in result.stdout.splitlines() if line.startswith(prefix)]
    assert len(receipts) == 1, result.stdout + '\n' + result.stderr
    assert receipts[0]['scenario'] == scenario
    assert receipts[0]['title_source'] == ('llm' if scenario in {'recovery', 'healthy'} else 'derived')
