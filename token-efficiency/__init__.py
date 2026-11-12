"""Pack new JSON results and observe bounded, content-free runtime metadata."""
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time

LOG = logging.getLogger('token_efficiency')
KEY = secrets.token_bytes(32)
LOCK = threading.Lock()
REMAINING = 0
DEADLINE = 0.0
DATABASE = None
RUN = 'once'
REQUESTS = set()
PRIOR = {}
REGISTRATION = None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('nonstandard JSON constant')


def _json(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_int=str, parse_float=str)


def _pack(text):
    if not isinstance(text, str) or text.lstrip()[:1] not in ('{', '['):
        return None
    _json(text)
    quoted = False
    escaped = False
    output = []
    for char in text:
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            output.append(char)
        elif char not in ' \t\r\n':
            output.append(char)
    return ''.join(output)


def _hash(value):
    data = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hmac.new(KEY, data.encode('utf-8', errors='surrogatepass'), hashlib.sha256).hexdigest()



def _emit(event, data):
    LOG.info('Token efficiency telemetry: %s', json.dumps(dict(event=event, fingerprint_epoch=_hash('epoch')[:16], **data), separators=(',', ':')))


def _transform(**data):
    try:
        raw = data.get('result')
        if not isinstance(raw, str) or len(raw) > 4 * 1024 * 1024:
            return None
        packed = _pack(raw)
        if packed is None:
            return None

        if len(packed) < len(raw):
            return packed
    except Exception:
        return None
    return None


def _observe_request(**data):
    global REMAINING
    try:
        request = data.get('request')
        api_id = data.get('api_request_id')
        if not isinstance(request, dict) or not isinstance(api_id, str) or len(api_id) > 256:
            return None
        with LOCK:
            if api_id in REQUESTS or DATABASE is None or REMAINING <= 0 or time.time() >= DEADLINE:
                return None
            with sqlite3.connect(DATABASE, timeout=1) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT remaining, expires FROM windows WHERE run = ?', (RUN,)).fetchone()
                if row is None or row[0] <= 0 or time.time() >= row[1]:
                    REMAINING = 0
                    return None
                db.execute('UPDATE windows SET remaining = remaining - 1 WHERE run = ?', (RUN,))
                REMAINING = row[0] - 1
            REQUESTS.add(api_id)
        messages = request.get('messages') or request.get('input') or []
        messages = messages if isinstance(messages, list) else []
        system = [message for message in messages if isinstance(message, dict) and message.get('role') in ('system', 'developer')]
        history = [message for message in messages if message not in system]
        session = _hash(data['session_id']) if isinstance(data.get('session_id'), str) and data['session_id'] else None
        system_hash, history_hash, tools_hash = _hash([request.get('instructions'), system]), _hash(history), _hash(request.get('tools'))
        route = _hash([data.get('provider'), data.get('base_url'), data.get('api_mode'), request.get('model')])
        previous = None
        if session is not None:
            key = (session, route, system_hash, tools_hash)
            with LOCK:
                previous = PRIOR.get(key)
                PRIOR[key] = (len(history), history_hash)
        _emit('request_view', {'api_request_id': api_id, 'session_ref': session, 'route_ref': route, 'request_view_hash': _hash(request), 'system_view_hash': system_hash, 'history_view_hash': history_hash, 'tools_view_hash': tools_hash, 'fingerprint_scope': 'sdk_request', 'history_count': len(history), 'previous_history_count': previous[0] if previous else None, 'previous_history_hash': previous[1] if previous else None, 'current_prefix_at_previous_count_hash': _hash(history[:previous[0]]) if previous else None})
    except Exception:
        return None
    return None


def _post_api(**data):
    try:
        api_id = data.get('api_request_id')
        with LOCK:
            if api_id not in REQUESTS:
                return None
            REQUESTS.remove(api_id)
        usage = data.get('usage')
        if not isinstance(usage, dict):
            return None
        allowed = ('prompt_tokens', 'input_tokens', 'completion_tokens', 'output_tokens', 'total_tokens', 'cached_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'reasoning_tokens')
        clean = {key: usage[key] for key in allowed if isinstance(usage.get(key), int) and not isinstance(usage[key], bool) and usage[key] >= 0}
        for key in ('prompt_tokens_details', 'input_tokens_details', 'completion_tokens_details', 'output_tokens_details'):
            detail = usage.get(key)
            if isinstance(detail, dict):
                values = {name: detail[name] for name in ('cached_tokens', 'reasoning_tokens') if isinstance(detail.get(name), int) and not isinstance(detail[name], bool) and detail[name] >= 0}
                if values:
                    clean[key] = values
        _emit('usage_view', {'api_request_id': data.get('api_request_id'), 'usage_view': clean})
    except Exception:
        return None
    return None


def _release_if_inactive():
    global REGISTRATION
    with LOCK:
        registration = REGISTRATION if REMAINING <= 0 or time.time() >= DEADLINE else None
        if registration is not None:
            REGISTRATION = None
            PRIOR.clear()
    if registration is not None:
        registration.dispose()


def _full_request(*, next_call, request, **data):
    try:
        _observe_request(request=request, **data)
        _release_if_inactive()
    except Exception:
        pass
    return next_call(request)


def _request_hook(**data):
    _release_if_inactive()
    return None


def _reset(**data):
    if REMAINING > 0 and time.time() < DEADLINE:
        counts = {key: data[key] for key in ('before_tokens', 'after_tokens', 'messages_removed') if isinstance(data.get(key), int) and not isinstance(data[key], bool)}
        _emit('compaction_boundary', {'session_ref': _hash(data['session_id']) if isinstance(data.get('session_id'), str) and data['session_id'] else None, 'kind': data.get('kind') if data.get('kind') in ('summarize', 'micro_summarize', 'prune_tool_results', 'provider_native') else 'unspecified', 'core_estimates': counts})
    return None


def register(ctx):
    global REMAINING, DEADLINE, DATABASE, RUN, REGISTRATION
    with LOCK:
        REMAINING, DEADLINE, DATABASE, RUN = 0, 0.0, None, 'once'
        REQUESTS.clear()
        PRIOR.clear()
        REGISTRATION = None
    ctx.register_hook('transform_tool_result', _transform)
    try:
        budget = ctx.get_config('request_budget', default=2500)
        run = ctx.get_config('observation_run', default='once')
        if ctx.get_config('observe_requests', default=False) is True and isinstance(budget, int) and not isinstance(budget, bool) and 0 < budget <= 2500 and isinstance(run, str) and 0 < len(run) <= 128:
            directory = ctx.state.data_dir
            directory.mkdir(parents=True, exist_ok=True)
            DATABASE = directory / 'observation.sqlite3'
            RUN = run
            with sqlite3.connect(DATABASE, timeout=1) as db:
                db.execute('CREATE TABLE IF NOT EXISTS windows (run TEXT PRIMARY KEY, expires REAL NOT NULL, remaining INTEGER NOT NULL)')
                db.execute('INSERT OR IGNORE INTO windows VALUES (?, ?, ?)', (RUN, time.time() + 12 * 60 * 60, budget))
                db.execute('UPDATE windows SET remaining = MIN(remaining, ?) WHERE run = ?', (budget, RUN))
                DEADLINE, REMAINING = db.execute('SELECT expires, remaining FROM windows WHERE run = ?', (RUN,)).fetchone()
            LOG.setLevel(logging.INFO)
            if REMAINING > 0 and time.time() < DEADLINE:
                REGISTRATION = ctx.register_middleware('llm_execution', _full_request)
    except Exception:
        REMAINING = 0
        LOG.warning('Token observation unavailable; JSON packing remains active.')
    ctx.register_hook('pre_api_request', _request_hook)
    ctx.register_hook('post_api_request', _post_api)
    ctx.register_hook('post_compaction', _reset)
