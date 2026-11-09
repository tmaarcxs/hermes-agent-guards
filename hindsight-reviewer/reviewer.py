"""Background review of the Hindsight memories injected into a turn.

After each gateway turn, one daemon thread checks every memory injected into that
turn against the same conversation context Hermes' background review replays (recent
messages verbatim, older turns digested, including tool results), using the model selected for cron jobs (or
the main model when none is set). A memory is invalidated only when a first pass
proposes it with an exact quote from a user message or tool result in that context and a skeptical second
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
import sys
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

from . import policy

logger = logging.getLogger(__name__)
CTX = None
# Plugin reloads re-import this module, but live provider instances keep the wrappers of
# earlier imports; one process-wide registry keeps every generation feeding the same hook.
_state = sys.modules.setdefault('hermes_hindsight_reviewer_state', types.ModuleType('hermes_hindsight_reviewer_state'))
_recalls = _state.__dict__.setdefault('recalls', {})
_tool_recalls = _state.__dict__.setdefault('tool_recalls', {})
MEMORY_TOOLS = frozenset({'hindsight_recall', 'hindsight_reflect', 'hindsight_retain', 'memory', 'session_search'})
_lock = _state.__dict__.setdefault('lock', threading.Lock())
_gateway = _state.__dict__.setdefault('gateway', {})
_SECRET = re.compile(r'(?i)(?:bearer\s+\S{12,}|(?:api[_-]?key|client[_-]?secret|password|token)\s*[:=]\s*["\']?\S{12,}|\bsk-[a-zA-Z0-9_-]{16,})')
NOTICE_PREFIX = '🧠 Memory review: '
NOTICE_LABELS = {'superseded': 'superseded', 'contradicted': 'outdated', 'duplicate': 'duplicate', 'fragment': 'no context', 'transient': 'one-off'}
NOTICE_SHOWN = 3


def notice(items):
    """One short line: up to three invalidated memories with their reason, secrets masked."""
    parts = []
    for kind, text in items[:NOTICE_SHOWN]:
        text = ' '.join(text.split())
        text = '[withheld]' if _SECRET.search(text) else (text if len(text) <= 40 else text[:39].rstrip() + '…')
        parts.append(f"'{text}' ({NOTICE_LABELS[kind]})")
    more = f' · +{len(items) - NOTICE_SHOWN} more' if len(items) > NOTICE_SHOWN else ''
    return NOTICE_PREFIX + 'invalidated ' + ' · '.join(parts) + more


def patch_provider():
    """Wrap every loaded Hindsight provider class so prefetch and the recall tool record ids per session.

    A reload can load the provider again as a new class while live sessions keep instances of the
    old one, so every loaded copy is (re-)wrapped from its original methods.
    """
    from plugins.memory import import_provider_module
    module = import_provider_module('hindsight')
    classes = {getattr(module, 'HindsightMemoryProvider', None) if module else None}
    classes |= {getattr(mod, 'HindsightMemoryProvider', None) for mod in list(sys.modules.values())
                if isinstance(getattr(mod, 'HindsightMemoryProvider', None), type)}
    classes.discard(None)
    for cls in classes:
        _patch(cls)
    return bool(classes)


def _patch(cls):
    if '_hindsight_reviewer_originals' not in cls.__dict__:
        cls._hindsight_reviewer_originals = (cls.prefetch, cls._tool_recall)
    original, original_tool = cls._hindsight_reviewer_originals

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

    def tool_recall(self, args):
        """Run the provider's own recall tool, recording the returned ids for this turn's review."""
        captured = []
        had_own = '_recall' in self.__dict__
        recall = self._recall

        def observed(*a, **k):
            items = recall(*a, **k)
            captured.append(items)
            return items
        self._recall = observed
        try:
            return original_tool(self, args)
        finally:
            if had_own:
                self._recall = recall
            else:
                del self._recall
            try:
                records = [{'id': str(item.id), 'text': item.text} for items in captured for item in items
                           if getattr(item, 'id', None) and item.text]
                if records and self._session_id:
                    with _lock:
                        _tool_recalls.setdefault(self._session_id, {'bank': self._bank_id, 'records': []})['records'].extend(records)
            except Exception:
                logger.warning('Hindsight review could not record tool recall ids', exc_info=True)

    prefetch._hindsight_reviewer = True
    cls.prefetch = prefetch
    cls._tool_recall = tool_recall
    cls._TOOL_HANDLERS = {**cls._TOOL_HANDLERS, 'hindsight_recall': ('query', tool_recall, cls._TOOL_HANDLERS['hindsight_recall'][2])}


async def on_pre_gateway_dispatch(gateway=None, **_):
    """Remember the running gateway and its loop so the background thread can post the notice."""
    if gateway is not None:
        _gateway['runner'], _gateway['loop'] = gateway, asyncio.get_running_loop()


def _origin():
    from gateway.session_context import get_session_env
    origin = {name: get_session_env('HERMES_SESSION_' + name.upper(), '') for name in
              ('platform', 'chat_id', 'chat_type', 'thread_id', 'user_id', 'message_id', 'profile')}
    return origin if origin['platform'] and origin['chat_id'] and origin['platform'] != 'local' else None


def _turn_review_callback():
    """This turn's background-review sender, so the notice follows the skills review's delivery rules.

    It is held until the reply lands and dropped if a newer message superseded the turn.
    """
    try:
        from gateway.session_context import bound_session_key
        agents = getattr(_gateway.get('runner'), '_running_agents', None) or {}
        return getattr(agents.get(bound_session_key()), 'background_review_callback', None)
    except Exception:
        return None


def on_post_llm_call(session_id='', turn_id='', user_message=None, assistant_response=None,
                     conversation_history=None, **_):
    """Start one background review for a completed gateway turn whose injected block is known."""
    with _lock:
        injected = _recalls.pop(session_id, None)
        tooled = _tool_recalls.pop(session_id, None)
    try:
        if not (injected or tooled) or not isinstance(user_message, str) or not isinstance(assistant_response, str):
            return
        origin = _origin()
        if origin is None:
            return
        rows = conversation_history or []
        if injected:
            from agent.memory_context import build_memory_context_block
            expected = user_message + '\n\n' + build_memory_context_block(injected['block'])
            # A mid-turn steering message adds a newer user row, so look at every user row of the turn.
            if not any(isinstance(row, dict) and row.get('role') == 'user' and isinstance(row.get('api_content'), str)
                       and row['api_content'].startswith(expected) for row in rows):
                logger.info('Hindsight review: injected memories could not be matched; reviewing tool recalls only')
                injected = None
        recall = _merge(injected, tooled)
        if recall is None:
            return
        from hermes_constants import get_hermes_home
        transcript, quotable = conversation_context(rows)
        job = {'home': str(get_hermes_home()), 'session_id': session_id, 'turn_id': turn_id,
               'transcript': transcript, 'quotable': quotable, 'recall': recall, 'origin': origin,
               'deliver': _turn_review_callback()}
        import contextvars
        copied = contextvars.copy_context()
        threading.Thread(target=lambda: copied.run(_run, job), name='hindsight-reviewer', daemon=True).start()
    except Exception:
        logger.warning('Hindsight review could not start; the turn is skipped', exc_info=True)


_MEMORY_SPAN = re.compile(r'<memory-context>.*?</memory-context>', re.S)
MAX_CONTEXT_CHARS = 60000


def _text(message):
    content = message.get('content')
    if isinstance(content, list):
        content = ' '.join(block.get('text', '') for block in content if isinstance(block, dict))
    return _MEMORY_SPAN.sub('', content).strip() if isinstance(content, str) else ''


def conversation_context(rows):
    """The background review's routed-model replay rendered as text, plus the quotable text.

    Uses Hermes' own ``_digest_history`` (recent messages verbatim, older turns digested). Only
    user messages and tool results are quotable evidence; assistant text is context, never proof,
    and memory-tool results (recall, reflect, session search) are omitted so a memory never refutes
    another memory.
    Injected memory blocks are stripped so a memory can never be cited against itself.
    """
    from agent.background_review import _digest_history
    lines, quotable, memory_calls = [], [], set()
    for message in _digest_history([row for row in rows if isinstance(row, dict)]):
        role, text = message.get('role'), _text(message)
        for call in message.get('tool_calls') or []:
            if isinstance(call, dict) and (call.get('function') or {}).get('name') in MEMORY_TOOLS:
                memory_calls.add(call.get('id'))
        if role == 'assistant' and message.get('tool_calls'):
            names = [(call.get('function') or {}).get('name', '?') for call in message['tool_calls'] if isinstance(call, dict)]
            lines.append('[ASSISTANT TOOL CALLS] ' + ', '.join(names))
        if not text:
            continue
        lines.append('[' + str(role).upper() + ']\n' + text)
        if role == 'tool' and (message.get('tool_call_id') in memory_calls or message.get('name') in MEMORY_TOOLS):
            continue
        if role in {'user', 'tool'} and not text.startswith('[Earlier conversation digest'):
            quotable.append(text)
    transcript = '\n\n'.join(lines)
    return transcript[-MAX_CONTEXT_CHARS:], quotable


def _quotable(job, quote):
    return any(quote in text for text in job['quotable'])


def _merge(injected, tooled):
    """One review list: injected memories plus those the agent recalled itself, deduplicated by id."""
    parts = [part for part in (injected, tooled) if part]
    if not parts or len({part['bank'] for part in parts}) != 1:
        return None
    seen, records = set(), []
    for record in (record for part in parts for record in part['records']):
        if record['id'] not in seen:
            seen.add(record['id'])
            records.append(record)
    return {'bank': parts[0]['bank'], 'records': records}


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
            date = fact.get('mentioned_at') or fact.get('date') or ''
            sources[source] = {'text': text, 'date': date if isinstance(date, str) else '', 'recall_ids': [record['id']]}
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


MAX_PARALLEL = 8
MAX_ATTEMPTS = 10


def with_retries(call, *args, sleep=None):
    """Run one model call, retrying any error up to MAX_ATTEMPTS times with capped backoff."""
    import time
    sleep = sleep or time.sleep
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call(*args)
        except Exception as error:
            if attempt == MAX_ATTEMPTS:
                raise
            logger.info('Hindsight review model call failed (%s: %s); retry %d/%d', type(error).__name__,
                        str(error)[:120], attempt, MAX_ATTEMPTS - 1)
            sleep(min(2 * attempt, 20))


def with_bisect(call, llm, batch, *rest):
    """Retry a batch; if the provider still rejects it, halve it so one bad memory loses only itself."""
    try:
        return with_retries(call, llm, batch, *rest)
    except Exception:
        if len(batch) == 1:
            raise
    half = len(batch) // 2
    return _split(call, llm, batch[:half], *rest) + _split(call, llm, batch[half:], *rest)


def _split(call, llm, batch, *rest):
    try:
        return call(llm, batch, *rest)
    except Exception as error:
        if len(batch) == 1:
            logger.warning('Hindsight %s skipped memory %s (%s: %s)', call.__name__, batch[0].get('id'),
                           type(error).__name__, str(error)[:200])
            return []
    half = len(batch) // 2
    return _split(call, llm, batch[:half], *rest) + _split(call, llm, batch[half:], *rest)


def review(llm, job):
    """Review one turn; returns the number of invalidated memories."""
    url, key = _connection(job['home'])
    base = url + '/v1/default/banks/' + urllib.parse.quote(job['recall']['bank'], safe='') + '/memories/'
    sources = _sources(base, key, job['recall']['records'])
    evidence = job['transcript']
    route = cron_route(job['home'])
    cases = [{'id': sid, 'memory': row['text'], 'date': row.get('date', '')[:10]} for sid, row in sources.items()]
    batches = [cases[offset:offset + 20] for offset in range(0, len(cases), 20)]
    decisions, noisy, applied = [], [], []
    outcomes = Counter()
    if batches:
        from concurrent.futures import ThreadPoolExecutor
        import contextvars
        with ThreadPoolExecutor(max_workers=min(2 * len(batches), MAX_PARALLEL)) as pool:
            # Pool threads start with an empty context, and the gateway's scoped credentials live in
            # it: copy this thread's context once per call (a Context cannot be entered twice at once).
            submit = lambda call, *args: pool.submit(contextvars.copy_context().run, with_bisect, call, *args)
            refuted = [submit(policy.propose, llm, batch, route, evidence) for batch in batches]
            flagged = [submit(policy.noise, llm, batch, route, cases) for batch in batches]
            # A batch the provider keeps rejecting skips only its own memories, not the whole turn.
            for name, futures, sink in (('review', refuted, decisions), ('noise', flagged, noisy)):
                for batch, future in zip(batches, futures):
                    try:
                        sink.extend(future.result())
                    except Exception as error:
                        outcomes['failed_batches'] += 1
                        logger.warning('Hindsight %s batch of %d memories skipped (%s: %s)', name, len(batch),
                                       type(error).__name__, str(error)[:200])
    for row in decisions:
        if row['decision'] != 'candidate' or not _quotable(job, row['evidence_quote']) or _SECRET.search(row['evidence_quote']):
            continue
        text = sources[row['id']]['text']
        if not with_retries(policy.confirm, llm, text, row['evidence_quote'], evidence, route):
            continue
        event = uuid.uuid5(uuid.NAMESPACE_URL, 'hindsight-review:' + job['session_id'] + ':' + job['turn_id'] + ':' + row['id'])
        outcome = _invalidate(base, key, row['id'], text, 'hindsight-review:' + str(event))
        outcomes[outcome] += 1
        if outcome == 'applied':
            applied.append(('contradicted', text))
        logger.info('Hindsight review %s memory %s (contradicted)', outcome, row['id'])
    refuted_ids = {row['id'] for row in decisions if row['decision'] == 'candidate'}
    noisy = policy.survivors(noisy)
    for row in noisy:
        if row['id'] in refuted_ids:
            continue
        text = sources[row['id']]['text']
        kept = sources[row['duplicate_of']]['text'] if row['duplicate_of'] else ''
        if not with_retries(policy.confirm_noise, llm, text, row['reason'], kept, route):
            continue
        event = uuid.uuid5(uuid.NAMESPACE_URL, 'hindsight-noise:' + job['session_id'] + ':' + job['turn_id'] + ':' + row['id'])
        outcome = _invalidate(base, key, row['id'], text, 'hindsight-noise:' + row['reason'] + ':' + str(event))
        outcomes[outcome] += 1
        outcomes['noise'] += outcome == 'applied'
        if outcome == 'applied':
            applied.append((row['reason'], text))
        logger.info('Hindsight review %s memory %s (noise: %s)', outcome, row['id'], row['reason'])
    logger.info('Hindsight review outcome: reviewed=%d applied=%d noise=%d conflict=%d failed_batches=%d',
                len(cases), outcomes['applied'], outcomes['noise'], outcomes['conflict'], outcomes['failed_batches'])
    if outcomes['applied']:
        if job.get('deliver'):
            job['deliver'](notice(applied))
            logger.info('Hindsight review notice handed to the turn background-review sender')
        else:
            notify(job['origin'], notice(applied))
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
