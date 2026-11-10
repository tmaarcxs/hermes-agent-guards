"""Reliability regression scenarios for the Hindsight reviewer over real registered hooks.

Every case runs the same isolated subprocess as the reviewer fallback harness: the real Hermes
plugin loader loads the candidate plugin, the registered ``post_llm_call`` event admits the real
reviewer thread, the model and Hindsight boundaries are controlled loopback servers, and the test
joins the worker thread under its budget. The oracle (record states, invalidation reasons, notice
text and the ``finished`` audit record) is derived from the scenario table before the loopback
model is consulted, and the runtime verifies the persisted states through readbacks.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('HERMES_REVIEWER_E2E_CORE', Path.home() / '.hermes/hermes-agent'))
RUNTIME = os.environ.get('HERMES_REVIEWER_E2E_PYTHON') or (
    sys.executable if sys.version_info >= (3, 10)
    else str(Path.home() / '.hermes/tools/python-3.14.9+20261021-darwin-arm64/bin/python3'))

CASES = [
    ('healthy', 'confirmed contradiction invalidates with the exact notice'),
    ('protected', 'protected memory proposed is never invalidated'),
    ('noise_fragment', 'fragment confirmed invalidated, near-similar fragment kept'),
    ('duplicates', 'cross-batch duplicate: first copy retained, last retired'),
    ('superseded', 'superseded needs the strictly newer target and retires the old'),
    ('corpus', 'deterministic corpus with all survived record kinds at once'),
    ('first_recall_404', 'a 404 on the first recalled id still processes its sibling'),
    ('observation_source_404', 'an observation whose source 404s is skipped, sibling processed'),
    ('prewrite_404', 'a prewrite 404 conflicts without aborting the sibling'),
    ('confirm_exhausted', 'first invalidation survives a later exhausted confirmation (partial)'),
    ('malformed_rows', 'duplicate and unknown proposal ids need not hurt their siblings'),
    ('duplicate_survivor_gone', 'a disappeared duplicate survivor retires nothing'),
    ('evidence_traps', 'assistant-only and memory-tool-only evidence retires nothing'),
    ('unbound_notice', 'a finished review without a bound sender suppresses the notice'),
    ('superseded_by_admission', 'a newer admitted message cancels unfinished writes on the same route'),
    ('superseded_before_registration', 'a message already admitted cancels an older review that has not registered yet'),
    ('other_thread_not_canceled', 'admission on a different chat/thread does not cancel this review'),
]


@pytest.mark.parametrize('scenario,label', CASES, ids=[case[0] for case in CASES])
def test_hindsight_reliability_through_registered_hook(scenario, label, tmp_path):
    if not (CORE / 'agent/plugin_llm.py').is_file():
        pytest.skip('Requires an installed Hermes core; see README E2E setup')
    env = dict(os.environ, HERMES_REVIEWER_E2E_CORE=str(CORE), TMPDIR=str(tmp_path),
               PYTHONDONTWRITEBYTECODE='1')
    root = Path(os.environ.get('HERMES_REVIEWER_E2E_ROOT', ROOT))
    result = subprocess.run([RUNTIME, '-I', str(ROOT / 'tests/hindsight_reliability_runtime.py'),
                             'hindsight-reviewer', scenario, str(tmp_path / 'home'), str(root)],
                            capture_output=True, text=True, env=env, timeout=300)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    prefix = 'REVIEWER_E2E_RESULT='
    receipts = [json.loads(line[len(prefix):]) for line in (result.stdout + '\n' + result.stderr).splitlines()
                if line.startswith(prefix)]
    assert len(receipts) == 1, result.stdout + '\n' + result.stderr
    receipt = receipts[0]
    assert receipt['scenario'] == scenario
    assert receipt['applied'] == receipt['expected_applied']
    assert receipt['status'] == receipt['expected_status']
    assert receipt['notice_state'] == receipt['expected_notice_state']
    if scenario != 'unbound_notice':
        assert receipt['notice'] == receipt['expected_notice']
