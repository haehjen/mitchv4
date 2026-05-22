from core.event_bus import event_bus
from core.intent_registry import IntentRegistry
from core.system_log import get_logger

logger = get_logger("local_phi_intent")


def _strip_trigger(text: str) -> str:
    text = str(text or "").strip()
    lower = text.lower()
    triggers = (
        "ask miniphi",
        "ask mini phi",
        "ask phi",
        "ask phi3",
        "ask local phi",
        "ask the local model",
        "send to miniphi",
        "send this to miniphi",
        "run this through miniphi",
    )
    for trigger in triggers:
        if lower.startswith(trigger):
            return text[len(trigger):].lstrip(" :,.-")
    return text


def _handle_local_phi(text: str):
    prompt = _strip_trigger(text)
    if not prompt:
        event_bus.emit(
            "EMIT_TOOL_RESULT",
            {
                "function_name": "local_phi",
                "output": "MiniPhi is available. Ask it something after the trigger, for example: ask miniphi explain recursion simply.",
            },
        )
        return

    event_bus.emit(
        "EMIT_CHAT_REQUEST",
        {
            "prompt": prompt,
            "source": "local_phi_intent",
            "engine": "miniphi",
            "request_type": "local_phi",
        },
    )


IntentRegistry.register_intent(
    "local_phi",
    _handle_local_phi,
    keywords=[
        "ask miniphi",
        "ask mini phi",
        "ask phi",
        "ask phi3",
        "ask local phi",
        "ask the local model",
        "send to miniphi",
        "send this to miniphi",
        "run this through miniphi",
    ],
    patterns=[
        r"^(?:ask|send(?:\s+this)?\s+to|run(?:\s+this)?\s+through)\s+(?:mini\s*phi|miniphi|phi3?|local\s+phi|the\s+local\s+model)\b.*",
    ],
    priority=40,
)


def start_module(bus=None):
    logger.info("local_phi_intent started.")

