"""Hindsight reviewer reliability scenarios over the real registered post-turn hook.

One scenario per subprocess, mirroring the existing reviewer fallback harness: the plugin is
loaded by the real Hermes plugin loader, the registered ``post_llm_call`` event starts the real
reviewer thread, and both external boundaries are controlled loopback servers (an
OpenAI-compatible model backend and the Hindsight REST service). Recall metadata is seeded the
same way the existing harness does (the reviewer's shared tool-recall registry), and the test
waits on the bounded join of the real worker thread.

The oracle is declared from the scenario table before any model output is consumed: expected
final states, invalidation reasons, notice text and audit status are derived from the record
kinds and fault rules alone. The loopback model answers from the same declared kinds and may
propose malicious candidates (protected memories, assistant- or memory-tool-only evidence,
duplicated/unknown ids), which the reviewer must reject through its deterministic gates.
"""

import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

plugin, scenario_id, home_arg, root_arg = sys.argv[1:5]
home, root = Path(home_arg), Path(root_arg)
core = Path(os.environ['HERMES_REVIEWER_E2E_CORE'])
sys.path.insert(0, str(core))
import hermes_bootstrap
import hermes_yaml

os.environ.update(HERMES_HOME=str(home), HERMES_DATA_DIR=str(home), HOME=str(home),
                  HERMES_DISABLE_LAZY_INSTALLS='1', NO_PROXY='127.0.0.1,localhost')
for key in list(os.environ):
    if any(word in key for word in ('TOKEN', 'API_KEY', 'SECRET', 'PROXY')) and key != 'NO_PROXY':
        os.environ.pop(key, None)

original_connect = socket.socket.connect


def local_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in {'127.0.0.1', '::1'}:
        raise AssertionError('E2E attempted an external connection')
    return original_connect(sock, address)


socket.socket.connect = local_connect

USER = ('Quick update: the staging database is now serving on port 6543, and I am authorizing '
        'Bruno to run the next deploy. Please log the release.')
ASSISTANT = 'Release logged. For diagnostics, the Nebula staging runbook is stored in Vault path shared/nebula.'
TOOL_TEXT = 'Recalled: the Nebula deployment fleet is pinned to channel beta-9 today.'
QUOTE_BY_KIND = {'contradicted': 'now serving on port 6543',
                 'protected': 'authorizing Bruno to run the next deploy',
                 'assistant_trap': 'stored in Vault path shared/nebula',
                 'memory_trap': 'pinned to channel beta-9'}
T = {'contradicted': 'Nebula staging database listens on port 5432.',
     'protected': 'Deployment authorization for Nebula staging was granted by the user.',
     'assistant_trap': 'The Nebula staging runbook is stored in Vault path shared/nebula.',
     'memory_trap': 'Nebula deployment fleet is pinned to channel beta-9.',
     'fragment': 'The task is review-only.',
     'fragment_bad': 'The review found it worse.',
     'dup_first': 'The Nebula staging database runs on port 5432.',
     'dup_later': 'Nebula staging DB uses port 5432.',
     'superseded_old': 'Nebula on-call rota is Ana.',
     'superseded_new': 'Nebula on-call rota is Bruno.',
     'observation': 'Nebula staging observations were compiled.',
     'source': 'Nebula backup job runs hourly.'}
NOISE_KEEP = {T['fragment_bad']}
DUP_PARTNER = {'dup_later': 'dup_first', 'superseded_old': 'superseded_new'}
NOISE_REASON = {'dup_later': 'duplicate', 'superseded_old': 'superseded'}
INVALID_REASON = {'contradicted': 'hindsight-review:', 'fragment': 'hindsight-noise:fragment:',
                  'dup_later': 'hindsight-noise:duplicate:', 'superseded_old': 'hindsight-noise:superseded:'}
APPLIED_LABEL = {'contradicted': 'outdated', 'fragment': 'no context', 'dup_later': 'duplicate',
                 'superseded_old': 'superseded'}


def plain(number):
    return {'kind': 'plain', 'text': 'Nebula release checklist item %d was archived.' % number,
            'date': '2026-06-01', 'type': 'world', 'sources': None}


def entry(kind, date='2026-06-01', kind_type='world', sources=None, text=''):
    return {'kind': kind, 'text': text or T[kind], 'date': date, 'type': kind_type, 'sources': sources}


def corpus():
    records = [entry('contradicted'), entry('protected'), entry('assistant_trap'), entry('memory_trap'),
               entry('fragment'), entry('fragment_bad')]
    records += [plain(number) for number in range(1, 25)]
    records.append(entry('dup_first'))
    records += [plain(number) for number in range(25, 31)]
    records.append(entry('superseded_old', date='2026-01-05'))
    records += [plain(number) for number in range(31, 101)]
    records.append(entry('superseded_new', date='2026-11-01'))
    records += [plain(number) for number in range(101, 105)]
    records.append(entry('dup_later'))
    return records


OLD_CONTRA = 'Old copy: Nebula staging database listens on port 5432.'
WARM_CONTRA = 'Warm copy: Nebula staging database listens on port 5432.'
SCENARIOS = {
    'healthy': {'recall': [entry('contradicted'), plain(1)], 'bound': True, 'expected_status': 'complete'},
    'noise_fragment': {'recall': [entry('fragment'), entry('fragment_bad'), plain(2)], 'bound': True,
                       'expected_status': 'complete'},
    'duplicates': {'recall': [entry('dup_first')] + [plain(number) for number in range(1, 24)]
                   + [entry('dup_later')], 'bound': True, 'expected_status': 'complete'},
    'superseded': {'recall': [entry('superseded_old', date='2026-01-05'),
                              entry('superseded_new', date='2026-11-01'), plain(3), plain(4), plain(5)],
                   'bound': True, 'expected_status': 'complete'},
    'first_recall_404': {'recall': [entry('contradicted', text=OLD_CONTRA), plain(6), entry('contradicted')],
                         'get404': [OLD_CONTRA], 'expected_valid': [OLD_CONTRA],
                         'expect_counts': {'stale_records': 1}, 'bound': True, 'expected_status': 'partial'},
    'observation_source_404': {'recall': [entry('observation', kind_type='observation',
                                                    sources=[T['source']]), entry('contradicted'), plain(7)],
                               'store': [entry('source')], 'get404': [T['source']],
                               'expect_counts': {'stale_records': 1}, 'bound': True,
                               'expected_status': 'partial'},
    'prewrite_404': {'recall': [entry('contradicted', text=WARM_CONTRA), plain(8), entry('contradicted')],
                     'get404_after': {WARM_CONTRA: 1}, 'expected_valid': [WARM_CONTRA],
                     'expect_counts': {'conflict': 1}, 'bound': True, 'expected_status': 'partial'},
    'confirm_exhausted': {'recall': [entry('contradicted'), entry('contradicted', text=WARM_CONTRA), plain(9)],
                          'fail_confirm_from': 1, 'expected_valid': [WARM_CONTRA],
                          'expect_counts': {'confirmation_errors': 1}, 'bound': True,
                          'expected_status': 'partial'},
    'malformed_rows': {'recall': [plain(10), plain(11), plain(12), entry('contradicted')],
                       'evil_propose': True, 'evil_noise': True,
                       'expect_counts': {'review_rejected_rows': 3, 'review_missing_rows': 1,
                                         'noise_rejected_rows': 1},
                       'bound': True, 'expected_status': 'partial'},
    'protected': {'recall': [entry('protected'), plain(13)], 'bound': True, 'expected_status': 'complete'},
    'duplicate_survivor_gone': {'recall': [entry('dup_first'), entry('dup_later'), plain(14)],
                                'get404_after': {T['dup_first']: 1},
                                'expected_valid': [T['dup_first'], T['dup_later']],
                                'expect_counts': {'conflict': 1}, 'bound': True,
                                'expected_status': 'partial'},
    'evidence_traps': {'recall': [entry('assistant_trap'), entry('memory_trap'), plain(15)],
                       'expect_counts': {'unquotable': 2}, 'bound': True, 'expected_status': 'partial'},
    'unbound_notice': {'recall': [entry('contradicted'), plain(16)], 'bound': False,
                       'expected_status': 'complete'},
    'superseded_by_admission': {'recall': [entry('contradicted'), entry('contradicted', text=WARM_CONTRA), plain(17)],
                                'cancel_after': 1, 'expected_valid': [WARM_CONTRA],
                                'expect_counts': {'superseded_cases': 1}, 'bound': True,
                                'expected_status': 'partial',
                                'expected_notice_state': 'suppressed_superseded'},
    'superseded_before_registration': {'recall': [entry('contradicted'), plain(20)],
                                       'expected_valid': [T['contradicted'], plain(20)['text']],
                                       'expect_counts': {'superseded_cases': 2}, 'bound': True,
                                       'superseded_before_admission': True, 'expected_status': 'partial'},
    'other_thread_not_canceled': {'recall': [entry('contradicted'), plain(18), plain(19)],
                                  'cancel_other': {'chat_id': '8888', 'thread_id': 't-9'},
                                  'bound': True, 'expected_status': 'complete'},
    'corpus': {'recall': corpus(), 'bound': True, 'expected_status': 'partial'},
}
scenario = SCENARIOS[scenario_id]
all_records = scenario['recall'] + scenario.get('store', [])
KINDS = {record['text']: record['kind'] for record in all_records}
IDS = {record['text']: str(uuid.uuid5(uuid.NAMESPACE_URL, 'hindsight-reliability-e2e:' + record['text']))
       for record in all_records}
STORE = {IDS[record['text']]: {'text': record['text'], 'type': record['type'], 'state': 'valid',
                               'date': record['date'],
                               'sources': [IDS[text] for text in record['sources'] or []]}
         for record in all_records}
GET404 = {IDS[text] for text in scenario.get('get404', [])}
GET404_AFTER = {IDS[text]: limit for text, limit in scenario.get('get404_after', {}).items()}
FAIL_CONFIRM_FROM = scenario.get('fail_confirm_from')
CANCEL_AFTER = scenario.get('cancel_after')
CANCEL_OTHER = scenario.get('cancel_other')
EVIL_PROPOSE = scenario.get('evil_propose', False)
EVIL_NOISE = scenario.get('evil_noise', False)
recall_records = [{'id': IDS[record['text']], 'text': record['text']} for record in scenario['recall']]
expected_state = {record['text']: 'valid' if record['text'] in scenario.get('expected_valid', [])
                  or record['kind'] not in INVALID_REASON else 'invalidated' for record in all_records}
applied_labels = [APPLIED_LABEL[record['kind']] for record in all_records
                  if expected_state[record['text']] == 'invalidated']
expected_applied = len(applied_labels)
invalidated_ids = {IDS[text] for text, state in expected_state.items() if state == 'invalidated'}
expected_notice_state = scenario.get('expected_notice_state') or (
    'suppressed_unbound_or_group' if applied_labels and not scenario['bound']
    else 'handed_off' if applied_labels else 'not_needed')


def expected_notice_text():
    if scenario.get('cancel_after') is not None:
        return ''
    counts = Counter(applied_labels)
    breakdown = ' · '.join('%d %s' % (count, label) for label, count
                           in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))
    text = '🧠 Memory review: %d invalidated (%s)' % (len(applied_labels), breakdown) if applied_labels else ''
    if text and scenario['expected_status'] != 'complete':
        text += ' · review incomplete'
    return text


audit_lines, outcome_lines, notices, patches = [], [], [], []
calls, reads, model_calls, confirm_calls = Counter(), Counter(), 0, 0
pending = []
cancel_release = threading.Event()
lock = threading.Lock()


class Capture(logging.Handler):
    def emit(self, record):
        line = self.format(record)
        if 'Hindsight review audit:' in line:
            audit_lines.append(line.split('Hindsight review audit:', 1)[1].strip())
        elif 'Hindsight review outcome:' in line:
            outcome_lines.append(line.split('Hindsight review outcome:', 1)[1].strip())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        memory_id = self.path.rsplit('/', 1)[-1]
        record = STORE.get(memory_id)
        if record is None:
            self.reply(404, {'error': {'message': 'not found', 'code': 'not_found'}})
            return
        with lock:
            allowed = GET404_AFTER.get(memory_id)
            denied = memory_id in GET404 or (allowed is not None and reads[memory_id] >= allowed)
            if not denied:
                reads[memory_id] += 1
        if denied:
            self.reply(404, {'error': {'message': 'not found', 'code': 'not_found'}})
            return
        self.reply(200, {'id': memory_id, 'text': record['text'], 'type': record['type'],
                         'state': record['state'], 'mentioned_at': record.get('date', ''),
                         'source_memory_ids': record['sources'],
                         'invalidation_reason': record.get('invalidation_reason', '')})

    def do_PATCH(self):
        memory_id = self.path.rsplit('/', 1)[-1]
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with lock:
            record = STORE[memory_id]
            record.update(state=body['state'], invalidation_reason=body['reason'])
            patches.append({'id': memory_id, 'text': record['text'], 'reason': body['reason']})
        self.reply(200, {})

    def do_POST(self):
        global model_calls, confirm_calls
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        route = 'primary' if self.path.startswith('/primary/') else 'backup'
        messages = body['messages']
        content = messages[-1]['content']
        assert isinstance(content, list) and len(content) >= 2, content
        payload = json.loads(content[-1]['text'])
        instruction = content[0]['text']
        schema = body['response_format']['json_schema']['schema']
        with lock:
            model_calls += 1
            calls[route] += 1
            call_index, is_confirm = confirm_calls, 'verdict' in schema['properties']
            if is_confirm:
                confirm_calls += 1
        if is_confirm and FAIL_CONFIRM_FROM is not None and call_index >= FAIL_CONFIRM_FROM:
            self.reply(424, {'error': {'message': 'Synthetic upstream failure',
                                       'code': 'byok_upstream_provider_failed'}})
            return
        if is_confirm and CANCEL_OTHER and call_index == 0:
            source = {'platform': 'telegram', 'chat_id': CANCEL_OTHER['chat_id'],
                      'thread_id': CANCEL_OTHER['thread_id'], 'profile': ''}
            with lock:
                plugins.invoke_hook('post_gateway_admission', session_key='telegram:dm:8888',
                                    source=source)
        if is_confirm and CANCEL_AFTER is not None and call_index == CANCEL_AFTER:
            with lock:
                pending.append({'platform': 'telegram', 'chat_id': '424242',
                                'thread_id': '', 'profile': ''})
            if not cancel_release.wait(timeout=10):
                raise AssertionError('Cancellation hook did not release the confirmation')
        if is_confirm:
            if 'REASON' in payload:
                verdict = 'keep' if payload.get('MEMORY') in NOISE_KEEP else 'noise'
            else:
                verdict = 'refutes'
            answer = {'verdict': verdict}
        elif 'NOISE' in instruction:
            memories, others = payload['MEMORIES'], payload.get('OTHER_MEMORIES', [])
            pool = {case['memory']: case['id'] for case in [*memories, *others]}
            rows = []
            for case in memories:
                kind = KINDS[case['memory']]
                row = {'id': case['id'], 'decision': 'keep', 'reason': 'none', 'duplicate_of': ''}
                if kind in {'fragment', 'fragment_bad'}:
                    row = {'id': case['id'], 'decision': 'noise', 'reason': 'fragment', 'duplicate_of': ''}
                elif kind in DUP_PARTNER and T[DUP_PARTNER[kind]] in pool:
                    row = {'id': case['id'], 'decision': 'noise', 'reason': NOISE_REASON[kind],
                           'duplicate_of': pool[T[DUP_PARTNER[kind]]]}
                rows.append(row)
            with lock:
                evil = EVIL_NOISE
            if evil:
                target = next((case['id'] for case in memories if KINDS[case['memory']] == 'plain'), None)
                if target is not None:
                    rows.append({'id': target, 'decision': 'noise', 'reason': 'none', 'duplicate_of': ''})
            answer = {'items': rows}
        else:
            rows = []
            for case in payload['MEMORIES']:
                kind = KINDS[case['memory']]
                if kind in QUOTE_BY_KIND:
                    rows.append({'id': case['id'], 'decision': 'candidate', 'category': 'other',
                                 'evidence_quote': QUOTE_BY_KIND[kind]})
                else:
                    rows.append({'id': case['id'], 'decision': 'keep', 'category': 'other',
                                 'evidence_quote': ''})
            with lock:
                evil = EVIL_PROPOSE
            if evil:
                ids = [case['id'] for case in payload['MEMORIES']]
                if ids:
                    rows.append({**rows[0], 'id': ids[0]})
                    rows.append({'id': 'm9999', 'decision': 'candidate', 'category': 'other',
                                 'evidence_quote': QUOTE_BY_KIND['contradicted']})
            answer = {'items': rows}
        self.reply(200, {'id': 'synthetic', 'object': 'chat.completion', 'created': 0,
                         'model': body['model'],
                         'choices': [{'index': 0, 'finish_reason': 'stop',
                                      'message': {'role': 'assistant', 'content': json.dumps(answer)}}],
                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = 'http://127.0.0.1:' + str(server.server_port)
key = 'hermes-agent-guards/' + plugin
category = home / 'plugins/hermes-agent-guards'
category.mkdir(parents=True, exist_ok=True)
(category / plugin).symlink_to((root / plugin).resolve(), target_is_directory=True)
providers = {name + '-fixture': {'base_url': base + '/' + name + '/v1', 'api_key': 'synthetic',
                                 'transport': 'chat_completions', 'default_model': name + '-fixture'}
             for name in ('primary', 'backup')}
primary = {'provider': 'custom:primary-fixture', 'model': 'primary-fixture'}
backup = {'provider': 'custom:backup-fixture', 'model': 'backup-fixture'}
config = {'model': {'default': 'primary-fixture', 'provider': primary['provider']},
          'cron': {'model': primary['model'], 'model_provider': primary['provider']},
          'providers': providers, 'fallback_providers': [backup],
          'plugins': {'enabled': [key],
                      'entries': {key: {'llm': {'allow_provider_override': True,
                                                'allow_model_override': True, 'allowed_models': '*'}}}}}
(home / 'config.yaml').write_text(hermes_yaml.safe_dump(config), encoding='utf-8')
(home / 'hindsight').mkdir(exist_ok=True)
(home / 'hindsight/config.json').write_text(json.dumps({'api_url': base}), encoding='utf-8')

logging.basicConfig(level=logging.INFO)
capture = Capture()
capture.setFormatter(logging.Formatter('%(message)s'))
if not any(isinstance(handler, Capture) for handler in logging.getLogger().handlers):
    logging.getLogger().addHandler(capture)

from hermes_cli import plugins
from gateway.session_context import set_session_vars

plugins.discover_plugins(force=True)
manager = plugins.get_plugin_manager()
loaded = manager._plugins.get(key) or manager._plugins.get(plugin)
assert loaded is not None, sorted(manager._plugins)
assert loaded.module is not None
reviewer_name = loaded.module.__name__ + '.reviewer'
reviewer = sys.modules[reviewer_name]
assert reviewer.CTX.plugin_id == key
assert type(reviewer.CTX.llm).__name__ == 'PluginLlm'
logging.getLogger(reviewer_name).setLevel(logging.INFO)
session_key = 'telegram:dm:424242'
agents = {session_key: SimpleNamespace(background_review_callback=notices.append)} if scenario['bound'] else {}
runner = SimpleNamespace(_running_agents=agents)


async def bind():
    await plugins.ainvoke_hook('pre_gateway_dispatch', gateway=runner)


asyncio.run(bind())
set_session_vars(platform='telegram', source='telegram', chat_id='424242', chat_type='dm',
                 user_id='424242', message_id='original-message', session_key=session_key,
                 session_id='fixture-session', cron_session=False)
from agent import auxiliary_client

reviewer._tool_recalls['fixture-session'] = {'bank': 'synthetic', 'records': recall_records}
history = [{'role': 'user', 'content': USER},
           {'role': 'assistant', 'content': 'Checking the fleet channel.',
            'tool_calls': [{'id': 'call1', 'type': 'function',
                            'function': {'name': 'hindsight_recall', 'arguments': '{}'}}]},
           {'role': 'tool', 'tool_call_id': 'call1', 'name': 'hindsight_recall', 'content': TOOL_TEXT},
           {'role': 'assistant', 'content': ASSISTANT}]
try:
    if scenario.get('superseded_before_admission'):
        plugins.invoke_hook('post_gateway_admission', session_key=session_key,
                            source={'platform': 'telegram', 'chat_id': '424242', 'thread_id': '', 'profile': ''},
                            message_id='newer-message')
    plugins.invoke_hook('post_llm_call', session_id='fixture-session', turn_id='fixture-turn',
                        user_message=USER, assistant_response=ASSISTANT, conversation_history=history)
    workers = [t for t in threading.enumerate() if t.name == plugin]
    assert workers, 'Real post-turn hook did not admit its reviewer'
    if CANCEL_AFTER is not None:
        deadline = 120
        while not pending and deadline > 0:
            time.sleep(0.02)
            deadline -= 1
        assert pending or not workers, 'Cancel trigger never reached the model boundary'
        if pending:
            plugins.invoke_hook('post_gateway_admission', session_key='telegram:dm:424242',
                                source=pending.pop(0), message_id='newer-message')
            cancel_release.set()
    for worker in workers:
        worker.join(timeout=240 if scenario_id == 'corpus' else 90)
        assert not worker.is_alive(), 'Reviewer did not finish in its budget'
    for text, state in expected_state.items():
        assert STORE[IDS[text]]['state'] == state, (scenario_id, text, STORE[IDS[text]]['state'], state)
    assert {patch['id'] for patch in patches} == invalidated_ids, (scenario_id, patches)
    for patch in patches:
        assert patch['reason'].startswith(INVALID_REASON[KINDS[patch['text']]]), (scenario_id, patch)
    bound_notices = [expected_notice_text()] if expected_notice_text() and scenario['bound'] else []
    assert notices == bound_notices, (scenario_id, notices, expected_notice_text())
    finished = [json.loads(line) for line in audit_lines if '"event": "finished"' in line]
    assert len(finished) == 1, (scenario_id, audit_lines)
    finished = finished[0]
    for field in ('session_id', 'turn_id', 'status', 'counts', 'notice_state'):
        assert field in finished, finished
    assert finished['session_id'] == 'fixture-session' and finished['turn_id'] == 'fixture-turn', finished
    assert finished['status'] == scenario['expected_status'], finished
    assert finished['notice_state'] == expected_notice_state, finished
    counts = finished['counts']
    assert counts.get('applied', 0) == expected_applied, finished
    for stat, minimum in scenario.get('expect_counts', {}).items():
        assert counts.get(stat, 0) >= minimum, (scenario_id, stat, counts)
    assert outcome_lines and all('reviewed=' in line and 'applied=' in line and 'status=' in line
                                 for line in outcome_lines), outcome_lines
    banned = [record['text'] for record in all_records] + [USER, ASSISTANT, TOOL_TEXT] + list(QUOTE_BY_KIND.values())
    for line in audit_lines:
        for needle in banned:
            assert needle not in line, line[:200]
    print('REVIEWER_E2E_RESULT=' + json.dumps({
        'plugin': plugin, 'scenario': scenario_id, 'records': len(recall_records),
        'expected_applied': expected_applied, 'applied': len(patches),
        'write_verified': bool(expected_applied), 'status': finished['status'],
        'expected_status': scenario['expected_status'], 'notice': notices[0] if notices else '',
        'expected_notice': expected_notice_text(), 'notice_state': finished['notice_state'],
        'expected_notice_state': expected_notice_state, 'audit_counts': counts,
        'boundary': 'registered real post_llm_call -> real reviewer thread -> loopback PluginLlm model '
                    '+ loopback Hindsight REST -> patched states verified against the declared oracle'}))
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()
