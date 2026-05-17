import os
import threading
import time

from core.event_bus import event_bus
from core.system_log import get_logger

logger = get_logger("presence")

AWAY_AFTER_SECONDS = float(os.getenv("MITCH_PRESENCE_AWAY_AFTER", "45"))
CHECK_INTERVAL_SECONDS = float(os.getenv("MITCH_PRESENCE_CHECK_INTERVAL", "5"))

_lock = threading.Lock()
_last_activity_at = 0.0
_house_present = False
_started = False


def _emit_state(event_name: str, source: str, confidence: float):
    event_bus.emit(
        event_name,
        {
            "house_present": _house_present,
            "source": source,
            "confidence": confidence,
            "last_activity_at": _last_activity_at,
        },
    )


def handle_presence_activity(data):
    global _last_activity_at, _house_present
    data = data if isinstance(data, dict) else {}
    source = str(data.get("source") or "unknown")
    confidence = float(data.get("confidence") or 0.0)
    now = time.time()

    with _lock:
        was_present = _house_present
        _last_activity_at = now
        _house_present = True

    if not was_present:
        logger.info(f"House presence detected via {source}.")
        _emit_state("HOUSE_PRESENT", source, confidence)
        _emit_state("HOUSE_RETURNED", source, confidence)


def handle_user_turn(data):
    source = "user_turn"
    if isinstance(data, dict):
        source = str(data.get("source") or source)
    handle_presence_activity({"source": source, "confidence": 1.0})


def _presence_watchdog():
    global _house_present
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        with _lock:
            should_mark_away = (
                _house_present
                and _last_activity_at
                and (time.time() - _last_activity_at) >= AWAY_AFTER_SECONDS
            )
            if should_mark_away:
                _house_present = False
        if should_mark_away:
            logger.info("House presence decayed to away.")
            _emit_state("HOUSE_AWAY", "timeout", 0.0)


def start_presence():
    global _started
    if _started:
        return
    _started = True
    event_bus.subscribe("PRESENCE_ACTIVITY", handle_presence_activity)
    event_bus.subscribe("USER_TURN_ACCEPTED", handle_user_turn)
    threading.Thread(target=_presence_watchdog, daemon=True, name="presence_watchdog").start()
    logger.info("Presence service online.")


def get_presence_state():
    with _lock:
        return {
            "house_present": _house_present,
            "last_activity_at": _last_activity_at,
        }
