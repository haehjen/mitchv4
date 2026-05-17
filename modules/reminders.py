import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import OpenAI

from core.config import MITCH_ROOT
from core.intent_registry import IntentRegistry
from core.system_log import get_logger

logger = get_logger('reminders')
REMINDERS_PATH = Path(MITCH_ROOT) / 'data' / 'reminders.json'
TIMEZONE_NAME = os.getenv('MITCH_TIMEZONE', 'UTC')
CHECK_SECONDS = float(os.getenv('MITCH_REMINDER_CHECK_SECONDS', '15'))
MODEL = os.getenv('MITCH_REMINDER_MODEL', 'gpt-4o-mini')
_bus = None
_client = None
_lock = threading.Lock()


def _tz():
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except Exception:
        return timezone.utc


def _now_local():
    return datetime.now(_tz())


def _load():
    if not REMINDERS_PATH.exists():
        return {'reminders': []}
    try:
        data = json.loads(REMINDERS_PATH.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {'reminders': []}
    except Exception:
        return {'reminders': []}


def _save(data):
    REMINDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    REMINDERS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def _client_obj():
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def _parse(text: str):
    now = _now_local()
    if not os.getenv('OPENAI_API_KEY'):
        return None
    messages = [
        {
            'role': 'system',
            'content': (
                'Extract one reminder from the user text. Return strict JSON only: '
                '{"title":"...","due_at":"ISO-8601 with timezone","needs_clarification":true|false,"clarification":"..."}. '
                'If no usable date/time exists, set needs_clarification true. Preserve the task itself, not filler.'
            ),
        },
        {
            'role': 'user',
            'content': f'Current local datetime: {now.isoformat()}\nTimezone: {TIMEZONE_NAME}\nText: {text}',
        },
    ]
    try:
        r = _client_obj().chat.completions.create(model=MODEL, messages=messages, temperature=0.1)
        raw = r.choices[0].message.content if r.choices else ''
        raw = str(raw or '').strip().strip('`')
        if raw.lower().startswith('json'):
            raw = raw[4:].strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        logger.error(f'Reminder parse failed: {e}')
        return None


def _emit_tool(output):
    _bus.emit('EMIT_TOOL_RESULT', {'tool_call_id': None, 'function_name': 'reminders', 'output': output})


def _create(text: str):
    parsed = _parse(text)
    if not parsed or parsed.get('needs_clarification'):
        _emit_tool({'status': 'needs_time', 'message': parsed.get('clarification') if parsed else 'I could not find a reminder time.'})
        return
    title = str(parsed.get('title') or '').strip()
    due_raw = str(parsed.get('due_at') or '').strip()
    try:
        due = datetime.fromisoformat(due_raw)
        if due.tzinfo is None:
            due = due.replace(tzinfo=_tz())
    except Exception:
        _emit_tool({'status': 'needs_time', 'message': 'I could not parse the reminder time.'})
        return
    item = {
        'id': f'rem_{int(time.time() * 1_000_000)}',
        'title': title,
        'due_at': due.isoformat(),
        'created_at': _now_local().isoformat(),
        'status': 'pending',
    }
    with _lock:
        data = _load()
        data.setdefault('reminders', []).append(item)
        _save(data)
    _bus.emit('REMINDER_CREATED', item)
    _emit_tool({'status': 'created', 'title': title, 'due_at': due.isoformat()})


def _pending():
    return [r for r in _load().get('reminders', []) if r.get('status') == 'pending']


def _list(_text=None):
    items = sorted(_pending(), key=lambda r: r.get('due_at', ''))
    _emit_tool({'status': 'listed', 'count': len(items), 'reminders': items[:10]})


def _watchdog():
    while True:
        time.sleep(CHECK_SECONDS)
        now = _now_local()
        due = []
        with _lock:
            data = _load()
            for item in data.get('reminders', []):
                if item.get('status') != 'pending':
                    continue
                try:
                    when = datetime.fromisoformat(item['due_at'])
                except Exception:
                    continue
                if when <= now:
                    item['status'] = 'fired'
                    item['fired_at'] = now.isoformat()
                    due.append(dict(item))
            if due:
                _save(data)
        for item in due:
            _bus.emit('REMINDER_DUE', item)
            _bus.emit(
                'QUEUE_PENDING_UPDATE',
                {
                    'source': 'reminders',
                    'title': 'Reminder',
                    'summary': item.get('title'),
                    'details': item,
                    'priority': 'high',
                },
            )


def start_module(event_bus):
    global _bus
    _bus = event_bus
    IntentRegistry.register_intent(
        'create_reminder',
        _create,
        keywords=['remind me to', 'set a reminder', 'remind me at', 'remind me on'],
        patterns=[r'\bremind me\b'],
        priority=18,
    )
    IntentRegistry.register_intent(
        'list_reminders',
        _list,
        keywords=['show reminders', 'list reminders', 'what are my reminders', 'upcoming reminders'],
        priority=16,
    )
    threading.Thread(target=_watchdog, daemon=True, name='reminders.watchdog').start()
    logger.info(f'Reminders service online ({TIMEZONE_NAME}).')
