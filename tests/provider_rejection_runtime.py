"""Exercise native title persistence and plugin recovery against a loopback provider."""

import hashlib
import json
import os
import time
import socket
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

scenario, home_arg, root_arg = sys.argv[1:]
home, root = Path(home_arg), Path(root_arg)
core = Path(os.environ['HERMES_PROVIDER_E2E_CORE'])
home.mkdir(parents=True, exist_ok=True)
os.environ.update(HERMES_HOME=str(home), HERMES_DATA_DIR=str(home), HOME=str(home),
                  HERMES_DISABLE_LAZY_INSTALLS='1', NO_PROXY='127.0.0.1,localhost')
for key in list(os.environ):
    if any(word in key for word in ('TOKEN', 'API_KEY', 'SECRET', 'PROXY')) and key != 'NO_PROXY':
        os.environ.pop(key, None)
sys.path.insert(0, str(core))
import hermes_bootstrap
import hermes_yaml

original_connect = socket.socket.connect


def local_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in {'127.0.0.1', '::1'}:
        raise AssertionError('E2E attempted an external connection')
    return original_connect(sock, address)


socket.socket.connect = local_connect
calls = Counter()
requests = []
expected_title = 'Hindsight raw memory cleanup'


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

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        lane = 'primary' if self.path.startswith('/primary/') else 'backup'
        calls[lane] += 1
        requests.append({'lane': lane, 'path': self.path, 'model': body.get('model')})
        assert self.path == '/' + lane + '/v1/chat/completions', self.path
        assert body['messages'][0]['role'] == 'system'
        assert body['response_format']['type'] == 'json_schema'
        if lane == 'primary' and scenario != 'healthy':
            code = 'upstream_request_rejected'
            message = 'The model provider rejected the request.'
            if scenario == 'unrelated':
                code, message = 'invalid_request_error', 'Invalid messages parameter.'
            elif scenario == 'policy':
                code, message = 'content_policy_violation', 'Content policy violation.'
            elif scenario == 'different_message':
                message = 'Invalid response_format parameter.'
            self.reply(400, {'error': {'code': code, 'message': message}})
            return
        if lane == 'backup' and scenario == 'exhausted':
            self.reply(400, {'error': {'code': 'invalid_request_error', 'message': 'Invalid request.'}})
            return
        self.reply(200, {'id': 'fixture-title', 'object': 'chat.completion', 'created': 0,
                         'model': body['model'], 'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': json.dumps({'title': expected_title})}}],
                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = 'http://127.0.0.1:' + str(server.server_port)
key = 'hermes-agent-guards/provider-guards'
category = home / 'plugins/hermes-agent-guards'
category.mkdir(parents=True)
(category / 'provider-guards').symlink_to(root / 'provider-guards', target_is_directory=True)
copilot = Path(os.environ['HERMES_PROVIDER_E2E_COPILOT'])
(home / 'plugins/github-copilot').symlink_to(copilot, target_is_directory=True)
primary_provider = 'custom:other-fixture' if scenario == 'other_provider' else 'copilot'
config = {'model': {'default': 'main-fixture', 'provider': 'custom:backup-fixture'},
          'providers': {name + '-fixture': {'base_url': base + '/' + lane + '/v1',
                         'api_key': 'synthetic', 'transport': 'chat_completions', 'default_model': name + '-fixture'}
                        for name, lane in [('backup', 'backup'), ('other', 'primary')]},
          'auxiliary': {'title_generation': {'provider': primary_provider, 'model': 'primary-fixture',
                         'base_url': base + '/primary/v1', 'timeout': 5,
                         'fallback_chain': [{'provider': 'custom:backup-fixture', 'model': 'backup-fixture'}]}},
          'plugins': {'enabled': ['github-copilot', key]}}
if scenario == 'other_provider':
    config['auxiliary']['title_generation']['api_key'] = 'synthetic'
os.environ['COPILOT_GITHUB_TOKEN'] = 'ghu_fixture'
fingerprint = hashlib.sha256(b'ghu_fixture').hexdigest()[:16]
(home / '.copilot_jwt.json').write_text(json.dumps({fingerprint: {
    'api_token': 'fixture-jwt', 'expires_at': time.time() + 3600, 'base_url': base + '/primary/v1'}}))
(home / 'config.yaml').write_text(hermes_yaml.safe_dump(config), encoding='utf-8')

from hermes_cli import plugins
from hermes_state import SessionDB
from agent.title_generator import apply_instant_title, auto_title_session

plugins.discover_plugins(force=True)
assert plugins.get_plugin_manager()._plugins[key].module is not None
notices, failures = [], []
db = SessionDB(home / 'state.db')
session_id = 'provider-rejection-fixture'
user = 'Should raw world and experience memories be recalled for cleanup?'
try:
    db.create_session(session_id, source='cli')
    apply_instant_title(db, session_id, user)
    placeholder = db.get_session_title(session_id)
    assert db.get_session_title_source(session_id) == 'derived'
    auto_title_session(db, session_id, user, title_callback=lambda *args: notices.append(args),
                       failure_callback=lambda *args: failures.append(str(args[1])))
    success = scenario in {'recovery', 'healthy'}
    expected_calls = {'primary': 1}
    if scenario in {'recovery', 'exhausted'}:
        expected_calls['backup'] = 1
    assert dict(calls) == expected_calls, (scenario, calls, requests, failures)
    assert db.get_session_title(session_id) == (expected_title if success else placeholder), (scenario, failures)
    assert db.get_session_title_source(session_id) == ('llm' if success else 'derived')
    assert notices == ([(expected_title, 'llm')] if success else []), notices
    assert len(failures) == int(not success), failures
    db.close()
    db = SessionDB(home / 'state.db')
    assert db.get_session_title_source(session_id) == ('llm' if success else 'derived')
    print('PROVIDER_E2E_RESULT=' + json.dumps({'scenario': scenario, 'calls': calls,
          'title_source': db.get_session_title_source(session_id), 'callback_count': len(notices),
          'boundary': 'native auto_title_session -> registered plugin -> auxiliary HTTP fallback -> SessionDB readback',
          'external_dependency': 'loopback OpenAI-compatible provider'}))
finally:
    db.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()
