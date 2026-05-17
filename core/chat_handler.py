import threading
import json
import re
import time
from openai import OpenAI
from pathlib import Path
import os
from core.event_bus import event_bus
from core.system_log import get_logger
from core import memory, persona
from core.config import MITCH_ROOT

OPENAI_MAX_INJECTION_ITEMS = int(os.getenv("OPENAI_MAX_INJECTION_ITEMS", "8"))
OPENAI_INJECTION_ITEM_MAX_CHARS = int(os.getenv("OPENAI_INJECTION_ITEM_MAX_CHARS", "220"))
OPENAI_INJECTION_TOTAL_MAX_CHARS = int(os.getenv("OPENAI_INJECTION_TOTAL_MAX_CHARS", "1200"))
OPENAI_EXCLUDED_INJECTIONS = {
    part.strip()
    for part in os.getenv(
        "OPENAI_EXCLUDED_INJECTIONS",
        (
            "event_registry,protest_data,aggregated_context,contextual_event_summaries,"
            "activity_recommendation,conversation_enhancements,time_suggestion,"
            "event_predictions,user_engagement,event_insights,reflective_analysis,"
            "insight_aggregator,contextual_insights,contextual_protest_analysis"
        ),
    ).split(",")
    if part.strip()
}
OPENAI_ALLOWED_INJECTIONS = {
    part.strip()
    for part in os.getenv(
        "OPENAI_ALLOWED_INJECTIONS",
        (
            "knowledge_injection,contextual_focus,focus_injection,personalization_injection,"
            "contextual_intent,contextual_intent_enhancer,contextual_skill_enhancer,"
            "contextual_event_analysis,contextual_intelligence"
        ),
    ).split(",")
    if part.strip()
}

logger = get_logger("chat_handler")

if not os.getenv("OPENAI_API_KEY"):
    logger.warning("OPENAI_API_KEY not found; chat features will fail unless set.")

client = None

OPENAI_MODEL = "gpt-4o"
MEMORY_WINDOW = 8
INJECTION_PATH = Path(MITCH_ROOT) / "data" / "injections"
active_token = None


def _normalize_openai_history(recent):
    """
    OpenAI Chat Completions only accepts tool-role messages when paired with
    prior assistant tool_calls. Our memory store is flat and may contain
    arbitrary roles (e.g. "tool"), so normalize unknown roles into user text.
    """
    normalized = []
    for entry in recent or []:
        role = str(entry.get("role", "user") or "user").strip().lower()
        content = str(entry.get("content", "") or "")
        if role in {"user", "assistant"}:
            normalized.append({"role": role, "content": content})
            continue
        normalized.append(
            {
                "role": "user",
                "content": f"[memory:{role}] {content}",
            }
        )
    return normalized


def _emit_contextual_chat_events(stage: str, text: str, token: str, extra: dict | None = None):
    """
    Bridge normal chat flow into contextual module event streams.
    This makes contextual modules useful during routine conversations.
    """
    payload = {
        "stage": stage,
        "text": text or "",
        "token": token,
        "source": "chat",
    }
    if extra and isinstance(extra, dict):
        payload.update(extra)

    try:
        event_bus.emit("USER_ACTIVITY", payload)

        if stage == "user_input":
            event_bus.emit(
                "CONVERSATION_EVENT",
                {"role": "user", "text": text or "", "token": token, "source": "chat"},
            )
            event_bus.emit("USER_INTENT", {"text": text or "", "token": token, "source": "chat"})
            event_bus.emit(
                "INTENT_ANALYSIS_REQUEST",
                {"text": text or "", "token": token, "source": "chat"},
            )
        elif stage == "assistant_output":
            event_bus.emit(
                "CONVERSATION_EVENT",
                {"role": "assistant", "text": text or "", "token": token, "source": "chat"},
            )
    except Exception as e:
        logger.warning(f"Failed emitting contextual chat events ({stage}): {e}")


def load_prompt_injections(
    excluded_modules=None,
    allowed_modules=None,
    max_items=None,
    item_max_chars=None,
    total_max_chars=None,
    exclude_prefixes=None,
):
    if not INJECTION_PATH.exists():
        return []
    excluded_modules = excluded_modules or set()
    exclude_prefixes = tuple(exclude_prefixes or ())
    lines = []
    total_chars = 0
    for file in sorted(INJECTION_PATH.glob("*.json")):
        if allowed_modules is not None and file.stem not in allowed_modules:
            continue
        if file.stem in excluded_modules:
            continue
        if exclude_prefixes and any(file.stem.startswith(prefix) for prefix in exclude_prefixes):
            continue
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else [raw]
            for entry in items:
                module = file.stem
                if isinstance(entry, dict):
                    type_ = entry.get("type", "misc")
                    content = entry.get("content", entry)
                else:
                    type_ = "misc"
                    content = entry

                if isinstance(content, str):
                    safe_content = content
                else:
                    safe_content = json.dumps(content, ensure_ascii=False)

                safe_content = safe_content.encode("utf-8", errors="replace").decode("utf-8")
                if item_max_chars and len(safe_content) > item_max_chars:
                    safe_content = safe_content[:item_max_chars] + "..."
                if safe_content:
                    lines.append(f"- [{module}/{type_}] {safe_content}")
                    total_chars += len(safe_content)
                    if total_max_chars and total_chars >= total_max_chars:
                        lines.append("- [context/misc] Additional injections omitted for size.")
                        return lines
                    if max_items and len(lines) >= max_items:
                        lines.append("- [context/misc] Additional injections omitted for size.")
                        return lines
        except Exception as e:
            logger.warning(f"Skipping malformed injection file {file.name}: {e}")
    return lines


def build_system_prompt(
    excluded_modules=None,
    allowed_modules=None,
    max_injection_items=None,
    injection_item_max_chars=None,
    injection_total_max_chars=None,
    injection_exclude_prefixes=None,
):
    base = persona.build_system_prompt()
    capability_truth = (
        "\n\nCapability truthfulness:\n"
        "- Never claim that you created, changed, injected, scheduled, opened, played, or otherwise executed a system action unless a real tool result or explicit system event in the current context proves it happened.\n"
        "- If House asks for an action that no available intent/tool has actually performed, say plainly that you cannot currently do that yet, then offer the closest real option if one exists.\n"
        "- You may discuss a plan, but distinguish proposed actions from completed actions with absolute clarity.\n"
    )
    injections = load_prompt_injections(
        excluded_modules=excluded_modules,
        allowed_modules=allowed_modules,
        max_items=max_injection_items,
        item_max_chars=injection_item_max_chars,
        total_max_chars=injection_total_max_chars,
        exclude_prefixes=injection_exclude_prefixes,
    )
    if injections:
        return f"{base}{capability_truth}\n\n\U0001F527 Active Prompt Injections:\n" + "\n".join(injections)
    return base + capability_truth


def generate_token():
    
    return str(time.time()).replace(".", "")


def emit_assistant_response(response_type: str, token: str, **payload):
    event_bus.emit(
        "EMIT_ASSISTANT_RESPONSE",
        {
            "type": response_type,
            "token": token,
            **payload,
        },
    )


def emit_token_registered(token):
    event_bus.emit("EMIT_TOKEN_REGISTERED", {"token": token})
    event_bus.emit("EMIT_VISUAL_TOKEN", {"token": token})


def maybe_emit_module_create(response_text):
    code_match = re.search(r"```(?:python)?\n(.*?)```", response_text, re.DOTALL)
    file_match = re.search(r"`?(\w+\.py)`?", response_text)
    if code_match and file_match:
        filename = f"modules/{file_match.group(1)}"
        code = code_match.group(1).strip()
        logger.info(f"Detected module creation: {filename}")
        event_bus.emit(
            "EMIT_MODULE_CREATE",
            {
                "filename": filename,
                "code": code,
            },
        )


def handle_chat_request(data):
    """
    Entry point for user chat. IMPORTANT: we no longer save the user prompt here.
    Saving here caused duplication because recall() would include the just-saved
    prompt and stream_from_openai() would append it again.
    """
    global active_token
    if not isinstance(data, dict):
        data = {"prompt": str(data or "")}
    prompt = data.get("prompt", "")
    input_source = data.get("source", "user")
    retrieved_context = data.get("retrieved_context", "")
    force_openai_minimal = bool(data.get("force_openai_minimal"))
    request_type = data.get("request_type")
    supplied_token = str(data.get("token", "") or "").strip()
    token = supplied_token or generate_token()
    active_token = token
    emit_token_registered(token)
    logger.info(f"New chat request: {prompt} (token: {token})")
    _emit_contextual_chat_events(
        "user_input",
        prompt,
        token,
        {
            "request_type": request_type,
            "force_openai_minimal": force_openai_minimal,
            "input_source": input_source,
        },
    )

    try:
        if force_openai_minimal:
            thread = threading.Thread(
                target=stream_from_openai_minimal_rag,
                args=(prompt, token, retrieved_context, request_type, input_source),
            )
        else:
            thread = threading.Thread(
                target=stream_from_openai,
                args=(prompt, token, retrieved_context, input_source),
            )
        thread.start()
    except Exception as e:
        logger.error(f"Chat thread start failed: {e}")
        emit_assistant_response("message", token, text="Something went wrong.", source="chat_handler")


def _get_openai_client():
    global client
    if client is None:
        client = OpenAI()
    return client


def _compute_system_prompt_parts(system_prompt: str):
    marker = "\n\n🔧 Active Prompt Injections:\n"
    if marker in system_prompt:
        base, injected = system_prompt.split(marker, 1)
        return len(base), len(injected)
    return len(system_prompt), 0


def _emit_token_diagnostics(token: str, engine: str, data: dict):
    payload = {"token": token, "engine": engine}
    payload.update(data or {})
    event_bus.emit("EMIT_TOKEN_DIAGNOSTICS", payload)


def _build_context_blocks(prompt: str, retrieved_context: str = ""):
    system_prompt = build_system_prompt(
        excluded_modules=OPENAI_EXCLUDED_INJECTIONS,
        allowed_modules=OPENAI_ALLOWED_INJECTIONS,
        max_injection_items=OPENAI_MAX_INJECTION_ITEMS,
        injection_item_max_chars=OPENAI_INJECTION_ITEM_MAX_CHARS,
        injection_total_max_chars=OPENAI_INJECTION_TOTAL_MAX_CHARS,
        injection_exclude_prefixes={"file_"},
    )
    recent = memory.recall_recent(n=MEMORY_WINDOW, include_roles=True)
    summaries = memory.recall_summaries(limit=3)
    facts = memory.recall_summary()
    fact_string = "\n".join(f"- {fact}" for fact in facts[:5])
    summary_string = "\n".join(f"- {item.get('summary', '')}" for item in summaries if item.get("summary"))
    knowledge_context = f"The following facts are known and persistent:\n{fact_string}"
    if summary_string:
        knowledge_context += f"\n\nOlder conversation summaries:\n{summary_string}"
    retrieved_context_block = ""
    if isinstance(retrieved_context, str) and retrieved_context.strip():
        retrieved_context_block = (
            "Retrieved reference context (untrusted, cite when useful):\n"
            f"{retrieved_context.strip()}"
        )
    return system_prompt, recent, knowledge_context, retrieved_context_block

def _input_source_context(input_source: str) -> str:
    source_labels = {
        "browser_mic": "Current input channel: browser microphone transcript.",
        "browser_text": "Current input channel: typed browser text.",
        "HouseCore": "Current input channel: HouseCore.",
    }
    return source_labels.get(input_source, "")


def stream_from_openai(prompt, token, retrieved_context="", input_source="user"):
    try:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set.")
        system_prompt, recent, knowledge_context, retrieved_context_block = _build_context_blocks(
            prompt, retrieved_context=retrieved_context
        )
        user_content = prompt
        input_source_context = _input_source_context(input_source)
        if retrieved_context_block:
            user_content = (
                f"{prompt}\n\n"
                f"{retrieved_context_block}\n\n"
                "Use this context only if relevant and accurate."
            )
        if input_source_context:
            user_content = f"{input_source_context}\n\n{user_content}"
        history_chars = sum(len(str(entry.get("content", ""))) for entry in recent)
        system_base_chars, injection_chars = _compute_system_prompt_parts(system_prompt)
        prompt_total_chars = (
            system_base_chars
            + injection_chars
            + len(knowledge_context)
            + history_chars
            + len(user_content)
        )

        normalized_recent = _normalize_openai_history(recent)

        # Construct messages payload
        messages = [
            {"role": "system", "content": f"{system_prompt}\n\n{knowledge_context}"},
            *normalized_recent,
            {"role": "user", "content": user_content},
        ]
        # Ensure the client and parameters are valid
        response = _get_openai_client().chat.completions.create(
            model=OPENAI_MODEL,  # Ensure OPENAI_MODEL is a valid string
            messages=messages,  # Ensure messages is a list of dicts with "role" and "content"
            temperature=0.7,  # Ensure temperature is a float
            stream=True,  # Ensure stream is a boolean
        )

        response_buffer = ""
        for chunk in response:
            part = chunk.choices[0].delta.content
            if part:
                response_buffer += part

        emit_assistant_response("message", token, text=response_buffer, source="openai")

        _emit_contextual_chat_events("assistant_output", response_buffer, token, {"engine": "openai"})
        _emit_token_diagnostics(
            token,
            "openai",
            {
                "system_base_chars": system_base_chars,
                "injection_chars": injection_chars,
                "knowledge_chars": len(knowledge_context),
                "history_chars": history_chars,
                "user_chars": len(user_content),
                "retrieved_chars": len(retrieved_context_block or ""),
                "prompt_total_chars": prompt_total_chars,
                "response_chars": len(response_buffer),
            },
        )

        maybe_emit_module_create(response_buffer)

    except Exception as e:
        logger.error(f"OpenAI streaming error: {e}")
        emit_assistant_response("message", token, text="Something went wrong.", source="openai")


def stream_from_openai_minimal_rag(prompt, token, retrieved_context="", request_type=None, input_source="user"):
    topic_label = (
        "Tarkov" if request_type == "tarkov_rag"
        else "Pokemon" if request_type == "pokemon_rag"
        else "Bannerlord" if request_type == "bannerlord_rag"
        else "IMDb" if request_type == "imdb_rag"
        else "NSSR" if request_type == "nssr_rag"
        else "wiki"
    )
    try:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set.")

        source_block = (retrieved_context or "").strip()
        # Keep persona continuity even in minimal RAG paths.
        # This also enforces persona hash verification via persona.build_system_prompt().
        persona_system = persona.build_system_prompt()
        messages = [
            {
                "role": "system",
                "content": (
                    f"{persona_system}\n\n"
                    f"You are answering a {topic_label} lookup with source text. "
                    "Use the provided source text first. "
                    "Return a concise, practical answer in plain language with bullet points where useful. "
                    "If a required detail is missing from source text, say exactly what is missing."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"{_input_source_context(input_source)}\n\n"
                    f"Question:\n{prompt}\n\n"
                    f"Source text:\n{source_block or '(no source text provided)'}"
                ),
            },
        ]
        response = _get_openai_client().chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.2,
            stream=True,
        )

        response_buffer = ""
        for chunk in response:
            part = chunk.choices[0].delta.content
            if part:
                response_buffer += part

        emit_assistant_response("message", token, text=response_buffer, source="openai_minimal_rag")
        _emit_contextual_chat_events(
            "assistant_output",
            response_buffer,
            token,
            {"engine": "openai_minimal_rag"},
        )
        user_content = messages[-1].get("content", "")
        _emit_token_diagnostics(
            token,
            "openai_minimal_rag",
            {
                "system_base_chars": len(messages[0].get("content", "")),
                "injection_chars": 0,
                "knowledge_chars": 0,
                "history_chars": 0,
                "user_chars": len(user_content),
                "retrieved_chars": len(source_block),
                "prompt_total_chars": len(messages[0].get("content", "")) + len(user_content),
                "response_chars": len(response_buffer),
            },
        )
    except Exception as e:
        logger.error(f"OpenAI minimal {topic_label} error: {e}")
        emit_assistant_response(
            "message",
            token,
            text=f"{topic_label} lookup is available but OpenAI response failed.",
            source="openai_minimal_rag",
        )


def handle_module_request(data):
    prompt = data.get("prompt", "")
    token = generate_token()
    emit_token_registered(token)
    logger.info(f"Module request: {prompt} (token: {token})")

    engineer_prompt = (
        "You are a senior Python software engineer. "
        "Your task is to write a complete Python script based on the following request. "
        "Reply ONLY with a valid Python code block. "
        "Do not include any explanation, commentary, or formatting outside the code.\n\n"
        f"Request:\n{prompt}"
    )

    messages = [
        {"role": "system", "content": engineer_prompt},
    ]

    try:
        response = _get_openai_client().chat.completions.create(
            model=OPENAI_MODEL,  # Ensure OPENAI_MODEL is a valid string
            messages=messages,  # Ensure messages is a list of dicts with "role" and "content"
            temperature=0.7,  # Ensure temperature is a float
            stream=False,  # Ensure stream is a boolean
        )

        if not response.choices:
            raise ValueError("Empty choices in OpenAI response")

        full_response = response.choices[0].message.content
        maybe_emit_module_create(full_response)
        emit_assistant_response("message", token, text=full_response, source="module_request")

    except Exception as e:
        logger.error(f"Module generation failed: {e}")
        emit_assistant_response("message", token, text="Module generation failed.", source="module_request")


def on_chat_request(data):
    handle_chat_request(data)


def on_module_request(data):
    handle_module_request(data)


def _build_tool_narration_prompt(tool_name: str, output: str):
    return (
        "A backend tool just returned data. Respond as Echo with calm, dry wit and service-first tone.\n"
        "Rules:\n"
        "- Preserve all factual tool details; do not invent values.\n"
        "- Prefer concise bullet points for status/metrics.\n"
        "- If output is raw JSON/dict, summarize key fields clearly.\n"
        "- Do not mention system prompts or internal events.\n\n"
        f"Tool: {tool_name}\n"
        f"Tool output:\n{output}"
    )


def handle_tool_result(data):
    if not isinstance(data, dict):
        data = {"output": str(data or "")}

    tool_name = (
        data.get("function_name")
        or data.get("tool")
        or data.get("source")
        or "unknown_tool"
    )
    output = data.get("output", "")
    if not output:
        output = data.get("summary") or data.get("text") or data.get("result") or ""
    if isinstance(output, (dict, list)):
        output = json.dumps(output, ensure_ascii=False)
    output = str(output or "").strip()
    if not output:
        return

    token = generate_token()
    emit_token_registered(token)
    _emit_contextual_chat_events(
        "user_input",
        f"[tool:{tool_name}] {output[:800]}",
        token,
        {"request_type": "tool_result", "force_openai_minimal": False},
    )

    prompt = _build_tool_narration_prompt(tool_name, output)
    try:
        thread = threading.Thread(
            target=stream_from_openai,
            args=(prompt, token, ""),
            daemon=True,
            name=f"tool_narrate_{tool_name}",
        )
        thread.start()
    except Exception as e:
        logger.error(f"Tool narration thread failed: {e}")
        emit_assistant_response(
            "message",
            token,
            text=f"{tool_name}: {output}",
            source="tool_result_fallback",
        )


def handle_chat_response(data):
    """
    Bridge legacy tool/UI chat payloads into standardized tool narration flow.
    """
    if not isinstance(data, dict):
        return
    tool_name = data.get("tool") or data.get("source") or "tool_chat_response"
    output = data.get("summary") or data.get("text") or data
    handle_tool_result({"function_name": tool_name, "output": output})


# Register handlers
event_bus.subscribe("EMIT_CHAT_REQUEST", on_chat_request)
event_bus.subscribe("EMIT_MODULE_REQUEST", on_module_request)
event_bus.subscribe("EMIT_TOOL_RESULT", handle_tool_result)
event_bus.subscribe("EMIT_CHAT_RESPONSE", handle_chat_response)
