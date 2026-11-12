"""Real reviewer hook, host LLM/HTTP routing, and isolated backend persistence."""

import asyncio
import datetime
import json
import logging
import os
import shutil
import socket
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

plugin, mode, home_arg, root_arg = sys.argv[1:]
home, root = Path(home_arg), Path(root_arg)
core = Path(os.environ['HERMES_REVIEWER_E2E_CORE'])
jot = Path(os.environ['HERMES_REVIEWER_E2E_JOT'])
sys.path.insert(0, str(core))

os.environ.update(HERMES_HOME=str(home), HERMES_DATA_DIR=str(home), HOME=str(home),
                  HERMES_DISABLE_LAZY_INSTALLS='1', NO_PROXY='127.0.0.1,localhost')
for key in list(os.environ):
    if any(word in key for word in ('TOKEN', 'API_KEY', 'SECRET', 'PROXY')) and key != 'NO_PROXY':
        os.environ.pop(key, None)

logging.basicConfig(level=logging.WARNING)
original_connect = socket.socket.connect

def local_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in {'127.0.0.1', '::1'}:
        raise AssertionError('E2E attempted an external connection')
    return original_connect(sock, address)

socket.socket.connect = local_connect
import hermes_bootstrap
import hermes_yaml
calls, reads, patches = Counter(), [], []
fact_id = '00000000-0000-4000-8000-000000000001'
fact_text = 'The other fix is ready.' if mode == 'noise' else 'Nebula staging database is on port 5432.'
user = 'The Nebula staging database is now on port 6543.'
fact = {'id': fact_id, 'text': fact_text, 'type': 'world', 'state': 'valid'}
notices = []

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
        assert self.path.endswith('/memories/' + fact_id), self.path
        reads.append(fact['state'])
        self.reply(200, fact)

    def do_PATCH(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path.endswith('/memories/' + fact_id), self.path
        fact.update(state=body['state'], invalidation_reason=body['reason'])
        patches.append(body)
        self.reply(200, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        route = 'primary' if self.path.startswith('/primary/') else 'backup'
        assert self.path == '/' + route + '/v1/chat/completions', self.path
        calls[route] += 1
        if (route == 'primary' and mode not in {'healthy', 'malformed_primary'}) or mode == 'exhausted':
            self.reply(424, {'error': {'message': 'Synthetic upstream failure', 'code': 'byok_upstream_provider_failed'}})
            return
        messages = body['messages']
        content = messages[-1]['content']
        assert isinstance(content, list) and len(content) >= 2, content
        payload = json.loads(content[-1]['text'])
        instruction = content[0]['text']
        schema = body['response_format']['json_schema']['schema']
        if mode in {'malformed', 'malformed_primary'}:
            answer = {'wrong': True}
        elif plugin == 'jot-reviewer':
            if 'verdict' in schema['properties']:
                answer = {'verdict': 'skip' if mode == 'rejected' else 'apply'}
            else:
                answer = {'changes': [{'op': 'set', 'day': today, 'task': 'Nebula: deploy PR #4242',
                                       'state': '✅', 'next': 'Nada', 'evidence_quote': user}]}
        elif 'verdict' in schema['properties']:
            answer = {'verdict': 'noise' if mode == 'noise' else 'not_refuted' if mode == 'rejected' else 'refutes'}
        elif 'NOISE' in instruction:
            cases = payload['MEMORIES']
            answer = {'items': [{'id': c['id'], 'decision': 'noise' if mode == 'noise' else 'keep',
                                 'reason': 'fragment' if mode == 'noise' else 'none', 'duplicate_of': ''} for c in cases]}
        else:
            answer = {'items': [{'id': c['id'], 'decision': 'keep' if mode == 'noise' else 'candidate',
                                 'category': 'other', 'evidence_quote': '' if mode == 'noise' else user}
                                for c in payload['MEMORIES']]}
        self.reply(200, {'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': body['model'],
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
(category / plugin).symlink_to(root / plugin, target_is_directory=True)
if plugin == 'jot-reviewer':
    (home / 'plugins/jot').symlink_to(jot, target_is_directory=True)
providers = {name + '-fixture': {'base_url': base + '/' + name + '/v1', 'api_key': 'synthetic',
                                'transport': 'chat_completions', 'default_model': name + '-fixture'}
             for name in ('primary', 'backup')}
primary = {'provider': 'custom:primary-fixture', 'model': 'primary-fixture'}
backup = {'provider': 'custom:backup-fixture', 'model': 'backup-fixture'}
chain = [backup] if mode != 'duplicates' else [primary, backup, backup]
config = {'model': {'default': 'primary-fixture', 'provider': primary['provider']},
          'cron': {'model': primary['model'], 'model_provider': primary['provider']},
          'providers': providers, 'fallback_providers': chain,
          'plugins': {'enabled': [key] + (['jot'] if plugin == 'jot-reviewer' else []),
                      'entries': {key: {'llm': {'allow_provider_override': True, 'allow_model_override': True,
                                                'allowed_models': ['primary-fixture'] if mode == 'trust_denied' else '*'}}}}}
(home / 'config.yaml').write_text(hermes_yaml.safe_dump(config), encoding='utf-8')
(home / 'hindsight').mkdir(exist_ok=True)
(home / 'hindsight/config.json').write_text(json.dumps({'api_url': base}), encoding='utf-8')

from hermes_cli import plugins
from gateway.session_context import set_session_vars
from tools.registry import registry

def tool(name, args):
    raw = registry.dispatch(name, args)
    value = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(value, dict) and 'error' not in value, value
    return value.get('data', value)

plugins.discover_plugins(force=True)
manager = plugins.get_plugin_manager()
loaded = manager._plugins[key]
assert loaded.module is not None
reviewer = sys.modules[loaded.module.__name__ + '.reviewer']
assert reviewer.CTX.plugin_id == key
assert type(reviewer.CTX.llm).__name__ == 'PluginLlm'
session_key = 'telegram:dm:424242'
runner = SimpleNamespace(_running_agents={session_key: SimpleNamespace(background_review_callback=notices.append)})

async def bind():
    await plugins.ainvoke_hook('pre_gateway_dispatch', gateway=runner)

asyncio.run(bind())
set_session_vars(platform='telegram', source='telegram', chat_id='424242', chat_type='dm',
                 user_id='424242', session_key=session_key, session_id='fixture-session', cron_session=False)
from agent import auxiliary_client

class FixtureDate(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 10)

clock = patch('datetime.date', FixtureDate)
clock.start()
note_id = None
backend = None
if plugin == 'hindsight-reviewer':
    reviewer._tool_recalls['fixture-session'] = {'bank': 'synthetic', 'records': [{'id': fact_id, 'text': fact_text}]}
    today = ''
else:
    jot_module = manager._plugins['jot'].module
    assert jot_module is not None
    backend = sys.modules[jot_module.__name__ + '.backend']
    switched = backend.handle_command('ai on')
    assert 'on' in str(switched).lower(), switched
    date = datetime.date.today()
    assert date.weekday() < 5, 'Jot scenario requires a weekday'
    today = reviewer.today_heading(date)
    monday = date - datetime.timedelta(days=date.weekday())
    note = '\n\n'.join('## ' + reviewer.DAYS[i] + ' ' + str((monday + datetime.timedelta(days=i)).day)
                       + '\n\n| Estado | Tarea | Siguiente paso |\n| --- | --- | --- |\n'
                       + ('| 🔄 | Nebula: deploy PR #4242 | Deploy |' if i == date.weekday() else '| ⚪ |  |  |')
                       for i in range(5))
    created = tool('jot_create', {'title': 'Semana synthetic', 'text': note})
    note_id = created['id']
    user = 'Nebula: deploy PR #4242 is complete and verified.'
    if mode == 'collaboration_disabled':
        backend.handle_command('ai off')

try:
    if plugin == 'jot-reviewer':
        print('REVIEWER_RUNTIME=' + json.dumps({'node': shutil.which('node'), 'source': str(root / plugin), 'probe_present': (root / plugin / 'scripts/probe-runtime.mts').is_file()}))
    plugins.invoke_hook('post_llm_call', session_id='fixture-session', turn_id='fixture-turn',
                        user_message=user, assistant_response='Synthetic acknowledgement.',
                        conversation_history=[{'role': 'user', 'content': user},
                                              {'role': 'assistant', 'content': 'Synthetic acknowledgement.'}])
    workers = [t for t in threading.enumerate() if t.name == plugin]
    assert workers or mode == 'collaboration_disabled', 'Real post-turn hook did not admit its reviewer'
    for worker in workers:
        worker.join(timeout=45)
        assert not worker.is_alive(), 'Reviewer did not finish in its budget'
    expected_write = mode in {'fallback', 'healthy', 'duplicates', 'noise'}
    if plugin == 'hindsight-reviewer':
        assert fact['state'] == ('invalidated' if expected_write else 'valid'), (mode, fact, calls)
        assert len(patches) == int(expected_write), patches
        assert (reads[-1] == 'invalidated') == expected_write, reads
    else:
        assert isinstance(note_id, str)
        if mode == 'collaboration_disabled':
            assert backend is not None
            backend.handle_command('ai on')
        after = tool('jot_read', {'id': note_id})
        markdown = after['markdown']
        assert ('✅' in markdown) == expected_write, (mode, markdown, calls)
        assert after['revision'] == (2 if expected_write else 1), after
    assert len(notices) == int(expected_write), notices
    stages = 3 if plugin == 'hindsight-reviewer' else 2
    if mode in {'runtime_missing', 'collaboration_disabled'}:
        assert not calls, calls
    elif mode == 'healthy':
        assert calls == {'primary': stages}, calls
    elif mode == 'malformed_primary':
        assert calls == {'primary': 2 if plugin == 'hindsight-reviewer' else 1}, calls
    elif mode in {'exhausted', 'malformed', 'trust_denied'}:
        initial = 2 if plugin == 'hindsight-reviewer' else 1
        assert calls['primary'] == initial, calls
        assert calls['backup'] == (0 if mode == 'trust_denied' else initial), calls
    else:
        assert calls == {'primary': stages, 'backup': stages}, calls
    print('REVIEWER_E2E_RESULT=' + json.dumps({'plugin': plugin, 'scenario': mode, 'http_calls': calls,
                      'write_verified': expected_write, 'notice_count': len(notices),
                      'boundary': 'registered real post_llm_call event -> native PluginLlm -> real auxiliary/HTTP -> real reviewer writes/readback',
                      'external_dependencies': 'loopback model and Hindsight REST; real isolated Jot store/CLI; seeded recalled-source fixture'}))
finally:
    clock.stop()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()
