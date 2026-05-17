import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.config import MITCH_ROOT
from core.event_bus import event_bus
from core.intent_registry import IntentRegistry
from core.system_log import get_logger

logger = get_logger("pending_updates")

PENDING_UPDATES_PATH = Path(MITCH_ROOT) / "data" / "pending_updates.json"
MAX_ITEMS = 100

_lock = threading.Lock()
_started = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load():
    if not PENDING_UPDATES_PATH.exists():
        return {"items": []}
    try:
        payload = json.loads(PENDING_UPDATES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}
    return payload if isinstance(payload, dict) else {"items": []}


def _save(payload: dict):
    PENDING_UPDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_UPDATES_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _fingerprint(source: str, title: str, summary: str) -> str:
    raw = f"{source}|{title}|{summary}".strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def add_update(source: str, title: str, summary: str, details=None, priority: str = "routine"):
    source = str(source or "unknown").strip()
    title = str(title or "Update").strip()
    summary = str(summary or "").strip()
    if not summary:
        return None

    fingerprint = _fingerprint(source, title, summary)
    with _lock:
        payload = _load()
        items = payload.setdefault("items", [])
        for item in items:
            if item.get("fingerprint") == fingerprint and not item.get("delivered_at"):
                item["last_seen_at"] = _now()
                item["count"] = int(item.get("count", 1)) + 1
                _save(payload)
                return item

        item = {
            "id": f"upd_{fingerprint}",
            "fingerprint": fingerprint,
            "created_at": _now(),
            "last_seen_at": _now(),
            "source": source,
            "priority": str(priority or "routine"),
            "title": title,
            "summary": summary,
            "details": details,
            "count": 1,
            "delivered_at": None,
        }
        items.insert(0, item)
        payload["items"] = items[:MAX_ITEMS]
        _save(payload)
        logger.info(f"Queued pending update from {source}: {title}")
        event_bus.emit("PENDING_UPDATE_QUEUED", item)
        return item


def pending_items():
    with _lock:
        return [x for x in _load().get("items", []) if not x.get("delivered_at")]


def _mark_delivered(items):
    ids = {item.get("id") for item in items}
    with _lock:
        payload = _load()
        delivered_at = _now()
        for item in payload.get("items", []):
            if item.get("id") in ids and not item.get("delivered_at"):
                item["delivered_at"] = delivered_at
        _save(payload)


def _render_digest(items):
    if not items:
        return "No pending updates."
    lines = [f"{len(items)} pending update{'s' if len(items) != 1 else ''}:"]
    for item in items[:8]:
        lines.append(f"- [{item.get('priority', 'routine')}] {item.get('title')}: {item.get('summary')}")
    if len(items) > 8:
        lines.append(f"- And {len(items) - 8} more.")
    return "\n".join(lines)


def deliver_pending(reason: str):
    items = pending_items()
    output = _render_digest(items)
    event_bus.emit(
        "EMIT_TOOL_RESULT",
        {
            "tool_call_id": None,
            "function_name": "pending_updates",
            "output": {
                "reason": reason,
                "count": len(items),
                "summary": output,
            },
        },
    )
    if items:
        _mark_delivered(items)


def handle_pending_update(data):
    if not isinstance(data, dict):
        return
    add_update(
        source=data.get("source", "unknown"),
        title=data.get("title", "Update"),
        summary=data.get("summary", ""),
        details=data.get("details"),
        priority=data.get("priority", "routine"),
    )


def handle_house_returned(_data):
    if pending_items():
        deliver_pending("house_returned")


def _handle_manual_catchup(_text: str):
    deliver_pending("manual_request")


def start_pending_updates():
    global _started
    if _started:
        return
    _started = True
    event_bus.subscribe("QUEUE_PENDING_UPDATE", handle_pending_update)
    event_bus.subscribe("HOUSE_RETURNED", handle_house_returned)
    IntentRegistry.register_intent(
        "pending_updates",
        _handle_manual_catchup,
        keywords=[
            "pending updates",
            "any updates",
            "what did i miss",
            "catch me up",
            "catch up",
        ],
        priority=90,
    )
    logger.info("Pending updates service online.")
