import os

from core.event_bus import event_bus


DEBUG = os.getenv("MITCH_DEBUG", "false").lower() == "true"


def emit(level: str, component: str, message: str, **data):
    payload = {
        "level": str(level).upper(),
        "component": component,
        "message": str(message),
    }
    if data:
        payload.update(data)
    event_bus.emit("SYSTEM_LOG", payload)


class EventLogger:
    def __init__(self, component: str):
        self.component = component

    def debug(self, message: str):
        if DEBUG:
            emit("DEBUG", self.component, message)

    def info(self, message: str):
        emit("INFO", self.component, message)

    def warning(self, message: str):
        emit("WARNING", self.component, message)

    def error(self, message: str):
        emit("ERROR", self.component, message)


def get_logger(component: str) -> EventLogger:
    return EventLogger(component)
