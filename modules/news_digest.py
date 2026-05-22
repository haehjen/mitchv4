import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI

from core.config import MITCH_ROOT
from core.event_bus import event_bus
from core.intent_registry import IntentRegistry
from core.system_log import get_logger
from modules.web_search import ddgs_news_search

logger = get_logger('news_digest')

TOPICS_PATH = Path(MITCH_ROOT) / 'data' / 'news_topics.json'
DIGEST_PATH = Path(MITCH_ROOT) / 'data' / 'news_digest.json'

REFRESH_SECONDS = int(os.getenv('MITCH_NEWS_REFRESH_SECS', '1800'))
MAX_TOPICS_PER_PULL = int(os.getenv('MITCH_NEWS_TOPICS_PER_PULL', '8'))
MAX_RESULTS_PER_TOPIC = int(os.getenv('MITCH_NEWS_RESULTS_PER_TOPIC', '5'))
TOPIC_PROMOTION_MENTIONS = int(os.getenv('MITCH_NEWS_TOPIC_PROMOTION_MENTIONS', '3'))
OPENAI_MODEL = os.getenv('MITCH_NEWS_MODEL', 'gpt-4o')

DEFAULT_TOPICS = [
    'world news',
    'uk politics',
    'geopolitics',
    'defense',
    'technology',
    'artificial intelligence',
]
STOPWORDS = {
    'the','and','that','this','with','from','have','what','when','where','which','while','there','about',
    'into','your','just','some','thing','things','echo','mitch','house','would','could','should','news',
    'search','google','play','please','more','tell','give','like','want','think','really','currently',
    'using','through','after','before','only','then','than','they','them','were','been','being','will',
}

_lock = threading.Lock()
_client = None
_bus = None
_house_present = False
_last_refresh = 0.0
_started = False


def _now():
    return datetime.now(timezone.utc).isoformat()


def _client_instance():
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def _load_topics():
    if not TOPICS_PATH.exists():
        payload = {
            'active': [{'term': t, 'mentions': 0, 'source': 'baseline'} for t in DEFAULT_TOPICS],
            'candidates': [],
        }
        TOPICS_PATH.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        return payload
    try:
        payload = json.loads(TOPICS_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {'active': [], 'candidates': []}
    if not isinstance(payload, dict):
        return {'active': [], 'candidates': []}
    payload.setdefault('active', [])
    payload.setdefault('candidates', [])
    return payload


def _save_topics(payload):
    TOPICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOPICS_PATH.write_text(json.dumps(payload, indent=2), encoding='utf-8')


def _candidate_terms(text: str):
    words = [w for w in re.findall(r"[a-z][a-z0-9'-]+", str(text or '').lower()) if len(w) >= 4 and w not in STOPWORDS]
    counts = Counter(words)
    return [term for term, _ in counts.most_common(4)]


def _learn_topics_from_text(text: str):
    terms = _candidate_terms(text)
    if not terms:
        return
    with _lock:
        payload = _load_topics()
        active = {str(x.get('term','')).lower(): x for x in payload['active'] if isinstance(x, dict)}
        candidates = {str(x.get('term','')).lower(): x for x in payload['candidates'] if isinstance(x, dict)}
        changed = False
        for term in terms:
            if term in active:
                active[term]['mentions'] = int(active[term].get('mentions', 0)) + 1
                changed = True
                continue
            item = candidates.get(term)
            if item is None:
                item = {'term': term, 'mentions': 0, 'first_seen_at': _now(), 'last_seen_at': _now()}
                payload['candidates'].append(item)
                candidates[term] = item
            item['mentions'] = int(item.get('mentions', 0)) + 1
            item['last_seen_at'] = _now()
            changed = True
            if item['mentions'] >= TOPIC_PROMOTION_MENTIONS:
                payload['candidates'] = [x for x in payload['candidates'] if str(x.get('term','')).lower() != term]
                payload['active'].append({'term': term, 'mentions': item['mentions'], 'source': 'learned', 'promoted_at': _now()})
                event_bus.emit('NEWS_TOPIC_PROMOTED', {'term': term, 'mentions': item['mentions']})
        if changed:
            _save_topics(payload)


def _active_topics():
    payload = _load_topics()
    return [x.get('term') for x in payload.get('active', []) if isinstance(x, dict) and x.get('term')][:MAX_TOPICS_PER_PULL]


def _gather_stories():
    seen = set()
    stories = []
    for topic in _active_topics():
        for result in ddgs_news_search(f'{topic}', max_results=MAX_RESULTS_PER_TOPIC):
            title = str(result.get('title') or '').strip()
            url = str(result.get('url') or '').strip()
            key = (title.lower(), url.lower())
            if not title or key in seen:
                continue
            seen.add(key)
            stories.append({
                'topic': topic,
                'title': title,
                'body': str(result.get('body') or '').strip(),
                'source': str(result.get('source') or 'unknown').strip(),
                'date': str(result.get('date') or '').strip(),
                'url': url,
            })
    return stories


def _editorial_prompt(stories):
    return (
        'You are Echo\'s news editor. Classify supplied news stories into a strict digest.\n'
        'Return valid JSON only with key stories, a list of objects.\n'
        'Each object must include: title, subtitle, bucket, priority, url, why.\n'
        'bucket must be one of: stop_press, store_for_later, generic_digest, discard.\n'
        'priority must be integer 1 to 4 where 1 is extremely rare.\n'
        'Priority 1 / stop_press is ONLY for world-historical or immediate mass-harm events such as nuclear weapon use, declared war, successful government overthrow, major terrorist attack, revolution/civil collapse, or comparable events.\n'
        'Do NOT classify ordinary politics, business, product launches, sports, celebrity news, routine battlefield updates, or merely interesting stories as priority 1.\n'
        'subtitle must be one plain sentence, no more than 18 words.\n'
        'Prefer the most important distinct stories; deduplicate near-identical reports.\n'
        'Keep at most 8 non-discard stories.\n\n'
        f'Stories:\n{json.dumps(stories[:40], ensure_ascii=False)}'
    )


def _editorial_pass(stories):
    if not stories:
        return []
    try:
        response = _client_instance().chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{'role':'user','content':_editorial_prompt(stories)}],
            temperature=0.1,
            response_format={'type':'json_object'},
        )
        payload = json.loads(response.choices[0].message.content or '{}')
        items = payload.get('stories', []) if isinstance(payload, dict) else []
        return [x for x in items if isinstance(x, dict)]
    except Exception as exc:
        logger.warning(f'News editorial pass failed: {exc}')
        return []


def _save_digest(items):
    DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_PATH.write_text(json.dumps({'updated_at': _now(), 'stories': items}, indent=2), encoding='utf-8')


def _headline_brief(items):
    if not items:
        return 'No digest-worthy stories at the moment.'
    lines = []
    for idx, item in enumerate(items[:6], 1):
        lines.append(f"{idx}. {item.get('title','').strip()} — {item.get('subtitle','').strip()}")
    return '\n'.join(lines)


def _queue_or_emit(items):
    present_updates = []
    for item in items:
        bucket = item.get('bucket')
        priority = int(item.get('priority') or 4)
        if bucket == 'discard':
            continue
        summary = f"{item.get('title','').strip()} — {item.get('subtitle','').strip()}"
        details = item
        if bucket == 'stop_press' and priority == 1:
            event_bus.emit('EMIT_TOOL_RESULT', {
                'tool_call_id': None,
                'function_name': 'news_stop_press',
                'output': {'headline': item.get('title'), 'subtitle': item.get('subtitle'), 'url': item.get('url')},
            })
            continue
        if bucket == 'store_for_later':
            if _house_present:
                present_updates.append(item)
                continue
            event_bus.emit('QUEUE_PENDING_UPDATE', {
                'source': 'news_digest',
                'title': item.get('title') or 'News update',
                'summary': item.get('subtitle') or summary,
                'details': details,
                'priority': 'high' if priority <= 2 else 'routine',
            })
    if present_updates:
        # House is present; speak once like an editor, not once per article.
        # Stop-press items are handled above and still interrupt individually.
        present_updates.sort(key=lambda item: int(item.get('priority') or 4))
        event_bus.emit('EMIT_TOOL_RESULT', {
            'tool_call_id': None,
            'function_name': 'news_update_brief',
            'output': {
                'count': len(present_updates),
                'headlines': [
                    {
                        'headline': item.get('title'),
                        'subtitle': item.get('subtitle'),
                        'url': item.get('url'),
                    }
                    for item in present_updates[:4]
                ],
            },
        })


def refresh_news(force=False):
    global _last_refresh
    now = time.time()
    if not force and (now - _last_refresh) < REFRESH_SECONDS:
        return []
    _last_refresh = now
    stories = _gather_stories()
    edited = _editorial_pass(stories)
    kept = [x for x in edited if x.get('bucket') != 'discard']
    _save_digest(kept)
    _queue_or_emit(kept)
    logger.info(f'News refresh complete: gathered={len(stories)} kept={len(kept)}')
    return kept


def _load_digest_items():
    if not DIGEST_PATH.exists():
        return []
    try:
        payload = json.loads(DIGEST_PATH.read_text(encoding='utf-8'))
        return payload.get('stories', []) if isinstance(payload, dict) else []
    except Exception:
        return []


def _handle_digest_request(_text):
    items = _load_digest_items() or refresh_news(force=True)
    event_bus.emit('EMIT_TOOL_RESULT', {
        'tool_call_id': None,
        'function_name': 'news_digest',
        'output': {
            'summary': _headline_brief(items),
            'offer': 'Ask for more context on any headline by number or title.',
        },
    })


def _handle_refresh_request(_text):
    items = refresh_news(force=True)
    event_bus.emit('EMIT_TOOL_RESULT', {
        'tool_call_id': None,
        'function_name': 'news_refresh',
        'output': {'summary': f'Refreshed news digest with {len(items)} digest-worthy stories.'},
    })


def _handle_stop_press_test(_text):
    """
    Deliberate live-system test hook. This bypasses editorial classification
    without weakening the real stop-press threshold.
    """
    item = {
        'title': '[TEST] Synthetic priority one alert',
        'subtitle': 'Synthetic stop-press event injected to verify interruption flow.',
        'bucket': 'stop_press',
        'priority': 1,
        'url': '',
        'why': 'manual test hook',
    }
    event_bus.emit('NEWS_STOP_PRESS_TEST_INJECTED', item)
    _queue_or_emit([item])


def _on_user_turn(data):
    if isinstance(data, dict):
        _learn_topics_from_text(str(data.get('text') or ''))


def _on_house_present(_data):
    global _house_present
    _house_present = True


def _on_house_away(_data):
    global _house_present
    _house_present = False


def _loop():
    while True:
        try:
            refresh_news(force=False)
        except Exception as exc:
            logger.warning(f'News loop failed: {exc}')
        time.sleep(30)


def start_module(bus):
    global _bus, _started
    if _started:
        return
    _started = True
    _bus = bus
    event_bus.subscribe('USER_TURN_ACCEPTED', _on_user_turn)
    event_bus.subscribe('HOUSE_PRESENT', _on_house_present)
    event_bus.subscribe('HOUSE_RETURNED', _on_house_present)
    event_bus.subscribe('HOUSE_AWAY', _on_house_away)
    IntentRegistry.register_intent('news_digest', _handle_digest_request, keywords=['news digest','news rundown','latest headlines'], priority=70)
    IntentRegistry.register_intent('news_refresh', _handle_refresh_request, keywords=['refresh news','update news digest'], priority=70)
    IntentRegistry.register_intent(
        'news_stop_press_test',
        _handle_stop_press_test,
        keywords=['test stop press', 'test priority one news', 'inject test stop press'],
        priority=80,
    )
    threading.Thread(target=_loop, daemon=True, name='news_digest_loop').start()
    logger.info('News digest service online.')
