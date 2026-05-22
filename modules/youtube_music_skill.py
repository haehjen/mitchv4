import os
import re
import time
import json
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

from core.intent_registry import IntentRegistry
from core.system_log import get_logger
from core.config import MITCH_ROOT

logger = get_logger("youtube_music_skill")

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
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


def _extract_youtube_video_id(text: str) -> str:
    t = str(text or "")
    m = re.search(r"https?://[^\s]+", t, flags=re.IGNORECASE)
    if not m:
        return ""
    raw = m.group(0).strip().rstrip(".,)]\"'")
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path or ""
    if host.endswith("youtu.be"):
        return path.strip("/").split("/", 1)[0][:32]
    if "youtube.com" not in host:
        return ""
    query_video = (parse_qs(parsed.query).get("v") or [""])[0].strip()
    if query_video:
        return query_video[:32]
    m_path = re.search(r"/(?:embed|shorts|live)/([^/?#]+)", path)
    if m_path:
        return m_path.group(1).strip()[:32]
    return ""


def _get_youtube_video(video_id: str):
    vid = str(video_id or "").strip()
    if not vid or not YOUTUBE_API_KEY:
        return {"video_id": vid, "title": "requested YouTube video", "channel": "", "queue": []} if vid else None
    params = {
        "part": "snippet,status",
        "id": vid,
        "key": YOUTUBE_API_KEY,
    }
    url = f"{YOUTUBE_VIDEOS_URL}?{urlencode(params)}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json() if r.content else {}
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        return {"video_id": vid, "title": "requested YouTube video", "channel": "", "queue": []}
    item = items[0] if isinstance(items[0], dict) else {}
    snippet = item.get("snippet") or {}
    status = item.get("status") or {}
    if status and status.get("embeddable") is False:
        return {
            "video_id": vid,
            "title": str(snippet.get("title") or "requested YouTube video").strip(),
            "channel": str(snippet.get("channelTitle") or "").strip(),
            "queue": [],
            "embeddable": False,
        }
    return {
        "video_id": vid,
        "title": str(snippet.get("title") or "requested YouTube video").strip(),
        "channel": str(snippet.get("channelTitle") or "").strip(),
        "queue": [],
        "embeddable": True,
    }


def _clean_query_tail(q: str) -> str:
    q = str(q or "").strip()
    q = re.sub(r"https?://[^\s]+", "", q, flags=re.IGNORECASE).strip()
    q = re.sub(r"\s+on\s+youtube(?:\s+music)?$", "", q, flags=re.IGNORECASE).strip()
    q = re.sub(r"^(?:for|about)\s+", "", q, flags=re.IGNORECASE).strip()
    q = re.sub(r"^something\b", "", q, flags=re.IGNORECASE).strip()
    return q


def _extract_query(text: str) -> str:
    t = str(text or "").strip()
    t = re.sub(r"^[^a-zA-Z0-9]*(echo[:,\s-]*)?", "", t, flags=re.IGNORECASE)

    m = re.search(
        r"\b(?:play|put on|start|queue up)\s+(?:a\s+|my\s+)?(?:song\s+|music\s+|playlist\s+|video\s+)?(.+)$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        return _clean_query_tail(m.group(1))
    m = re.search(
        r"\b(?:play\s+)?playlist\s+(.+)$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        return _clean_query_tail(m.group(1))
    m = re.search(
        r"\b(?:choose|pick)\s+(?:a\s+)?song(?:\s+for\s+me)?\s+(?:from|on)\s+youtube(?:\s+music)?(?:\s*[:\-]?\s*(.+))?$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        tail = _clean_query_tail(m.group(1) or "")
        return tail or "popular music mix"
    m = re.search(
        r"\b(?:find|search(?:\s+for)?|look\s+for|get)\s+(?:me\s+)?(.+?)\s+(?:on|from)\s+youtube(?:\s+music)?\b",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        return _clean_query_tail(m.group(1))
    m = re.search(
        r"\byoutube(?:\s+music)?\s+(?:for\s+)?(.+)$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        return _clean_query_tail(m.group(1))
    return ""


def _looks_live_request(text: str) -> bool:
    low = str(text or "").lower()
    return any(x in low for x in ("live stream", "livestream", "live feed", "live coverage", "live on youtube"))


def _search_youtube_video(query: str, live: bool = False):
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
    if live:
        params["eventType"] = "live"
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
    if any(x in low for x in ("next song", "skip song", "skip track", "next track", "next video", "skip video", "next youtube", "skip youtube")):
        _emit_music_command("next")
        _emit_tool_result("next", "Skipping to the next queued YouTube item.")
        return

    direct_video_id = _extract_youtube_video_id(msg)
    if direct_video_id:
        try:
            result = _get_youtube_video(direct_video_id)
        except Exception as e:
            logger.warning(f"YouTube video lookup failed: {e}")
            result = {"video_id": direct_video_id, "title": "requested YouTube video", "channel": "", "queue": []}
        if result and result.get("embeddable") is False:
            _emit_tool_result(
                "blocked",
                "That YouTube video exists, but it is not embeddable in the Mitch viewer. I left the current player alone.",
                video_id=direct_video_id,
                title=result.get("title", ""),
                channel=result.get("channel", ""),
            )
            return
        title = (result or {}).get("title") or "requested YouTube video"
        channel = (result or {}).get("channel") or ""
        _emit_music_command(
            "play",
            video_id=direct_video_id,
            title=title,
            channel=channel,
            query="direct YouTube URL",
            queue=[],
        )
        _record_preference("direct YouTube URL", title, channel)
        message = f"Playing {title} by {channel}." if channel else f"Playing {title}."
        _emit_tool_result(
            "play",
            message,
            query="direct YouTube URL",
            title=title,
            channel=channel,
            video_id=direct_video_id,
            queued=0,
        )
        return

    query = _extract_query(msg)
    vague = any(x in low for x in ("play something", "pick something", "choose something", "for my mood", "for the mood"))
    if not query and vague:
        query = _build_preference_query()

    if not query:
        _emit_tool_result("help", "Tell me what to play, for example: play song daft punk harder better faster stronger.")
        return

    try:
        result = _search_youtube_video(query, live=_looks_live_request(msg))
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
            "next video",
            "skip video",
            "next youtube",
            "skip youtube",
            "youtube music",
            "find on youtube",
            "search on youtube",
            "look for on youtube",
            "youtube live stream",
            "live stream on youtube",
            "youtube live feed",
        ],
        patterns=[
            r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+",
            r"^play\s+\S.+$",
            r"^put on\s+\S.+$",
            r"^start\s+\S.+$",
            r"\b(?:find|search(?:\s+for)?|look\s+for|get)\s+.+\s+(?:on|from)\s+youtube(?:\s+music)?\b",
            r"\byoutube(?:\s+music)?\s+.+\blive\s+(?:stream|feed)\b",
            r"\blive\s+(?:stream|feed)\s+.+\bon\s+youtube\b",
        ],
        objects=[],
        priority=120,
    )

    logger.info("youtube_music_skill started.")
