import re
from core.event_bus import event_bus, INNERMONO_PATH
from core.system_log import get_logger
from core.intent_registry import IntentRegistry

logger = get_logger("interpreter")

_last_input_text = None  # Prevent repeated GPT calls
THRESHOLD = 0.15  # Match confidence threshold

def extract_numbers(text):
    return list(map(int, re.findall(r"\b\d+\b", text)))

def extract_search_query(text):
    match = re.search(r"(?:search|google|look(?:\s*up)?|find)(?:\s+for)?\s+(.*)", text)
    return match.group(1).strip() if match else text.strip()

def extract_region_for_flights(text: str) -> str:
    m = re.search(r"(?:track\s+(?:flight|flights)|show\s+(?:flight|flights)|planes|aircraft)\s+([a-zA-Z][a-zA-Z\s\-]+)$", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:over|above|near|in|around)\s+([a-zA-Z][a-zA-Z\s\-]+)$", text)
    if m:
        return m.group(1).strip()
    tokens = [t for t in re.split(r"[^a-zA-Z\-]+", text) if t]
    if tokens:
        return tokens[-1].strip()
    return ""

def extract_location_from_text(text: str) -> str:
    m = re.search(r"(?:weather|forecast|conditions)\s+(in|at|for)\s+([a-zA-Z\s\-]+)", text)
    if m:
        return m.group(2).strip()
    tokens = [t for t in re.split(r"[^a-zA-Z\-]+", text) if t]
    return tokens[-1] if tokens else ""

def extract_route_coordinates(match) -> list:
    start = match.group(1).strip()
    end = match.group(2).strip()
    return [[-1.61, 54.97], [-2.24, 53.48]]  # Placeholder coordinates


def handle_input(data):
    global _last_input_text
    if isinstance(data, str):
        data = {"text": data, "source": "user"}
    elif not isinstance(data, dict):
        data = {"text": str(data or ""), "source": "user"}

    text = data.get("text", "").lower().strip()

    if data.get("source") == "assistant":
        logger.debug("Ignoring assistant-originated input")
        return

    if text == _last_input_text:
        logger.debug(f"Ignoring duplicate prompt: '{text}'")
        return

    _last_input_text = text
    event_bus.emit(
        "USER_TURN_ACCEPTED",
        {
            "text": text,
            "source": data.get("source", "user"),
        },
    )

    # === INTENT MATCHING ===
    intent = IntentRegistry.match_intent(text)
    if intent:
        logger.info(f"Matched intent '{intent.name}' for input: '{text}'")
        event_bus.emit(
            "EMIT_USER_INTENT",
            {
                "intent": intent.name,
                "text": text,
                "source": data.get("source", "user"),
                "matched_by": "interpreter",
            },
        )
        return

    # === ESCALATE TO GPT ===
    logger.info(f"No intent matched for input: '{text}'")
    event_bus.emit(
        "INTENT_MATCH_FAILED",
        {
            "text": text,
            "source": data.get("source", "user"),
        },
    )

    if "?" in text or len(text.split()) < 4:
        logger.debug(f"Escalating to GPT for ambiguous input: '{text}'")
    event_bus.emit(
        "EMIT_CHAT_REQUEST",
        {
            "prompt": text,
            "source": data.get("source", "user"),
        },
    )


def handle_assistant_response(data):
    """
    Route normalized assistant output from chat_handler.

    Expected shapes:
      {"type": "message", "text": "...", "token": "..."}
      {"type": "tool_call", "intent": "...", "params": {...}, "tool_call_id": "..."}
    """
    if not isinstance(data, dict):
        logger.warning("Ignoring malformed assistant response.")
        return

    response_type = data.get("type")
    token = data.get("token")

    if response_type == "message":
        text = str(data.get("text", "") or "").strip()
        if not text:
            logger.warning("Ignoring empty assistant message.")
            return
        event_bus.emit("EMIT_SPEAK_CHUNK", {"chunk": text, "token": token})
        event_bus.emit("EMIT_SPEAK_END", {"token": token, "full_text": text})
        return

    if response_type == "tool_call":
        intent_name = str(data.get("intent", "") or "").strip()
        if not intent_name:
            logger.warning("Ignoring assistant tool call without intent.")
            return
        if IntentRegistry.get_intent(intent_name) is None:
            logger.warning(f"Ignoring assistant tool call for unknown intent '{intent_name}'.")
            event_bus.emit(
                "EMIT_SPEAK_CHUNK",
                {"chunk": f"I do not know how to run {intent_name}.", "token": token},
            )
            event_bus.emit(
                "EMIT_SPEAK_END",
                {"token": token, "full_text": f"I do not know how to run {intent_name}."},
            )
            return
        event_bus.emit(
            "EMIT_USER_INTENT",
            {
                "intent": intent_name,
                "params": data.get("params", {}) or {},
                "tool_call_id": data.get("tool_call_id"),
                "text": str(data.get("text", "") or ""),
                "source": data.get("source", "assistant"),
                "matched_by": "assistant_response",
            },
        )
        return

    logger.warning(f"Ignoring assistant response with unknown type '{response_type}'.")

def start_interpreter():
    logger.info("Interpreter online and listening for input events...")

    # Standard user input
    event_bus.subscribe("EMIT_INPUT_RECEIVED", handle_input)
    event_bus.subscribe("EMIT_ASSISTANT_RESPONSE", handle_assistant_response)

    # HouseCore remote input from Pi
    def transform_housecore_input(event):
        transcript = event.get("transcript")
        if transcript:
            logger.debug(f"Transforming HOUSECORE_INPUT to EMIT_INPUT_RECEIVED: {transcript}")
            event_bus.emit("EMIT_INPUT_RECEIVED", {
                "text": transcript,
                "source": "HouseCore"
            })

    event_bus.subscribe("HOUSECORE_INPUT", transform_housecore_input)
