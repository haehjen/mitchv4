import json
from datetime import datetime
from pathlib import Path
from core.system_log import get_logger
from core.config import MITCH_ROOT
from core.event_bus import event_bus

logger = get_logger("memory")

MEMORY_LOG = Path(MITCH_ROOT) / "data" / "memory.jsonl"
KNOWLEDGE_BASE = Path(MITCH_ROOT) / "data" / "knowledge.json"
MEMORY_LOG.parent.mkdir(parents=True, exist_ok=True)

# === Memory ===

def save_memory(role, content):
    content = str(content or "").strip()
    if not content:
        return
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "role": role,
        "content": content
    }
    with open(MEMORY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.debug(f"Saved memory: role={role}, content_length={len(content)}")

def recall_recent(n=5, include_roles=False):
    if not MEMORY_LOG.exists():
        logger.info("No memory log found.")
        return []
    with open(MEMORY_LOG, "r") as f:
        lines = f.readlines()[-n:]
        result = [json.loads(line) for line in lines] if include_roles else [json.loads(line)["content"] for line in lines]
        logger.debug(f"Recalled {len(result)} recent memory items.")
        return result

def clear_memory():
    if MEMORY_LOG.exists():
        MEMORY_LOG.unlink()
        logger.info("Cleared memory.jsonl.")

# === Knowledge ===

def load_knowledge():
    if KNOWLEDGE_BASE.exists():
        with open(KNOWLEDGE_BASE, "r") as f:
            try:
                data = json.load(f)
                if isinstance(data, dict) and "facts" in data:
                    return data["facts"]
                return data  # fallback for legacy format
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse knowledge.json: {e}")
    return []

def save_knowledge(fact, tags=None):
    if tags is None:
        tags = []

    current_data = {}
    if KNOWLEDGE_BASE.exists():
        with open(KNOWLEDGE_BASE, "r") as f:
            try:
                current_data = json.load(f)
            except json.JSONDecodeError:
                current_data = {}

    facts = current_data.get("facts", []) if isinstance(current_data, dict) else current_data
    facts.append({
        "fact": fact,
        "tags": tags,
        "timestamp": datetime.utcnow().isoformat()
    })

    new_data = {"facts": facts}
    with open(KNOWLEDGE_BASE, "w") as f:
        json.dump(new_data, f, indent=2)
    logger.info(f"Saved new knowledge: '{fact}' with tags={tags}")

def recall_summary(tags=None):
    knowledge = load_knowledge()
    if not isinstance(knowledge, list):
        logger.warning("Knowledge base format was invalid or empty.")
        return []

    if tags:
        result = [
            entry["fact"]
            for entry in knowledge
            if isinstance(entry, dict) and "tags" in entry and any(tag in entry["tags"] for tag in tags)
        ]
        logger.debug(f"Recalled {len(result)} tagged knowledge items.")
        return result
    else:
        result = [
            entry["fact"]
            for entry in knowledge
            if isinstance(entry, dict) and "fact" in entry
        ]
        logger.debug(f"Recalled {len(result)} total knowledge items.")
        return result

# === Context Management ===

def truncate_tail():
    if not MEMORY_LOG.exists():
        return
    lines = MEMORY_LOG.read_text().splitlines()
    if not lines:
        return
    last = json.loads(lines[-1])
    if last["role"] == "user":
        logger.info("Removing unpaired user prompt from memory.jsonl")
        lines.pop()
        MEMORY_LOG.write_text("\n".join(lines) + "\n")

def clear_temp_context():
    truncate_tail()


def handle_user_turn(data):
    if not isinstance(data, dict):
        return
    text = str(data.get("text", "") or "").strip()
    source = data.get("source", "user")
    if text and source != "assistant":
        save_memory("user", text)


def handle_assistant_turn(data):
    if not isinstance(data, dict) or data.get("type") != "message":
        return
    text = str(data.get("text", "") or "").strip()
    if text:
        save_memory("assistant", text)


def start_memory():
    event_bus.subscribe("USER_TURN_ACCEPTED", handle_user_turn)
    event_bus.subscribe("EMIT_ASSISTANT_RESPONSE", handle_assistant_turn)
    logger.info("Memory online and listening for conversation events.")
