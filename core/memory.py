import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from core.system_log import get_logger
from core.config import MITCH_ROOT
from core.event_bus import event_bus

logger = get_logger("memory")

MEMORY_LOG = Path(MITCH_ROOT) / "data" / "memory.jsonl"
KNOWLEDGE_BASE = Path(MITCH_ROOT) / "data" / "knowledge.json"
MEMORY_SUMMARIES = Path(MITCH_ROOT) / "data" / "memory_summaries.jsonl"
MEMORY_LOG.parent.mkdir(parents=True, exist_ok=True)

RECENT_CONTEXT_TURNS = int(os.getenv("MITCH_RECENT_CONTEXT_TURNS", "8"))
MEMORY_REVIEW_MODEL = os.getenv("MITCH_MEMORY_REVIEW_MODEL", "gpt-4o-mini")
MEMORY_REVIEW_ENABLED = os.getenv("MITCH_MEMORY_REVIEW_ENABLED", "1").lower() in {"1", "true", "yes"}
MEMORY_SUMMARY_MODEL = os.getenv("MITCH_MEMORY_SUMMARY_MODEL", "gpt-4o-mini")
MEMORY_SUMMARY_CHUNK_TURNS = int(os.getenv("MITCH_MEMORY_SUMMARY_CHUNK_TURNS", "20"))
MEMORY_SUMMARY_KEEP_RECENT_TURNS = int(os.getenv("MITCH_MEMORY_SUMMARY_KEEP_RECENT_TURNS", "12"))

_client = None
_state_lock = threading.Lock()
_last_unpaired_user: dict[str, Any] | None = None


# === Working Memory ===

def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to parse {path.name}: {e}")
        return default


def save_memory(role, content, **meta):
    content = str(content or "").strip()
    if not content:
        return None
    entry = {
        "timestamp": _utcnow(),
        "role": role,
        "content": content,
    }
    if meta:
        entry.update(meta)
    with open(MEMORY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    event_bus.emit("MEMORY_TURN_SAVED", {"role": role, "content": content, **meta})
    logger.debug(f"Saved memory: role={role}, content_length={len(content)}")
    return entry


def recall_recent(n=RECENT_CONTEXT_TURNS, include_roles=False):
    if not MEMORY_LOG.exists():
        logger.info("No memory log found.")
        return []
    with open(MEMORY_LOG, "r", encoding="utf-8") as f:
        lines = f.readlines()[-n:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    result = entries if include_roles else [entry.get("content", "") for entry in entries]
    logger.debug(f"Recalled {len(result)} recent memory items.")
    return result


def _read_memory_entries():
    if not MEMORY_LOG.exists():
        return []
    entries = []
    for line in MEMORY_LOG.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def recall_summaries(limit=3):
    if not MEMORY_SUMMARIES.exists():
        return []
    items = []
    for line in MEMORY_SUMMARIES.read_text(encoding="utf-8").splitlines():
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items[-limit:]


def clear_memory():
    if MEMORY_LOG.exists():
        MEMORY_LOG.unlink()
        logger.info("Cleared memory.jsonl.")


# === Long-Term Knowledge ===

def _knowledge_doc():
    data = _safe_read_json(KNOWLEDGE_BASE, {"facts": []})
    if isinstance(data, list):
        return {"facts": data}
    if not isinstance(data, dict):
        return {"facts": []}
    data.setdefault("facts", [])
    return data


def load_knowledge():
    return _knowledge_doc().get("facts", [])


def _normalize_fact_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _infer_slot(fact: str, tags: list[str] | None = None) -> str | None:
    """
    Backstop for obvious identity slots when the reviewer omits one.
    The model is still the main judge; this only protects the plainest cases.
    """
    normalized = _normalize_fact_text(fact)
    tags = tags or []
    if "identity" in tags and normalized.startswith(("user's name is ", "user name is ")):
        return "user_name"
    return None


def save_knowledge(fact, tags=None, *, confidence=None, reason=None, source=None, slot=None):
    fact = str(fact or "").strip()
    if not fact:
        return None
    tags = [str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()]
    slot = str(slot or "").strip().lower() or _infer_slot(fact, tags)
    data = _knowledge_doc()
    facts = data["facts"]
    normalized = _normalize_fact_text(fact)
    now = _utcnow()

    for entry in facts:
        if _normalize_fact_text(entry.get("fact", "")) == normalized:
            entry["tags"] = sorted(set(entry.get("tags", []) + tags))
            entry["updated_at"] = now
            if confidence is not None:
                entry["confidence"] = confidence
            if reason:
                entry["reason"] = reason
            if source:
                entry["source"] = source
            if slot:
                entry["slot"] = slot
            KNOWLEDGE_BASE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            event_bus.emit("KNOWLEDGE_UPDATED", {"fact": fact, "tags": entry["tags"]})
            return entry

    superseded = []
    if slot:
        retained = []
        for entry in facts:
            if entry.get("slot") == slot and entry.get("fact") != fact:
                entry["superseded_at"] = now
                entry["superseded_by"] = fact
                superseded.append(entry)
                continue
            retained.append(entry)
        facts[:] = retained

    entry = {
        "fact": fact,
        "tags": tags,
        "created_at": now,
        "updated_at": now,
    }
    if confidence is not None:
        entry["confidence"] = confidence
    if reason:
        entry["reason"] = reason
    if source:
        entry["source"] = source
    if slot:
        entry["slot"] = slot
    facts.append(entry)
    KNOWLEDGE_BASE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for old in superseded:
        event_bus.emit(
            "KNOWLEDGE_SUPERSEDED",
            {
                "fact": old.get("fact"),
                "slot": slot,
                "superseded_by": fact,
            },
        )
    event_bus.emit("KNOWLEDGE_PROMOTED", {"fact": fact, "tags": tags, "reason": reason})
    logger.info(f"Saved new knowledge: '{fact}' with tags={tags}")
    return entry


def recall_summary(tags=None, limit=None):
    knowledge = load_knowledge()
    if not isinstance(knowledge, list):
        logger.warning("Knowledge base format was invalid or empty.")
        return []

    result = []
    for entry in knowledge:
        if not isinstance(entry, dict) or "fact" not in entry:
            continue
        entry_tags = entry.get("tags", [])
        if tags and not any(tag in entry_tags for tag in tags):
            continue
        result.append(entry["fact"])
        if limit and len(result) >= limit:
            break
    logger.debug(f"Recalled {len(result)} knowledge items.")
    return result


# === Memory Reasoning ===

def _get_openai_client():
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def _extract_json(text: str):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def _review_prompt(user_text: str, assistant_text: str) -> list[dict[str, str]]:
    existing = recall_summary(limit=20)
    existing_block = "\n".join(f"- {fact}" for fact in existing) or "- none"
    return [
        {
            "role": "system",
            "content": (
                "You are Echo's memory gate. Decide whether a completed chat turn contains durable knowledge worth preserving long term. "
                "Promote only stable information that will still help later: House's preferences, identity, recurring projects, relationships, explicit instructions, "
                "important system truths, durable plans, or repeated interests. Do not promote greetings, transient states, one-off requests, assistant guesses, "
                "or news unless House explicitly wants it remembered.\n\n"
                "Return strict JSON only with this shape: "
                '{"promote": true|false, "facts": [{"fact": "...", "tags": ["user"|"preference"|"project"|"identity"|"system"|"plan"|"interest"], "slot": "optional_stable_slot", "confidence": 0.0, "reason": "..."}], "discard_reason": "..."}. '
                "A turn may produce zero, one, or a few facts. Prefer atomic facts. "
                "Use a stable slot when a fact should replace older facts of the same kind, for example user_name, dog_name, primary_location, preferred_name, or preferred_music_genre. "
                "If the user corrects an earlier fact, reuse that same slot so the new fact supersedes the old one."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Existing durable knowledge:\n{existing_block}\n\n"
                f"User said:\n{user_text}\n\n"
                f"Assistant replied:\n{assistant_text}"
            ),
        },
    ]


def summarize_old_memory_if_needed():
    if not os.getenv("OPENAI_API_KEY"):
        return None
    entries = _read_memory_entries()
    already_summarized = sum(int(item.get("turn_count", 0)) for item in recall_summaries(limit=10_000))
    unsummarized = entries[already_summarized:]
    if len(unsummarized) < MEMORY_SUMMARY_CHUNK_TURNS + MEMORY_SUMMARY_KEEP_RECENT_TURNS:
        return None
    chunk = unsummarized[:MEMORY_SUMMARY_CHUNK_TURNS]
    transcript = "\n".join(f"{e.get('role')}: {e.get('content')}" for e in chunk)
    messages = [
        {
            "role": "system",
            "content": (
                "Summarize this older conversation segment for future continuity. "
                "Keep durable developments, decisions, preferences, corrections, and unfinished threads. "
                "Discard greetings, filler, duplicate tool narration, and transient chatter. "
                "Return a concise plain-text summary, not bullets unless they truly help."
            ),
        },
        {"role": "user", "content": transcript},
    ]
    try:
        response = _get_openai_client().chat.completions.create(
            model=MEMORY_SUMMARY_MODEL,
            messages=messages,
            temperature=0.2,
            stream=False,
        )
        summary = str(response.choices[0].message.content if response.choices else "").strip()
        if not summary:
            return None
        entry = {
            "created_at": _utcnow(),
            "from_timestamp": chunk[0].get("timestamp"),
            "to_timestamp": chunk[-1].get("timestamp"),
            "turn_count": len(chunk),
            "summary": summary,
        }
        with open(MEMORY_SUMMARIES, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        event_bus.emit("MEMORY_SUMMARY_CREATED", entry)
        return entry
    except Exception as e:
        logger.error(f"Memory summarization failed: {e}")
        return None


def review_completed_turn(user_entry: dict[str, Any], assistant_entry: dict[str, Any]):
    if not MEMORY_REVIEW_ENABLED:
        return
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("Skipping memory review: OPENAI_API_KEY is not set.")
        return
    user_text = str(user_entry.get("content", "") or "").strip()
    assistant_text = str(assistant_entry.get("content", "") or "").strip()
    if not user_text or not assistant_text:
        return

    event_bus.emit("MEMORY_REVIEW_REQUEST", {"user": user_text, "assistant": assistant_text})
    try:
        response = _get_openai_client().chat.completions.create(
            model=MEMORY_REVIEW_MODEL,
            messages=_review_prompt(user_text, assistant_text),
            temperature=0.1,
            stream=False,
        )
        content = response.choices[0].message.content if response.choices else ""
        verdict = _extract_json(content)
        promoted = []
        for item in verdict.get("facts", []) if verdict.get("promote") else []:
            if not isinstance(item, dict):
                continue
            fact = str(item.get("fact", "") or "").strip()
            if not fact:
                continue
            entry = save_knowledge(
                fact,
                item.get("tags", []),
                confidence=item.get("confidence"),
                reason=item.get("reason"),
                source="memory_review",
                slot=item.get("slot"),
            )
            if entry:
                promoted.append(fact)
        event_bus.emit(
            "MEMORY_REVIEW_RESULT",
            {
                "promoted": promoted,
                "discard_reason": verdict.get("discard_reason", ""),
            },
        )
        summarize_old_memory_if_needed()
    except Exception as e:
        logger.error(f"Memory review failed: {e}")


# === Context Management ===

def truncate_tail():
    if not MEMORY_LOG.exists():
        return
    lines = MEMORY_LOG.read_text(encoding="utf-8").splitlines()
    if not lines:
        return
    try:
        last = json.loads(lines[-1])
    except json.JSONDecodeError:
        return
    if last.get("role") == "user":
        logger.info("Removing unpaired user prompt from memory.jsonl")
        lines.pop()
        MEMORY_LOG.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")


def clear_temp_context():
    truncate_tail()


def handle_user_turn(data):
    global _last_unpaired_user
    if not isinstance(data, dict):
        return
    text = str(data.get("text", "") or "").strip()
    source = data.get("source", "user")
    if text and source != "assistant":
        entry = save_memory("user", text, source=source, token=data.get("token"))
        with _state_lock:
            _last_unpaired_user = entry


def handle_assistant_turn(data):
    global _last_unpaired_user
    if not isinstance(data, dict) or data.get("type") != "message":
        return
    text = str(data.get("text", "") or "").strip()
    if not text:
        return
    assistant_entry = save_memory("assistant", text, source=data.get("source"), token=data.get("token"))
    # Only ordinary conversational replies should close a user turn for review.
    # Proactive/system messages also arrive as assistant output, but they should
    # not be mistaken for evidence about whatever House last said.
    conversational_sources = {"openai", "openai_minimal_rag"}
    if data.get("source") not in conversational_sources:
        return
    with _state_lock:
        user_entry = _last_unpaired_user
        _last_unpaired_user = None
    if user_entry and assistant_entry:
        threading.Thread(
            target=review_completed_turn,
            args=(user_entry, assistant_entry),
            daemon=True,
            name="memory.review_completed_turn",
        ).start()


def start_memory():
    event_bus.subscribe("USER_TURN_ACCEPTED", handle_user_turn)
    event_bus.subscribe("EMIT_ASSISTANT_RESPONSE", handle_assistant_turn)
    logger.info("Memory online: working turns plus long-term knowledge review.")
