import asyncio
import threading
import time
import os
import json
from core.event_bus import event_bus
from core.config import MITCH_ROOT
from core.intent_registry import IntentRegistry

DEBUG = os.getenv("MITCH_DEBUG", "false").lower() == "true"

vision_ai = None

def get_vision_ai():
    global vision_ai
    if vision_ai is None:
        from core.vision_ai import VisionAI
        vision_ai = VisionAI()
    return vision_ai

_dispatcher_started = False
_USER_FEEDBACK_EVENTS = (
    "EMIT_ASSISTANT_RESPONSE",
    "EMIT_CHAT_REQUEST",
    "EMIT_SPEAK",
    "EMIT_TOOL_RESULT",
    "EMIT_CHAT_RESPONSE",
)


def _new_token() -> str:
    return str(int(time.time() * 1_000_000))


def _intent_human_name(intent_name: str) -> str:
    return str(intent_name or "action").replace("_", " ").strip()


def _emit_action_confirmation(intent_name: str):
    spoken = f"Done. I handled {_intent_human_name(intent_name)}."
    event_bus.emit(
        "EMIT_TOOL_RESULT",
        {
            "tool_call_id": None,
            "function_name": intent_name,
            "output": spoken,
        },
    )


def _run_registry_intent_with_feedback(intent, text: str):
    feedback_seen = {"value": False}
    callbacks = []

    def _mark_feedback(_data):
        feedback_seen["value"] = True

    for event_name in _USER_FEEDBACK_EVENTS:
        callbacks.append((event_name, _mark_feedback))
        event_bus.subscribe(event_name, _mark_feedback)
    try:
        intent.handler(text)
    except Exception as exc:
        logger_message = f"Intent '{intent.name}' handler failed, falling back to chat: {exc}"
        if DEBUG:
            print(f"[DISPATCHER] {logger_message}")
        event_bus.emit("EMIT_CHAT_REQUEST", {"prompt": text})
    finally:
        # Give synchronous handler emits a brief moment to fire before fallback.
        time.sleep(0.12)
        for event_name, callback in callbacks:
            event_bus.unsubscribe(event_name, callback)
    if not feedback_seen["value"]:
        _emit_action_confirmation(intent.name)

def emit_intent_response(intent, tool_call_id, output, status="success", token=None):
    if not token:
        token = str(time.time())

    payloads = [
        ("EMIT_TOOL_RESULT", {
            "tool_call_id": tool_call_id,
            "function_name": intent,
            "output": output,
            "source_token": token,
        }),
        ("EMIT_ACK", {
            "status": status,
            "intent": intent,
            "executed": output
        })
    ]

    for event, payload in payloads:
        event_bus.emit(event, payload)

def _describe_scene_intent(_text: str):
    description = asyncio.run(get_vision_ai().capture_and_describe())
    emit_intent_response("describe_scene", None, description, token=_new_token())


def _detect_objects_intent(_text: str):
    objects = asyncio.run(get_vision_ai().detect_objects())
    emit_intent_response("detect_objects", None, objects, token=_new_token())


IntentRegistry.register_intent(
    "describe_scene",
    _describe_scene_intent,
    keywords=[
        "what can you see",
        "what do you see",
        "can you see me",
        "look through the camera",
        "describe the scene",
        "describe what you see",
        "see me through the camera",
    ],
    priority=20,
)

IntentRegistry.register_intent(
    "detect_objects",
    _detect_objects_intent,
    keywords=[
        "detect objects",
        "what objects can you see",
        "what objects do you see",
        "list visible objects",
        "what is in front of you",
    ],
    priority=20,
)

def handle_user_intent(data):
    if DEBUG:
        print("[DISPATCHER] handle_user_intent triggered")

    if not data:
        return

    intent = data.get("intent")
    text = str(data.get("text", "") or "")
    params = data.get("params", {})
    tool_call_id = data.get("tool_call_id")
    token = str(time.time())

    registered_intent = IntentRegistry.get_intent(intent)
    if registered_intent is not None:
        _run_registry_intent_with_feedback(registered_intent, text)
        event_bus.emit(
            "EMIT_ACK",
            {
                "status": "dispatched",
                "intent": intent,
                "route": "registry",
            },
        )
        return

    if intent == "launch_drone":
        drone_ids = params.get("drone_ids", [])
        output = f"Launching drones {', '.join(map(str, drone_ids))}"
        if DEBUG:
            print(f"[DISPATCHER] {output}")
        emit_intent_response(intent, tool_call_id, output, token=token)

    elif intent == "describe_scene":
        try:
            description = asyncio.run(get_vision_ai().capture_and_describe())
            emit_intent_response(intent, tool_call_id, description, token=token)
        except Exception as e:
            error_msg = f"Failed to describe the scene: {e}"
            emit_intent_response(intent, tool_call_id, error_msg, status="error", token=token)

    elif intent == "detect_objects":
        try:
            objects = asyncio.run(get_vision_ai().detect_objects())
            emit_intent_response(intent, tool_call_id, objects, token=token)
        except Exception as e:
            error_msg = f"Failed to detect objects: {e}"
            emit_intent_response(intent, tool_call_id, error_msg, status="error", token=token)

    else:
        # Try to resolve via dynamic intents
        dynamic_path = os.path.join(MITCH_ROOT, "data", "injections", "dynamic_intents.json")
        intent_routed = False

        if os.path.exists(dynamic_path):
            try:
                with open(dynamic_path, "r") as f:
                    dynamic_data = json.load(f)
                    for entry in dynamic_data.get("intents", []):
                        if entry.get("intent") == intent:
                            action = entry.get("action")
                            if action:
                                routed_event = f"dynamic_intent:{action}"
                                event_bus.emit(routed_event, data)
                                event_bus.emit("EMIT_ACK", {
                                    "status": "dispatched",
                                    "intent": intent,
                                    "routed_event": routed_event
                                })
                                intent_routed = True
                                break
            except Exception as e:
                if DEBUG:
                    print(f"[DISPATCHER] Failed reading dynamic intents: {e}")

        if not intent_routed:
            msg = f"Sorry, I didn't understand the command: {intent}"
            emit_intent_response(intent, tool_call_id, msg, status="failure", token=token)

def start_dispatcher():
    global _dispatcher_started
    if _dispatcher_started:
        if DEBUG:
            print(f"[DISPATCHER] Ignoring second call to start_dispatcher in thread {threading.current_thread().name}")
        return

    _dispatcher_started = True
    if DEBUG:
        print(f"[DISPATCHER] start_dispatcher() CALLED in thread {threading.current_thread().name}")

    event_bus.subscribe("EMIT_USER_INTENT", handle_user_intent)
    if DEBUG:
        print("[DISPATCHER] Subscribed to EMIT_USER_INTENT")
