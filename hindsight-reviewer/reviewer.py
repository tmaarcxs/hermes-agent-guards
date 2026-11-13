"""Background review of the Hindsight memories injected into a turn.

After each gateway turn, one daemon thread checks every memory injected into that
turn against the same conversation context Hermes' background review replays (recent
messages verbatim, older turns digested, including tool results), using the model selected for cron jobs (or
the main model when none is set). A memory is invalidated only when a first pass
proposes it with an exact quote from a user message or tool result in that context and a skeptical second
pass confirms the quote refutes it. The thread re-reads the memory just before the
standard ``PATCH`` and requires it to be valid and unchanged; the stock API has no
compare-and-set, so an edit landing between that re-read and the write can still be
invalidated. A newer admitted message on the same route cancels unfinished writes
and suppresses the stale notice; completed writes remain visible as a partial
outcome. Notices use the originating turn's background-review sender. Each stage
tries its selected route once, then the configured global provider/model fallbacks
once each after an eligible provider failure. Hermes' own transport recovery is
separate; exhausted calls are skipped, never queued or split.

Recall ids are captured by wrapping the Hindsight provider's ``_recall`` during
``prefetch``; the injected block itself is produced by the provider unchanged.
Plugin reloads re-import this module, but live provider instances keep earlier
wrappers, so one process-wide registry keeps every generation feeding the same
hook.
"""

import asyncio
import json
import logging
import re
import sys
import threading
import time
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
_state = sys.modules.setdefault('hermes_hindsight_reviewer_state', types.ModuleType('hermes_hindsight_reviewer_state'))
_recalls = _state.__dict__.setdefault('recalls', {})
_tool_recalls = _state.__dict__.setdefault('tool_recalls', {})
MEMORY_TOOLS = frozenset({'hindsight_recall', 'hindsight_reflect', 'hindsight_retain', 'memory', 'session_search'})
_lock = _state.__dict__.setdefault('lock', threading.Lock())
_write_lock = _state.__dict__.setdefault('write_lock', threading.Lock())
_gateway = _state.__dict__.setdefault('gateway', {})
_active_reviews = _state.__dict__.setdefault('active_reviews', {})
_latest_admissions = _state.__dict__.setdefault('latest_admissions', {})
_SECRET = re.compile(r'(?i)(?:bearer\s+\S{12,}|(?:api[_-]?key|client[_-]?secret|password|token)\s*[:=]\s*["\']?\S{12,}|\bsk-[a-zA-Z0-9_-]{16,})')
NOTICE_PREFIX = '🧠 Memory review: '
NOTICE_LABELS = {'superseded': 'superseded', 'contradicted': 'outdated', 'duplicate': 'duplicate', 'fragment': 'no context', 'transient': 'one-off'}


def notice(items):
    """Total first, then one count per reason, most frequent first; no memory text."""
    counts = Counter(NOTICE_LABELS[kind] for kind, _ in items)
    breakdown = ' · '.join(f'{n} {label}' for label, n in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))
    return f'{NOTICE_PREFIX}{len(items)} invalidated ({breakdown})'


def patch_provider():
    """Wrap every loaded Hindsight provider class so prefetch and the recall tool record ids per session.

    A reload can load the provider again as a new class while live sessions keep instances of the
    old one, so every loaded copy is (re-)wrapped from its original methods.
    """
    import plugins.memory as memory_plugins
    from plugins.memory import import_provider_module
    _wrap_loader(memory_plugins)
    module = import_provider_module('hindsight')
    classes = {getattr(module, 'HindsightMemoryProvider', None) if module else None}
    classes |= {getattr(mod, 'HindsightMemoryProvider', None) for mod in list(sys.modules.values())
                if isinstance(getattr(mod, 'HindsightMemoryProvider', None), type)}
    classes.discard(None)
    for cls in classes:
        _patch(cls)
    return bool(classes)


def _wrap_loader(memory_plugins):
    """Wrap every Hindsight provider class Hermes loads later (a new agent can import a fresh copy)."""
    current = getattr(memory_plugins, '_load_package', None)
    if current is None:
        return
    original = vars(memory_plugins).setdefault('_hindsight_reviewer_original_load', current)

    def load(provider_dir, name):
        module = original(provider_dir, name)
        cls = getattr(module, 'HindsightMemoryProvider', None) if module is not None else None
        if isinstance(cls, type):
            try:
                _patch(cls)
            except Exception:
                logger.warning('Hindsight review could not wrap a newly loaded provider', exc_info=True)
        return module
    memory_plugins._load_package = load


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


def _route_key(source):
    value = source.get if isinstance(source, dict) else lambda name, default='': getattr(source, name, default)
    platform = value('platform', '')
    return (str(getattr(platform, 'value', platform) or ''), str(value('chat_id', '') or ''),
            str(value('thread_id', '') or ''), str(value('profile', '') or ''))


def _unregister_job(job):
    event, route = job.get('cancel_event'), job.get('route_key')
    if event is None or route is None:
        return
    with _lock:
        reviews = _active_reviews.get(route)
        if reviews is not None:
            reviews.pop(event, None)
            if not reviews:
                _active_reviews.pop(route, None)


async def on_pre_gateway_dispatch(event=None, gateway=None, **_):
    """Remember the gateway and its loop so the background thread can post the notice."""
    if gateway is not None:
        _gateway['runner'], _gateway['loop'] = gateway, asyncio.get_running_loop()


def on_post_gateway_admission(session_key='', source=None, message_id=None, **_):
    """Cancel unfinished writes after a newer authorized message enters the same session."""
    if not session_key or source is None:
        return
    route = _route_key(source)
    with _lock:
        if message_id:
            _latest_admissions[route] = str(message_id)
        for job in _active_reviews.get(route, {}).values():
            job['cancel_event'].set()
            _audit(job, event='superseded', reason='new_admitted_message')


def _origin():
    from gateway.session_context import get_session_env
    origin = {name: get_session_env('HERMES_SESSION_' + name.upper(), '') for name in
              ('platform', 'chat_id', 'chat_type', 'thread_id', 'user_id', 'message_id', 'profile')}
    if origin['platform'] == 'local':
        from .desktop import turn_sender
        return origin if turn_sender(_gateway.get('runner')) else None
    return origin if origin['platform'] and origin['chat_id'] else None


def _origin_key(origin):
    return tuple(str(origin.get(name) or '') for name in ('platform', 'chat_id', 'thread_id', 'profile'))


def _turn_review_callback():
    """This turn's background-review sender, so the notice follows the skills review's delivery rules.

    It is held until the reply lands and dropped if a newer message superseded the turn.
    """
    try:
        from gateway.session_context import bound_session_key
        from gateway.session_context import get_session_env
        if get_session_env('HERMES_SESSION_PLATFORM', '') == 'local':
            from .desktop import turn_sender
            return turn_sender(_gateway.get('runner'))
        key = bound_session_key() or ''
        if ':dm:' not in key:
            return None  # notices only in private chats: in a group other bots would read them as messages
        agents = getattr(_gateway.get('runner'), '_running_agents', None) or {}
        return getattr(agents.get(key), 'background_review_callback', None)
    except Exception:
        return None


def on_post_llm_call(session_id='', turn_id='', user_message=None, assistant_response=None,
                     conversation_history=None, **_):
    """Start one review for a completed turn with a known injected block.

    A mid-turn steering message adds a newer user row, so bind against every user row of the turn.
    """
    with _lock:
        injected = _recalls.pop(session_id, None)
        tooled = _tool_recalls.pop(session_id, None)
    marker = {'session_id': session_id, 'turn_id': turn_id}
    job = None
    try:
        origin = _origin()
        if origin is None:
            _audit(marker, event='skipped', reason='no_origin')
            return
        if not (injected or tooled):
            _audit(marker, event='skipped', reason='no_captured_memories')
            return
        if not isinstance(user_message, str) or not isinstance(assistant_response, str):
            _audit(marker, event='skipped', reason='incomplete_turn')
            return
        injected_count = len(injected['records']) if injected else 0
        tool_count = len(tooled['records']) if tooled else 0
        rows = conversation_history or []
        if injected:
            from agent.memory_context import build_memory_context_block
            expected = user_message + '\n\n' + build_memory_context_block(injected['block'])
            if not any(isinstance(row, dict) and row.get('role') == 'user' and isinstance(row.get('api_content'), str)
                       and row['api_content'].startswith(expected) for row in rows):
                _audit(marker, event='binding_mismatch', injected=injected_count)
                injected = None
        recall = _merge(injected, tooled)
        if recall is None:
            _audit(marker, event='skipped', reason='unbound_or_mixed_banks', injected=injected_count, tool_recalled=tool_count)
            return
        from hermes_constants import get_hermes_home
        transcript, quotable = conversation_context(rows)
        job = {'home': str(get_hermes_home()), 'session_id': session_id, 'turn_id': turn_id,
               'transcript': transcript, 'quotable': quotable, 'recall': recall, 'origin': origin,
               'deliver': _turn_review_callback(), 'defer_notice': True,
               'injected_count': injected_count, 'tool_count': tool_count}
        job['route_key'], job['cancel_event'] = _origin_key(origin), threading.Event()
        with _lock:
            latest = _latest_admissions.get(job['route_key'])
            if latest and origin['message_id'] and latest != str(origin['message_id']):
                job['cancel_event'].set()
                _audit(job, event='superseded', reason='newer_message_already_admitted')
            elif latest == str(origin['message_id']):
                _latest_admissions.pop(job['route_key'], None)
            _active_reviews.setdefault(job['route_key'], {})[job['cancel_event']] = job
        import contextvars
        copied = contextvars.copy_context()
        threading.Thread(target=lambda: copied.run(_run, job), name='hindsight-reviewer', daemon=True).start()
    except Exception:
        if job is not None:
            _unregister_job(job)
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
        _unregister_job(job)


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


def _audit(job, **fields):
    """Only correlation ids, decisions and counters; never memory, evidence or exception text."""
    logger.info('Hindsight review audit: %s', json.dumps({
        'version': '0.12.0', 'session_id': job.get('session_id', ''), 'turn_id': job.get('turn_id', ''),
        **fields}, sort_keys=True))


def _superseded(job):
    event = job.get('cancel_event')
    return event is not None and event.is_set()


def _get(base, key, memory_id, stats):
    """An expected stale record is local; infrastructure and permission failures propagate."""
    try:
        if str(uuid.UUID(memory_id)) != memory_id:
            raise ValueError('Noncanonical id')
    except (ValueError, TypeError, AttributeError):
        stats['malformed_ids'] += 1
        return None
    try:
        return _http('GET', base + memory_id, key)
    except urllib.error.HTTPError as error:
        if error.code not in {400, 404, 409}:
            raise
        stats['stale_records'] += 1
        return None


def _sources(base, key, records, stats=None):
    """Resolve eligible primary facts without letting one stale recall abort its siblings."""
    stats = stats if stats is not None else Counter()
    counts = Counter(record['text'] for record in records)
    sources = {}
    for record in records:
        if counts[record['text']] != 1:
            stats['ambiguous_text'] += 1
            continue
        detail = _get(base, key, record['id'], stats)
        if detail is None:
            continue
        if detail.get('state') != 'valid' or detail.get('text') != record['text']:
            stats['changed_recalls'] += 1
            continue
        ids = detail.get('source_memory_ids') if detail.get('type') == 'observation' else [record['id']]
        if not isinstance(ids, list) or not ids or len(ids) > 50:
            stats['unresolved_recalls'] += 1
            continue
        for source in ids:
            if not isinstance(source, str):
                stats['malformed_ids'] += 1
                continue
            if source in sources:
                sources[source]['recall_ids'].append(record['id'])
                continue
            fact = detail if source == record['id'] else _get(base, key, source, stats)
            if fact is None:
                continue
            text = fact.get('text')
            if (fact.get('state') != 'valid' or (fact.get('type') or fact.get('fact_type')) == 'observation'
                    or not isinstance(text, str) or not text or len(text) > 5000 or _SECRET.search(text)):
                stats['ineligible_sources'] += 1
                continue
            date = fact.get('mentioned_at') or fact.get('date') or ''
            sources[source] = {'text': text, 'date': date if isinstance(date, str) else '', 'recall_ids': [record['id']]}
    return sources


def _invalidate(base, key, source_id, text, reason, kept=None, stats=None, job=None):
    """Serialize this process's writers; the stock API still cannot exclude external edits."""
    stats = stats if stats is not None else Counter()
    if _superseded(job or {}):
        return 'superseded'
    with _write_lock:
        if _superseded(job or {}):
            return 'superseded'
        if kept:
            survivor = _get(base, key, kept['id'], stats)
            if survivor is None or survivor.get('state') != 'valid' or survivor.get('text') != kept['text']:
                return 'conflict'
        detail = _get(base, key, source_id, stats)
        if detail is None or detail.get('state') != 'valid' or detail.get('text') != text:
            return 'conflict'
        stats['write_attempts'] += 1
        stats['unverified_writes'] += 1
        if _superseded(job or {}):
            stats['write_attempts'] -= 1
            stats['unverified_writes'] -= 1
            return 'superseded'
        try:
            _http('PATCH', base + source_id, key, {'state': 'invalidated', 'reason': reason})
        except urllib.error.HTTPError as error:
            if error.code in {400, 404, 409}:
                stats['unverified_writes'] -= 1
                return 'conflict'
            raise
        detail = _http('GET', base + source_id, key)
        if detail.get('state') != 'invalidated' or detail.get('invalidation_reason') != reason:
            raise RuntimeError('Invalidation read-back failed')
        stats['unverified_writes'] -= 1
        return 'applied'


MAX_PARALLEL = 8


def review(llm, job):
    """Finalize every admitted job, including changes confirmed before a later failure."""
    started = time.monotonic()
    outcomes, sources, route = Counter(), {}, {}
    applied, cases = [], []
    status, notice_state = 'complete', 'not_needed'
    _audit(job, event='started', bank=job['recall']['bank'], captured=len(job['recall']['records']),
           injected=job.get('injected_count', 0), tool_recalled=job.get('tool_count', 0))
    try:
        if _superseded(job):
            status = 'partial'
            outcomes['superseded_cases'] += len(job['recall']['records'])
            return 0
        url, key = _connection(job['home'])
        base = url + '/v1/default/banks/' + urllib.parse.quote(job['recall']['bank'], safe='') + '/memories/'
        sources = _sources(base, key, job['recall']['records'], outcomes)
        evidence = job['transcript']
        route = cron_route(job['home'])
        aliases = ({f'm{i}': sid for i, sid in enumerate(sources, 1)} if len(sources) >= 20
                   else {sid: sid for sid in sources})
        cases = [{'id': alias, 'memory': sources[sid]['text'], 'date': sources[sid].get('date', '')[:10]}
                 for alias, sid in aliases.items()]
        batches = [cases[offset:offset + 20] for offset in range(0, len(cases), 20)]
        decisions, noisy = [], []
        review_stats, noise_stats = [], []
        if batches:
            from concurrent.futures import ThreadPoolExecutor
            import contextvars
            with ThreadPoolExecutor(max_workers=min(2 * len(batches), MAX_PARALLEL)) as pool:
                submit = lambda call, *args: pool.submit(contextvars.copy_context().run, call, *args)
                review_stats, noise_stats = [Counter() for _ in batches], [Counter() for _ in batches]
                refuted = [submit(policy.propose, llm, batch, route, evidence, stats)
                           for batch, stats in zip(batches, review_stats)]
                flagged = [submit(policy.noise, llm, batch, route, cases, stats)
                           for batch, stats in zip(batches, noise_stats)]
                for name, futures, sink, counters in (('review', refuted, decisions, review_stats),
                                                       ('noise', flagged, noisy, noise_stats)):
                    for batch, future, stats in zip(batches, futures, counters):
                        try:
                            sink.extend(future.result())
                        except Exception as error:
                            outcomes['failed_batches'] += 1
                            outcomes[name + '_failed_cases'] += len(batch)
                            logger.warning('Hindsight %s batch of %d memories skipped (%s)', name, len(batch), type(error).__name__)
                        outcomes.update({name + '_' + k: v for k, v in stats.items() if isinstance(v, int)})
            for stats in noise_stats:
                for row in getattr(stats, 'row_results', []):
                    sid = aliases.get(row['id'])
                    if sid:
                        kept_id = aliases.get(row.get('kept_id', ''), '')
                        _audit(job, event='decision', memory_id=sid, lane='noise', decision=row['decision'],
                               reason=row.get('reason', ''), kept_id=kept_id)
        for index, row in enumerate(decisions):
            if _superseded(job):
                outcomes['superseded_cases'] += len(decisions) - index
                break
            sid = aliases[row['id']]
            _audit(job, event='decision', memory_id=sid, lane='review', decision=row['decision'], category=row['category'])
            if row['decision'] != 'candidate':
                continue
            if not _quotable(job, row['evidence_quote']) or _SECRET.search(row['evidence_quote']):
                outcomes['unquotable'] += 1
                continue
            text = sources[sid]['text']
            try:
                confirmed = policy.confirm(llm, text, row['evidence_quote'], evidence, route)
            except Exception as error:
                outcomes['confirmation_errors'] += 1
                _audit(job, event='confirmation_failed', memory_id=sid, lane='review', error_type=type(error).__name__)
                continue
            if _superseded(job):
                outcomes['superseded_cases'] += 1
                continue
            _audit(job, event='confirmation', memory_id=sid, lane='review', decision='invalidate' if confirmed else 'keep')
            if not confirmed:
                outcomes['confirmation_kept'] += 1
                continue
            event = uuid.uuid5(uuid.NAMESPACE_URL, 'hindsight-review:' + job['session_id'] + ':' + job['turn_id'] + ':' + sid)
            reason = 'hindsight-review:' + str(event)
            _audit(job, event='write_attempt', memory_id=sid, reason=reason)
            outcome = _invalidate(base, key, sid, text, reason, stats=outcomes, job=job)
            outcomes[outcome] += 1
            outcomes['superseded_cases'] += outcome == 'superseded'
            if outcome == 'applied':
                applied.append(('contradicted', text))
            logger.info('Hindsight review %s memory %s (contradicted)', outcome, sid)
        refuted_ids = {row['id'] for row in decisions if row['decision'] == 'candidate'}
        survivors = policy.survivors(noisy)
        for index, row in enumerate(survivors):
            if _superseded(job):
                outcomes['superseded_cases'] += len(survivors) - index
                break
            if row['id'] in refuted_ids or row['duplicate_of'] in refuted_ids:
                outcomes['noise_deferred'] += 1
                continue
            sid = aliases[row['id']]
            text = sources[sid]['text']
            kept_id = aliases[row['duplicate_of']] if row['duplicate_of'] else ''
            kept = sources[kept_id]['text'] if kept_id else ''
            _audit(job, event='decision', memory_id=sid, lane='noise', decision=row['reason'], kept_id=kept_id)
            try:
                confirmed = policy.confirm_noise(llm, text, row['reason'], kept, route)
            except Exception as error:
                outcomes['confirmation_errors'] += 1
                _audit(job, event='confirmation_failed', memory_id=sid, lane='noise', error_type=type(error).__name__)
                continue
            if _superseded(job):
                outcomes['superseded_cases'] += 1
                continue
            _audit(job, event='confirmation', memory_id=sid, lane='noise', decision='invalidate' if confirmed else 'keep', kept_id=kept_id)
            if not confirmed:
                outcomes['confirmation_kept'] += 1
                continue
            event = uuid.uuid5(uuid.NAMESPACE_URL, 'hindsight-noise:' + job['session_id'] + ':' + job['turn_id'] + ':' + sid)
            reason = 'hindsight-noise:' + row['reason'] + ':' + str(event)
            _audit(job, event='write_attempt', memory_id=sid, reason=reason, kept_id=kept_id)
            survivor = {'id': kept_id, 'text': kept} if kept_id else None
            outcome = _invalidate(base, key, sid, text, reason, survivor, outcomes, job)
            outcomes[outcome] += 1
            outcomes['superseded_cases'] += outcome == 'superseded'
            outcomes['noise'] += outcome == 'applied'
            if outcome == 'applied':
                applied.append((row['reason'], text))
            logger.info('Hindsight review %s memory %s (noise: %s)', outcome, sid, row['reason'])
    except Exception as error:
        status = 'partial' if applied else 'failed'
        outcomes['job_errors'] += 1
        _audit(job, event='failed', error_type=type(error).__name__, http_status=getattr(error, 'code', None))
        logger.warning('Hindsight review interrupted (%s); confirmed changes are preserved in its outcome', type(error).__name__)
        raise
    finally:
        if status == 'complete' and any(v for k, v in outcomes.items() if k.endswith(('errors', 'failed_cases', 'missing_rows', 'rejected_rows')) or k in {'stale_records', 'malformed_ids', 'changed_recalls', 'unresolved_recalls', 'unverified_writes',
                                                       'ambiguous_text', 'ineligible_sources', 'conflict', 'unquotable', 'noise_deferred', 'superseded_cases'}):
            status = 'partial'
        logger.info('Hindsight review outcome: reviewed=%d applied=%d noise=%d conflict=%d failed_batches=%d status=%s',
                    len(cases), outcomes['applied'], outcomes['noise'], outcomes['conflict'], outcomes['failed_batches'], status)
        if applied or outcomes['unverified_writes']:
            message = notice(applied) if applied else '🧠 Memory review: write outcome unknown; manual check needed'
            if outcomes['unverified_writes']:
                message += f" · {outcomes['unverified_writes']} write outcome(s) unknown"
            if status != 'complete':
                message += ' · review incomplete'
            try:
                if _superseded(job):
                    notice_state = 'suppressed_superseded'
                    _audit(job, event='notice_suppressed', reason='new_inbound_message')
                elif job.get('deliver'):
                    job['deliver'](message)
                    notice_state = 'handed_off'
                    logger.info('Hindsight review notice handed to the turn background-review sender')
                elif job.get('defer_notice') or job['origin'].get('chat_type') not in {None, '', 'dm', 'private'}:
                    notice_state = 'suppressed_unbound_or_group'
                else:
                    notify(job['origin'], message)
                    notice_state = 'delivered'
            except Exception as error:
                notice_state = 'failed'
                _audit(job, event='notice_failed', error_type=type(error).__name__)
        _audit(job, event='finished', status=status, counts=dict(outcomes), reviewed=len(cases),
               resolved=len(sources), notice_state=notice_state, route=route,
               elapsed_ms=round((time.monotonic() - started) * 1000))
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
