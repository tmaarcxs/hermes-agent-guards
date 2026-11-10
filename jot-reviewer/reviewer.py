"""Background review of the weekly Jot board after each gateway turn.

Mirrors hindsight-reviewer: a daemon thread after ``post_llm_call`` replays the turn with the
background review's context, the cron model proposes board changes with an exact quote, a
deterministic gate and a skeptical second call must both pass, and each change is applied with the
weekly-board scripts (fresh read, revision check, isolation check), then read back. One short notice
goes through the turn's background-review sender.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import types
from collections import Counter
from pathlib import Path

from . import policy

logger = logging.getLogger(__name__)
CTX = None
# Survives plugin reloads like hindsight-reviewer's registry: the gateway handle is set per message.
_state = sys.modules.setdefault('hermes_jot_reviewer_state', types.ModuleType('hermes_jot_reviewer_state'))
_gateway = _state.__dict__.setdefault('gateway', {})
MAX_CONTEXT_CHARS = 60000
MAX_EARLIER_CHARS = 6000
MAX_DAY_CHARS = 12000
HUMAN_SOURCES = ('telegram', 'cli', 'desktop', 'tui')
_SYNTHETIC = ('[IMPORTANT', '[ASYNC', '[Note', '[System', '[CONTEXT COMPACTION', '[Cron delivery')
_ORIGIN_BLOCK = re.compile(r'^Gateway message origin.*?\n\n', re.S)
_review_lock = _state.__dict__.setdefault('review_lock', threading.Lock())
DAYS = ('Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes')
NOTICE_PREFIX = '📝 Jot review: '
NOTICE_LABELS = {'add': 'added', 'set': 'updated', 'remove': 'removed'}
SCRIPTS = Path(__file__).resolve().parent / 'scripts'
_MEMORY_SPAN = re.compile(r'<memory-context>.*?</memory-context>', re.S)
JOT_WRITES = frozenset({'jot_update', 'jot_create', 'jot_set_task', 'jot_delete'})


def notice(applied):
    counts = Counter(NOTICE_LABELS[change['op']] for change in applied)
    total = len(applied)
    breakdown = ' · '.join(f'{n} {label}' for label, n in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))
    return f"{NOTICE_PREFIX}{total} change{'s' if total != 1 else ''} ({breakdown})"


async def on_pre_gateway_dispatch(gateway=None, **_):
    if gateway is not None:
        _gateway['runner'], _gateway['loop'] = gateway, asyncio.get_running_loop()


def _origin():
    from gateway.session_context import get_session_env
    origin = {name: get_session_env('HERMES_SESSION_' + name.upper(), '') for name in ('platform', 'chat_id')}
    return origin if origin['platform'] and origin['chat_id'] and origin['platform'] != 'local' else None


def _turn_review_callback():
    """This turn's background-review sender: held until the reply lands, dropped if superseded."""
    try:
        from gateway.session_context import bound_session_key
        key = bound_session_key() or ''
        if ':dm:' not in key:
            return None  # notices only in private chats: in a group other bots would read them as messages
        agents = getattr(_gateway.get('runner'), '_running_agents', None) or {}
        return getattr(agents.get(key), 'background_review_callback', None)
    except Exception:
        return None


def _unattended():
    """True in cron, Kanban and delegated-worker turns: only Marcos's own chat turns review the board."""
    from gateway.session_context import get_session_env
    truthy = lambda name: str(get_session_env(name, '') or os.environ.get(name, '')).strip().lower() in {'1', 'true', 'yes', 'on'}
    return truthy('HERMES_CRON_SESSION') or truthy('HERMES_KANBAN_TASK') or truthy('HERMES_DELEGATED_CHILD_CONTEXT')


def _jot_available():
    try:
        from tools.registry import registry
        return registry.get_entry('jot_read') is not None
    except Exception:
        return False


def on_post_llm_call(session_id='', turn_id='', user_message=None, assistant_response=None,
                     conversation_history=None, **_):
    """Start one background Jot review for a completed gateway turn."""
    try:
        if not isinstance(user_message, str) or not isinstance(assistant_response, str) or _origin() is None:
            return
        if _unattended():
            return
        if not _jot_available():
            return
        from hermes_constants import get_hermes_home
        earlier, turn = split_turn(conversation_history or [])
        job = {'home': str(get_hermes_home()), 'session_id': session_id, 'turn_id': turn_id, 'turn': turn,
               'earlier': earlier, 'user_message': user_message, 'transcript': with_earlier(earlier, turn),
               'deliver': _turn_review_callback()}
        import contextvars
        copied = contextvars.copy_context()
        threading.Thread(target=lambda: copied.run(_run, job), name='jot-reviewer', daemon=True).start()
    except Exception:
        logger.warning('Jot review could not start; the turn is skipped', exc_info=True)


def _text(message):
    content = message.get('content')
    if isinstance(content, list):
        content = ' '.join(block.get('text', '') for block in content if isinstance(block, dict))
    return _MEMORY_SPAN.sub('', content).strip() if isinstance(content, str) else ''


def split_turn(rows):
    """(earlier digest, current turn) as text: the turn starts at the last user message."""
    rows = [row for row in rows if isinstance(row, dict)]
    start = max((i for i, row in enumerate(rows) if row.get('role') == 'user'), default=0)
    return digest(rows[:start]), conversation_context(rows[start:])


def digest(rows):
    """Background lines of earlier turns: user and assistant text only, newest kept within the cap."""
    lines = []
    for message in rows:
        role, text = message.get('role'), _text(message).replace('\n', ' ')
        if role in ('user', 'assistant') and text:
            lines.append(f"{role.upper()}: {text[:300 if role == 'user' else 200]}")
    return '\n'.join(lines)[-MAX_EARLIER_CHARS:]


def day_messages(home, now=None, current=''):
    """Marcos's own messages of today across all his chat sessions, as 'HH:MM text' lines, oldest first.

    Read-only; any failure returns '' so the review still runs on the turn alone. The turn's own message is left out.
    """
    import sqlite3
    try:
        day = now or datetime.date.today()
        start = datetime.datetime.combine(day, datetime.time.min).timestamp()
        marks = ','.join('?' * len(HUMAN_SOURCES))
        db = sqlite3.connect(f"file:{Path(home) / 'state.db'}?mode=ro", uri=True, timeout=2)
        try:
            rows = db.execute(f"SELECT m.timestamp, m.content FROM messages m JOIN sessions s ON s.id = m.session_id "
                              f"WHERE m.role = 'user' AND s.source IN ({marks}) AND m.timestamp >= ? ORDER BY m.timestamp",
                              (*HUMAN_SOURCES, start)).fetchall()
        finally:
            db.close()
    except Exception:
        return ''
    lines = []
    for stamp, content in rows:
        text = _ORIGIN_BLOCK.sub('', _MEMORY_SPAN.sub('', content or '')).strip().replace('\n', ' ')
        if text and not text.startswith(_SYNTHETIC) and text[:200] != current.strip().replace('\n', ' ')[:200]:
            lines.append(f"{datetime.datetime.fromtimestamp(stamp).strftime('%H:%M')} {text[:200]}")
    return '\n'.join(lines)[-MAX_DAY_CHARS:]


def with_earlier(earlier, turn, day=''):
    """The model's context: today's messages and earlier turns as background, then the turn under review as the only evidence."""
    parts = []
    if day:
        parts.append(f"=== TODAY'S USER MESSAGES, ALL SESSIONS (background, never evidence) ===\n{day}")
    if earlier:
        parts.append(f'=== EARLIER TURNS (background, never evidence) ===\n{earlier}')
    if not parts:
        return turn
    return '\n\n'.join(parts) + f'\n\n=== THIS TURN (evidence) ===\n{turn}'


def conversation_context(rows):
    """One turn's messages as text; assistant text counts as the record of the work done.

    Injected memory blocks are stripped so an old memory never drives a board change.
    """
    lines = []
    for message in rows:
        role, text = message.get('role'), _text(message)
        if role == 'assistant' and message.get('tool_calls'):
            names = [(call.get('function') or {}).get('name', '?') for call in message['tool_calls'] if isinstance(call, dict)]
            lines.append('[ASSISTANT TOOL CALLS] ' + ', '.join(names))
        if text:
            lines.append('[' + str(role).upper() + ']\n' + text)
    return '\n\n'.join(lines)[-MAX_CONTEXT_CHARS:]


def _run(job):
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    token = set_secret_scope(build_profile_secret_scope(Path(job['home'])), profile_home=job['home'])
    try:
        review(CTX.llm, job)
    except Exception as error:
        logger.warning('Jot review failed (%s: %s); the turn is skipped', type(error).__name__, error)
    finally:
        reset_secret_scope(token)


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


def _jot(name, args):
    from tools.registry import registry
    raw = registry.dispatch(name, args)
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict) or 'error' in data:
        raise RuntimeError(f'{name} failed: ' + str(data)[:200])
    return data.get('data', data)


def today_heading(now=None):
    """Heading of today's day block, e.g. 'Lunes 9'; None on weekends."""
    now = now or datetime.date.today()
    return f'{DAYS[now.weekday()]} {now.day}' if now.weekday() < 5 else None


def week_note(today):
    """The weekly note whose day blocks include today (most recently updated first)."""
    listed = _jot('jot_list', {'query': 'Semana', 'limit': 10}).get('notes') or []
    for item in listed:
        note = _jot('jot_read', {'id': item['id']})
        days = policy.parse_note(note.get('markdown') or '')
        if today in days and list(days)[0].startswith('Lunes'):
            return note, days
    return None, None


def _script(home, name, *args):
    """Run one weekly-board script against the installed Jot plugin; True when it wrote."""
    import hermes_constants
    from plugins.plugin_storage import plugin_data_dir
    env = dict(os.environ, JOT_PLUGIN_DIR=str(Path(home) / 'plugins' / 'jot'), JOT_DATA_DIR=str(plugin_data_dir('jot')))
    tsx = shutil.which('tsx') or str(Path(hermes_constants.__file__).resolve().parent / 'node_modules' / '.bin' / 'tsx')
    result = subprocess.run([tsx, str(SCRIPTS / f'{name}.mts'), *args], env=env, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        logger.info('Jot review skipped a change (%s): %s', name, (result.stderr or result.stdout).strip()[-200:])
        return False
    return True


def apply(home, note, days, change):
    day, task = change['day'], change['task']
    if change['op'] == 'add':
        return _script(home, 'append-row', note['id'], day, change['state'], task, change['next'])
    if change['op'] == 'remove':
        return _script(home, 'remove-row', note['id'], day, task)
    row = next(row for row in days[day] if row['task'] == task)
    return _script(home, 'set-rich-row', note['id'], day, task, row['state'], row['next'], change['state'], change['next'], '--write')


def review(llm, job, now=None):
    """Review one turn; reviews of one process run one at a time so each reads the previous one's writes."""
    with _review_lock:
        return _review(llm, job, now)


def _review(llm, job, now):
    today = today_heading(now)
    if today is None:
        return []
    note, days = week_note(today)
    if note is None:
        logger.info('Jot review: no weekly note for %s; the turn is skipped', today)
        return []
    route = cron_route(job['home'])
    evidence = job.get('turn') or job['transcript']
    if 'turn' in job:
        job = {**job, 'transcript': with_earlier(job.get('earlier', ''), job['turn'], day_messages(job['home'], now, job.get('user_message', '')))}
    proposed = policy.propose(llm, note['markdown'], today, job['transcript'], route)
    changes = policy.gate(proposed, days, today, evidence)
    if len(changes) != len(proposed):
        logger.info('Jot review gate dropped %d of %d proposed changes', len(proposed) - len(changes), len(proposed))
    applied = []
    for group in policy.units(changes):
        shown = group[0] if len(group) == 1 else {'op': 'move', 'from': group[0]['day'], 'to': group[1]['day'],
                                                  'task': group[1]['task'], 'state': group[1]['state'],
                                                  'next': group[1]['next'], 'evidence_quote': group[1]['evidence_quote']}
        if not policy.confirm(llm, shown, note['markdown'], today, job['transcript'], route):
            continue
        fresh = _jot('jot_read', {'id': note['id']})
        fresh_days = policy.parse_note(fresh.get('markdown') or '')
        if len(policy.gate(group, fresh_days, today, evidence)) != len(group):
            logger.info('Jot review skipped a change: the note changed since it was read')
            continue
        for change in group:
            if not apply(job['home'], note, fresh_days, change):
                break
            applied.append(change)
            logger.info('Jot review applied %s on %s: %s', change['op'], change['day'], change['task'][:80])
    if applied:
        after = policy.parse_note(_jot('jot_read', {'id': note['id']}).get('markdown') or '')
        missing = [c for c in applied if c['op'] != 'remove' and not any(r['task'] == c['task'] and r['state'] == c['state']
                                                                       for r in after.get(c['day'], []))]
        if missing or list(after)[:5] != list(days)[:5]:
            logger.warning('Jot review readback mismatch: %d changes not visible', len(missing))
    logger.info('Jot review outcome: proposed=%d gated=%d applied=%d', len(proposed), len(changes), len(applied))
    if applied and job.get('deliver'):
        job['deliver'](notice(applied))
    return applied
