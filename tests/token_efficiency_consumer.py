"""Loaded-plugin consumer using real dispatch; tool payloads and API events are controlled inputs."""
import io
import json
import logging
import os
from pathlib import Path
import socket
import sys

core, home, scenario = sys.argv[1:]
setattr(sys, '_hermes_relaunched', True)
sys.path.insert(0, core)
os.environ['HERMES_HOME'] = home
original_connect = socket.socket.connect


def local_only(self, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
        raise AssertionError('external network forbidden')
    return original_connect(self, address)


socket.socket.connect = local_only
import hermes_cli.plugins as plugins
from hermes_cli.lifecycle import invoke_hook
from hermes_cli.middleware import run_llm_execution_middleware
from model_tools import handle_function_call
from tools.registry import registry

plugins.discover_plugins(force=True)
schema = {'name': 'fixture_payload', 'description': 'Controlled external JSON producer', 'parameters': {'type': 'object', 'properties': {'raw': {'type': 'string'}}, 'required': ['raw']}}
registry.register('fixture_payload', 'fixture', schema, lambda args, **kwargs: args['raw'], max_result_size_chars=float('inf'))


def call(raw, cid):
    return handle_function_call('fixture_payload', {'raw': raw}, tool_call_id=cid, task_id='fixture-task', session_id='fixture-session', turn_id='fixture-turn')


if scenario == 'json':
    fixtures = [' { "number": -0.00e+02, "text": "  keep \\"quoted\\" spaces  ", "items": [true, null, "ñ"] } ', ' [ 9007199254740993, 9007199254740993, 1e999999, -0.000 ] ']
    for i, raw in enumerate(fixtures):
        out = call(raw, str(i))
        assert out != raw
        assert json.loads(out, parse_int=str, parse_float=str) == json.loads(raw, parse_int=str, parse_float=str)
        for literal in ('-0.00e+02', '9007199254740993', '1e999999', '-0.000', '  keep '):
            if literal in raw:
                assert literal in out
elif scenario == 'composition':
    raw = json.dumps({'error': 'agent-guards blocked fixture_payload [fixture_gate]: call fixture_payload with a different payload.'})
    first = call(raw, 'first')
    assert json.loads(first) == json.loads(raw)
    repeated = call(raw, 'repeat')
    assert 'repeat_block_gate:' in repeated and 'fixture_gate' in repeated and raw in repeated, repeated
    successful = '{ "ok": true, "literal": "  unchanged  " }'
    assert json.loads(call(successful, 'other')) == json.loads(successful)
    assert json.loads(call(raw, 'reset')) == json.loads(raw), 'an unrelated result must reset the guard counter'
elif scenario == 'opaque':
    for i, raw in enumerate(['12:34:56\nESSENTIAL BUSINESS FACT\n', '{"same":1,"same":2}', '{"n":NaN}', '{invalid', '  a raw string  ', '[1, Infinity]']):
        assert call(raw, str(i)) == raw
    oversized = '{ "text": "' + 'x' * (4 * 1024 * 1024) + '" }'
    assert call(oversized, 'oversized') == oversized
elif scenario in ('observer', 'observer_mutation', 'observer_error', 'observer_off', 'observer_resume'):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger('token_efficiency')
    logger.addHandler(handler)
    canary = 'DO_NOT_PERSIST_PRIVATE_CANARY'
    request = {'model': 'fixture', 'messages': [{'role': 'user', 'content': canary + 'x' * 17000}], 'extra_headers': {'Authorization': canary}}
    transport_calls = []
    reply = object()
    failure = RuntimeError(canary)

    def transport(actual):
        transport_calls.append(json.dumps(actual))
        if scenario == 'observer_error' and len(transport_calls) == 2:
            raise failure
        return reply
    invoke_hook('post_compaction', kind='summarize', session_id='fixture-session', before_tokens=300, after_tokens=100, messages_removed=-1, private=canary)
    for i in range(3):
        if i == 1:
            request['messages'].extend([{'role': 'assistant', 'content': 'acknowledged'}, {'role': 'user', 'content': 'next'}])
            if scenario == 'observer_mutation':
                request['messages'][0]['content'] += 'changed after the hook-view cap'
        original = json.dumps(request)
        api_id = 'observer-api-' + str(i)
        invoke_hook('pre_api_request', request=request, api_request_id=api_id, model='fixture', provider='fixture', task_id='fixture-task', session_id='fixture-session', turn_id='fixture-turn')
        failed = scenario == 'observer_error' and i == 1
        try:
            returned = run_llm_execution_middleware(request, transport, api_request_id=api_id, model='fixture', provider='fixture', task_id='fixture-task', session_id='fixture-session', turn_id='fixture-turn')
            assert not failed and returned is reply
        except RuntimeError as error:
            assert failed and error is failure
        assert transport_calls[-1] == original and json.dumps(request) == original
        usage = None if failed else {'prompt_tokens': 9007199254740993, 'completion_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 80}, 'private': canary}
        invoke_hook('post_api_request', usage=usage, api_request_id=api_id, model='fixture', provider='fixture')
    logs = stream.getvalue()
    assert canary not in logs and len(transport_calls) == 3
    assert not plugins.has_middleware('llm_execution'), 'expired observation must release the middleware layer'
    if scenario in ('observer_off', 'observer_resume'):
        assert logs == ''
    else:
        records = [json.loads(line.split('telemetry: ', 1)[1]) for line in logs.splitlines()]
        assert len(records) == (4 if scenario == 'observer_error' else 5), records
        assert sum(record['event'] == 'compaction_boundary' for record in records) == 1
        boundary = next(record for record in records if record['event'] == 'compaction_boundary')
        assert boundary['kind'] == 'summarize' and boundary['core_estimates'] == {'before_tokens': 300, 'after_tokens': 100, 'messages_removed': -1}
        assert {record['session_ref'] for record in records if record['event'] in ('compaction_boundary', 'request_view')} == {boundary['session_ref']}
        assert len({record['fingerprint_epoch'] for record in records}) == 1
        assert {record['api_request_id'] for record in records if 'api_request_id' in record} == {'observer-api-0', 'observer-api-1'}
        views = [record for record in records if record['event'] == 'request_view']
        assert all(record['fingerprint_scope'] == 'sdk_request' for record in views)
        assert views[1]['previous_history_count'] == 1 and views[1]['history_count'] == 3
        assert views[1]['previous_history_hash'] == views[0]['history_view_hash']
        assert (views[1]['current_prefix_at_previous_count_hash'] == views[0]['history_view_hash']) is (scenario != 'observer_mutation')
        assert all(record['usage_view']['prompt_tokens'] == 9007199254740993 for record in records if record['event'] == 'usage_view')
else:
    raise AssertionError('unknown scenario')
Path(home, 'consumer-result.json').write_text(json.dumps({'scenario': scenario, 'passed': True}))
