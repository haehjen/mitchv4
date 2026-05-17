import os
import re
import time
import json
from pathlib import Path
from urllib.parse import urlencode

import requests

from core.intent_registry import IntentRegistry
from core.system_log import get_logger
from core.config import MITCH_ROOT

logger = get_logger("youtube_music_skill")

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
PREFS_PATH = Path(MITCH_ROOT) / "data" / "music_preferences.json"
PREF_MAX_RECENT = 80
STOP_WORDS = {
    "play", "song", "music", "youtube", "for", "the", "and", "with", "from",
    "something", "mitch", "echo", "please", "can", "you", "on", "my", "me",
}

_bus = None


def _load_prefs() -> dict:
    if not PREFS_PATH.exists():
        return {"queries": {}, "artists": {}, "recent": []}
    try:
        data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"queries": {}, "artists": {}, "recent": []}
    if not isinstance(data, dict):
        return {"queries": {}, "artists": {}, "recent": []}
    data.setdefault("queries", {})
    data.setdefault("artists", {})
    data.setdefault("recent", [])
    return data


def _save_prefs(data: dict):
    try:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to save music preferences: {e}")


def _record_preference(query: str, title: str, channel: str):
    q = str(query or "").strip().lower()
    c = str(channel or "").strip().lower()
    if not q and not c:
        return
    prefs = _load_prefs()
    if q:
        prefs["queries"][q] = int(prefs["queries"].get(q, 0)) + 1
    if c:
        prefs["artists"][c] = int(prefs["artists"].get(c, 0)) + 1
    prefs["recent"].append(
        {
            "ts": int(time.time()),
            "query": str(query or ""),
            "title": str(title or ""),
            "channel": str(channel or ""),
        }
    )
    prefs["recent"] = prefs["recent"][-PREF_MAX_RECENT:]
    _save_prefs(prefs)


def _build_preference_query() -> str:
    prefs = _load_prefs()
    artists = prefs.get("artists", {})
    queries = prefs.get("queries", {})
    top_artist = ""
    top_query = ""
    if isinstance(artists, dict) and artists:
        top_artist = max(artists.items(), key=lambda x: int(x[1]))[0]
    if isinstance(queries, dict) and queries:
        top_query = max(queries.items(), key=lambda x: int(x[1]))[0]

    if top_artist:
        return f"{top_artist} best tracks"
    if top_query:
        return top_query
    return "popular music mix"


def _new_token() -> str:
    return f"music_{int(time.time() * 1_000_000)}"


def _emit_tool_result(action: str, message: str, **details):
    if not _bus:
        return
    _bus.emit(
        "EMIT_TOOL_RESULT",
        {
            "tool_call_id": None,
            "function_name": "youtube_music_control",
            "output": {
                "action": action,
                "message": message,
                **details,
            },
        },
    )


def _emit_music_command(action: str, **kwargs):
    if not _bus:
        return
    payload = {"action": action}
    payload.update(kwargs)
    _bus.emit("EMIT_MUSIC_COMMAND", payload)


def _extract_query(text: str) -> str:
    t = str(text or "").strip()
    t = re.sub(r"^[^a-zA-Z0-9]*(echo[:,\s-]*)?", "", t, flags=re.IGNORECASE)

    m = re.search(
        r"\b(?:play|put on|start)\s+(?:a\s+|my\s+)?(?:song\s+|music\s+|playlist\s+)?(.+)$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        q = m.group(1).strip()
        q = re.sub(r"\s+on\s+youtube(?:\s+music)?$", "", q, flags=re.IGNORECASE).strip()
        q = re.sub(r"^something\b", "", q, flags=re.IGNORECASE).strip()
        return q
    m = re.search(
        r"\b(?:play\s+)?playlist\s+(.+)$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        q = m.group(1).strip()
        q = re.sub(r"\s+on\s+youtube(?:\s+music)?$", "", q, flags=re.IGNORECASE).strip()
        return q
    m = re.search(
        r"\b(?:choose|pick)\s+(?:a\s+)?song(?:\s+for\s+me)?\s+(?:from|on)\s+youtube(?:\s+music)?(?:\s*[:\-]?\s*(.+))?$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        tail = (m.group(1) or "").strip()
        return tail or "popular music mix"
    return ""


def _search_youtube_video(query: str):
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is not configured.")

    params = {
        "part": "snippet",
        "type": "video",
        "maxResults": 5,
        "q": query,
        "key": YOUTUBE_API_KEY,
        "videoEmbeddable": "true",
        "safeSearch": "none",
    }
    url = f"{YOUTUBE_SEARCH_URL}?{urlencode(params)}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json() if r.content else {}
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        return None

    top = items[0] if isinstance(items[0], dict) else {}
    vid = ((top.get("id") or {}).get("videoId") or "").strip()
    snippet = top.get("snippet") or {}
    title = str(snippet.get("title") or "").strip()
    channel = str(snippet.get("channelTitle") or "").strip()
    if not vid:
        return None
    queue = []
    for item in items[1:]:
        if not isinstance(item, dict):
            continue
        sid = ((item.get("id") or {}).get("videoId") or "").strip()
        ss = item.get("snippet") or {}
        st = str(ss.get("title") or "").strip()
        if sid:
            queue.append({"video_id": sid, "title": st})
    return {"video_id": vid, "title": title, "channel": channel, "queue": queue}


def _friendly_search_error(exc: Exception) -> str:
    s = str(exc or "")
    sl = s.lower()
    if "nameresolutionerror" in sl or "failed to resolve" in sl or "temporary failure in name resolution" in sl:
        return "I can't reach Google right now. DNS or outbound network seems blocked."
    if "connection refused" in sl or "max retries exceeded" in sl or "read timed out" in sl or "connect timeout" in sl:
        return "I can't reach YouTube right now. Network connectivity looks down."
    if "403" in sl and "youtube" in sl:
        return "YouTube API rejected the request. Check API key restrictions, quota, and that YouTube Data API v3 is enabled."
    if "400" in sl and "api key" in sl:
        return "The YouTube API key looks invalid or malformed."
    return "I couldn't search YouTube right now. Check the API key, API enablement, and network."


def _handle_music_intent(text: str):
    msg = str(text or "").strip()
    low = msg.lower()

    if any(x in low for x in ("pause music", "pause song", "pause player", "pause youtube")):
        _emit_music_command("pause")
        _emit_tool_result("pause", "Paused.")
        return
    if any(x in low for x in ("resume music", "resume song", "resume player", "unpause")):
        _emit_music_command("resume")
        _emit_tool_result("resume", "Resuming.")
        return
    if any(x in low for x in ("stop music", "stop song", "stop player", "stop youtube")):
        _emit_music_command("stop")
        _emit_tool_result("stop", "Stopped.")
        return
    if any(x in low for x in ("next song", "skip song", "skip track", "next track")):
        _emit_music_command("next")
        _emit_tool_result("next", "Skipping.")
        return

    query = _extract_query(msg)
    vague = any(x in low for x in ("play something", "pick something", "choose something", "for my mood", "for the mood"))
    if not query and vague:
        query = _build_preference_query()

    if not query:
        _emit_tool_result("help", "Tell me what to play, for example: play song daft punk harder better faster stronger.")
        return

    try:
        result = _search_youtube_video(query)
    except Exception as e:
        logger.warning(f"YouTube search failed: {e}")
        _emit_tool_result("error", _friendly_search_error(e), query=query)
        return

    if not result:
        _emit_tool_result("not_found", f"I couldn't find a playable result for {query}.", query=query)
        return

    _emit_music_command(
        "play",
        video_id=result["video_id"],
        title=result["title"],
        channel=result["channel"],
        query=query,
        queue=result.get("queue", []),
    )
    title = result["title"] or query
    channel = result["channel"]
    _record_preference(query, title, channel)
    message = f"Playing {title} by {channel}." if channel else f"Playing {title}."
    _emit_tool_result(
        "play",
        message,
        query=query,
        title=title,
        channel=channel,
        video_id=result["video_id"],
        queued=len(result.get("queue", [])),
    )


def start_module(event_bus):
    global _bus
    _bus = event_bus

    IntentRegistry.register_intent(
        "youtube_music_control",
        _handle_music_intent,
        keywords=[
            "play song",
            "play music",
            "play playlist",
            "play something",
            "play for me",
            "play on youtube",
            "play on youtube music",
            "choose song",
            "choose a song",
            "pick song",
            "youtube playlist",
            "playlist on youtube",
            "song from youtube",
            "youtube song",
            "for my mood",
            "pause music",
            "resume music",
            "stop music",
            "next song",
            "skip song",
            "youtube music",
        ],
        patterns=[
            r"^play\s+\S.+$",
            r"^put on\s+\S.+$",
            r"^start\s+\S.+$",
        ],
        objects=[],
        priority=120,
    )

    logger.info("youtube_music_skill started.")
