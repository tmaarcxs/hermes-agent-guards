"""Background review of the Hindsight memories injected into a turn.

After each gateway turn, one daemon thread checks every memory injected into that
turn against the user's own message, using the model selected for cron jobs (or
the main model when none is set). A memory is invalidated only when a first pass
proposes it with an exact quote from the user's message and a skeptical second
pass confirms the quote refutes it. The thread re-reads the memory just before the
standard ``PATCH`` and requires it to be valid and unchanged; the stock API has no
compare-and-set, so an edit landing between that re-read and the write can still be
invalidated. Afterwards it reads the state back and posts one notice to the
originating chat. Nothing is queued or retried: a failure skips that turn.

Recall ids are captured by wrapping the Hindsight provider's ``_recall`` during
``prefetch``; the injected block itself is produced by the provider unchanged.
"""

import asyncio
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

from . import policy

logger = logging.getLogger(__name__)
CTX = None
_gateway = {}
_recalls = {}
_lock = threading.Lock()
_SECRET = re.compile(r'(?i)(?:bearer\s+\S{12,}|(?:api[_-]?key|client[_-]?secret|password|token)\s*[:=]\s*["\']?\S{12,}|\bsk-[a-zA-Z0-9_-]{16,})')
NOTICE_ONE = 'A Hindsight memory was invalidated after this turn.'
NOTICE_MANY = 'Hindsight memories were invalidated after this turn.'


def patch_provider():
    """Wrap the Hindsight provider's prefetch once so it records the recalled ids per session."""
    from plugins.memory import import_provider_module
    module = import_provider_module('hindsight')
    cls = getattr(module, 'HindsightMemoryProvider', None) if module else None
    if cls is None or getattr(cls.prefetch, '_hindsight_reviewer', False):
        return False
    original = cls.prefetch

    def prefetch(self, query, *, session_id=''):
        captured = []
        had_own = '_recall' in self.__dict__
        recall = self._recall

        def observed(*args, **kwargs):
            items = recall(*args, **kwargs)
            captured.append(items)
            return items
        self._recall = observed
        try:
            result = original(self, query, session_id=session_id)
        finally:
            if had_own:
                self._recall = recall
            else:
                del self._recall
        try:
            if (len(captured) == 1 and result and session_id and self._recall_sync
                    and self._prefetch_method == 'recall' and self._build_recall_bank_ids() == [self._bank_id]):
                items = [item for item in captured[0] if item.text]
                records = [{'id': str(item.id), 'text': item.text} for item in items if getattr(item, 'id', None)]
                if records and len(records) == len(items):
                    with _lock:
                        _recalls[session_id] = {'bank': self._bank_id, 'records': records, 'block': result}
        except Exception:
            logger.warning('Hindsight review could not record recall ids; the turn is skipped', exc_info=True)
        return result

    prefetch._hindsight_reviewer = True
    cls.prefetch = prefetch
    return True


async def on_pre_gateway_dispatch(gateway=None, **_):
    """Remember the running gateway and its loop so the background thread can post the notice."""
    if gateway is not None:
        _gateway['runner'], _gateway['loop'] = gateway, asyncio.get_running_loop()


def _origin():
    from gateway.session_context import get_session_env
    origin = {name: get_session_env('HERMES_SESSION_' + name.upper(), '') for name in
              ('platform', 'chat_id', 'chat_type', 'thread_id', 'user_id', 'message_id', 'profile')}
    return origin if origin['platform'] and origin['chat_id'] and origin['platform'] != 'local' else None


def on_post_llm_call(session_id='', turn_id='', user_message=None, assistant_response=None,
                     conversation_history=None, **_):
    """Start one background review for a completed gateway turn whose injected block is known."""
    with _lock:
        recall = _recalls.pop(session_id, None)
    try:
        if not recall or not isinstance(user_message, str) or not isinstance(assistant_response, str):
            return
        origin = _origin()
        if origin is None:
            return
        rows = conversation_history or []
        user = next((row for row in reversed(rows) if isinstance(row, dict) and row.get('role') == 'user'), None)
        from agent.memory_context import build_memory_context_block
        api_content = user.get('api_content') if user else None
        if not isinstance(api_content, str) or not api_content.startswith(
                user_message + '\n\n' + build_memory_context_block(recall['block'])):
            logger.info('Hindsight review skipped turn: injected memories could not be matched')
            return
        from hermes_constants import get_hermes_home
        job = {'home': str(get_hermes_home()), 'session_id': session_id, 'turn_id': turn_id,
               'user_message': user_message, 'recall': recall, 'origin': origin}
        import contextvars
        copied = contextvars.copy_context()
        threading.Thread(target=lambda: copied.run(_run, job), name='hindsight-reviewer', daemon=True).start()
    except Exception:
        logger.warning('Hindsight review could not start; the turn is skipped', exc_info=True)


def _run(job):
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    token = set_secret_scope(build_profile_secret_scope(Path(job['home'])), profile_home=job['home'])
    try:
        review(CTX.llm, job)
    except Exception as error:
        logger.warning('Hindsight review failed (%s: %s); the turn is skipped', type(error).__name__, error)
    finally:
        reset_secret_scope(token)


def _connection(home):
    from agent.secret_scope import get_secret
    cfg = json.loads((Path(home) / 'hindsight' / 'config.json').read_text(encoding='utf-8'))
    url = cfg.get('api_url') or get_secret('HINDSIGHT_API_URL', '')
    key = cfg.get('apiKey') or cfg.get('api_key') or get_secret('HINDSIGHT_API_KEY', '')
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        raise ValueError('Hindsight API URL is not configured')
    return url.rstrip('/'), key


def _http(method, url, key, body=None):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30) as response:
        return json.load(response)


def cron_route(home):
    """Provider/model selected for cron jobs; empty means the main model."""
    import hermes_yaml
    cron = (hermes_yaml.safe_load((Path(home) / 'config.yaml').read_text(encoding='utf-8')) or {}).get('cron') or {}
    route = {}
    if isinstance(cron.get('model_provider'), str) and cron['model_provider']:
        route['provider'] = cron['model_provider']
    if isinstance(cron.get('model'), str) and cron['model']:
        route['model'] = cron['model']
    return route


def _sources(base, key, records):
    """Primary facts behind the injected memories; identical texts and observations' own rows are skipped."""
    counts = Counter(record['text'] for record in records)
    sources = {}
    for record in records:
        if counts[record['text']] != 1:
            continue
        detail = _http('GET', base + str(uuid.UUID(record['id'])), key)
        if detail.get('state') != 'valid' or detail.get('text') != record['text']:
            continue
        ids = detail.get('source_memory_ids') if detail.get('type') == 'observation' else [record['id']]
        if not isinstance(ids, list) or not ids or len(ids) > 50:
            continue
        for source in map(lambda value: str(uuid.UUID(value)), ids):
            if source in sources:
                sources[source]['recall_ids'].append(record['id'])
                continue
            try:
                fact = _http('GET', base + source, key)
            except urllib.error.HTTPError as error:
                if error.code in {400, 404, 409}:
                    continue
                raise
            text = fact.get('text')
            if (fact.get('state') != 'valid' or (fact.get('type') or fact.get('fact_type')) == 'observation'
                    or not isinstance(text, str) or not text or len(text) > 5000 or _SECRET.search(text)):
                continue
            sources[source] = {'text': text, 'recall_ids': [record['id']]}
    return sources


def _invalidate(base, key, source_id, text, reason):
    """Re-read, write, read back. Returns 'applied' or 'conflict'."""
    detail = _http('GET', base + source_id, key)
    if detail.get('state') != 'valid' or detail.get('text') != text:
        return 'conflict'
    try:
        _http('PATCH', base + source_id, key, {'state': 'invalidated', 'reason': reason})
    except urllib.error.HTTPError as error:
        if error.code in {400, 404, 409}:
            return 'conflict'
        raise
    detail = _http('GET', base + source_id, key)
    if detail.get('state') != 'invalidated' or detail.get('invalidation_reason') != reason:
        raise RuntimeError('Invalidation read-back failed')
    return 'applied'


def review(llm, job):
    """Review one turn; returns the number of invalidated memories."""
    user = job['user_message']
    if len(user) > 16000 or _SECRET.search(user):
        return 0
    url, key = _connection(job['home'])
    base = url + '/v1/default/banks/' + urllib.parse.quote(job['recall']['bank'], safe='') + '/memories/'
    sources = _sources(base, key, job['recall']['records'])
    evidence = '[CURRENT USER MESSAGE]\n' + user
    route = cron_route(job['home'])
    cases = [{'id': sid, 'memory': row['text'], 'evidence': evidence} for sid, row in sources.items()]
    decisions = []
    for offset in range(0, len(cases), 20):
        decisions.extend(policy.propose(llm, cases[offset:offset + 20], route))
    outcomes = Counter()
    for row in decisions:
        if row['decision'] != 'candidate' or row['evidence_quote'] not in user:
            continue
        text = sources[row['id']]['text']
        if not policy.confirm(llm, text, row['evidence_quote'], evidence, route):
            continue
        event = uuid.uuid5(uuid.NAMESPACE_URL, 'hindsight-review:' + job['session_id'] + ':' + job['turn_id'] + ':' + row['id'])
        outcomes[_invalidate(base, key, row['id'], text, 'hindsight-review:' + str(event))] += 1
    logger.info('Hindsight review outcome: reviewed=%d applied=%d conflict=%d',
                len(cases), outcomes['applied'], outcomes['conflict'])
    if outcomes['applied']:
        notify(job['origin'], NOTICE_ONE if outcomes['applied'] == 1 else NOTICE_MANY)
    return outcomes['applied']


def notify(origin, text):
    """Send one notice to the originating chat/topic through the running gateway."""
    from gateway.config import Platform
    from gateway.delivery import DeliveryRouter, DeliveryTarget
    from gateway.session import SessionSource
    runner, loop = _gateway.get('runner'), _gateway.get('loop')
    if runner is None or loop is None or not loop.is_running():
        raise RuntimeError('Gateway is not running')
    platform = Platform(origin['platform'])
    adapter = runner._authorization_adapter(platform, origin['profile'] or None)
    if adapter is None:
        raise RuntimeError('Origin adapter is not connected')
    source = SessionSource(platform=platform, chat_id=origin['chat_id'], chat_type=origin['chat_type'] or 'dm',
                           user_id=origin['user_id'] or None, thread_id=origin['thread_id'] or None)
    target = DeliveryTarget.parse('origin', origin=source)
    config = runner._profile_configs.get(origin['profile']) or runner.config
    metadata = {'telegram_reply_to_message_id': origin['message_id']} if origin['platform'] == 'telegram' and origin['message_id'] else None
    future = asyncio.run_coroutine_threadsafe(
        DeliveryRouter(config, adapters={platform: adapter}).deliver(text, [target], metadata=metadata), loop)
    result = future.result(timeout=45)
    item = result.get(target.to_string()) if isinstance(result, dict) else None
    if not isinstance(item, dict) or not item.get('success'):
        raise RuntimeError('Notice delivery failed: ' + str(item)[:200])
