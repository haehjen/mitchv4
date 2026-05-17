import os
# --- Load secrets before anything else ---
from core.keys_loader import load_keys
load_keys()
import threading
import sys
import importlib
from core import dispatcher
from core.event_bus import event_bus, INNERMONO_PATH
from core import (
    interpreter,
    memory,
    pending_updates,
    presence,
    stream_mouth,
    chat_handler,
)
from core.visual import visual_web

DEBUG = os.getenv("MITCH_DEBUG", "false").lower() == "true"
PRUNE_CONTEXTUAL_MODULES = os.getenv("MITCH_PRUNE_CONTEXTUAL", "0").lower() in {"1", "true", "yes"}
ENABLE_THOUGHT_THREAD = os.getenv("MITCH_ENABLE_THOUGHT", "0").lower() in {"1", "true", "yes"}
CLEAR_INNERMONO_ON_BOOT = os.getenv("MITCH_CLEAR_INNERMONO_ON_BOOT", "1").lower() in {"1", "true", "yes"}

EXCLUDED_MODULES = {
    "__init__", "dummy", "visual", "folder_access"
}

shutdown_hooks = []
loaded_modules = []
thread_refs = []
_stop_event = threading.Event()
_shutdown_started = threading.Event()

def _log_event(level: str, message: str, **kv):
    """Emit boot/runtime milestones into the canonical event stream."""
    event_bus.emit(
        "SYSTEM_LOG",
        {
            "level": level,
            "message": message,
            **kv,
        },
    )

def _start_thread(target, name: str, *args, **kwargs):
    t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True, name=name)
    t.start()
    return t


def _run_shutdown():
    if _shutdown_started.is_set():
        return
    _shutdown_started.set()

    print("\n🛑 MITCH shutdown signal received.")
    event_bus.emit("SHUTDOWN")

    for hook in shutdown_hooks:
        try:
            hook()
        except KeyboardInterrupt:
            print("[Shutdown] Interrupted while running shutdown hooks. Forcing exit.")
            return
        except Exception as e:
            print(f"[Shutdown] Error in shutdown hook: {e}")

    for t in thread_refs:
        try:
            if t.is_alive():
                t.join(timeout=5)
        except KeyboardInterrupt:
            print("[Shutdown] Interrupted while waiting for background threads. Forcing exit.")
            return

    print(f"[Shutdown] Shutdown complete for modules: {', '.join(loaded_modules)}")


def main():
    print("⚡ ECHO starting...")

    if CLEAR_INNERMONO_ON_BOOT and INNERMONO_PATH:
        try:
            os.makedirs(os.path.dirname(INNERMONO_PATH), exist_ok=True)
            with open(INNERMONO_PATH, "w", encoding="utf-8"):
                pass
            print("🧹 Cleared innermono.log on boot.")
        except Exception as e:
            print(f"[boot] Failed to clear innermono.log: {e}")

    # Core subsystems
    stream_mouth.start_stream_mouth()
    memory.start_memory()
    pending_updates.start_pending_updates()
    presence.start_presence()
    _start_thread(dispatcher.start_dispatcher, "dispatcher")
    _start_thread(interpreter.start_interpreter, "interpreter")
    _start_thread(visual_web.start_visual, "visual_web")

    # --- Explicitly ensure file_ingestor is online (so EMIT_FILE_READY is handled) ---
    try:
        from modules import file_ingestor  # noqa: F401
        if hasattr(file_ingestor, "start_module"):
            _start_thread(file_ingestor.start_module, "file_ingestor", event_bus)
            loaded_modules.append("file_ingestor")
            _log_event("INFO", "[boot] file_ingestor started")
        else:
            _log_event("WARNING", "[boot] file_ingestor has no start_module()")
    except Exception as e:
        _log_event("ERROR", "[boot] file_ingestor failed to start", error=str(e))

    # --- Autoload all other modules (with hard logging on failure) ---
    module_path = os.path.join(os.path.dirname(__file__), "modules")
    for filename in os.listdir(module_path):
        name, ext = os.path.splitext(filename)
        if ext != ".py" or name in EXCLUDED_MODULES or name == "file_ingestor":
            continue
        if PRUNE_CONTEXTUAL_MODULES and name.startswith("contextual_"):
            _log_event("INFO", "[autoload] pruned contextual module", module=name)
            continue

        try:
            mod = importlib.import_module(f"modules.{name}")
            if hasattr(mod, "start_module"):
                _start_thread(mod.start_module, f"{name}.start_module", event_bus)
                if DEBUG:
                    print(f"[AutoLoader] Started {name}.start_module()")
                loaded_modules.append(name)
            elif hasattr(mod, "run"):
                _start_thread(mod.run, f"{name}.run", event_bus)
                if DEBUG:
                    print(f"[AutoLoader] Started {name}.run()")
                loaded_modules.append(name)
            else:
                if DEBUG:
                    print(f"[AutoLoader] {name}.py found but no usable entrypoint.")
                _log_event("INFO", "[autoload] module has no entrypoint", module=name)

            if hasattr(mod, "shutdown"):
                shutdown_hooks.append(mod.shutdown)

        except Exception as e:
            _log_event("ERROR", "[autoload] failed to load module", module=name, error=str(e))
            if DEBUG:
                print(f"[AutoLoader] Failed to load {name}.py: {e}")

    # Optional thought loop (off by default to reduce runtime noise).
    if ENABLE_THOUGHT_THREAD:
        try:
            from thought import EchoThoughtThread

            echo_thought = EchoThoughtThread()
            echo_thought.start()
            shutdown_hooks.append(echo_thought.shutdown)
            thread_refs.append(echo_thought)
            if DEBUG:
                print("🧠 Echo's self-evolution thread online.")
        except Exception as e:
            _log_event("WARNING", "[boot] thought thread disabled after startup error", error=str(e))

    print("✅ Echo V4 online. Awaiting input...")

    try:
        # Idle without burning CPU
        while not _stop_event.wait(1.0):
            pass
    except KeyboardInterrupt:
        _run_shutdown()

if __name__ == "__main__":
    main()
