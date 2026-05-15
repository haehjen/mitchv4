import os
import json
from pathlib import Path
import mimetypes
from core.config import MITCH_ROOT

from core.event_bus import event_bus
from core.vision_ai import describe_image_from_url
from core.system_log import get_logger

# Optional parsers (present in your stack)
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text
from PIL import Image
INJECTION_PATH = f"{MITCH_ROOT}/data/injections/"
UPLOAD_DIR = f"{MITCH_ROOT}/uploads"
PUBLIC_URL_BASE = "https://mitch.andymitchell.online/uploads"  # ✅ for GPT-4o vision

logger = get_logger("file_ingestor")

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
HTML_EXTS = {"html", "htm"}
TEXT_EXTS = {"txt", "md", "log"}


def _classify(filetype: str | None, filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")

    if filetype and "/" in filetype:
        major, minor = filetype.split("/", 1)
        if major == "image":
            return "image"
        if minor == "pdf" or ext == "pdf":
            return "pdf"
        if minor in {"html", "x-html"} or ext in HTML_EXTS:
            return "html"
        if major == "text" or ext in TEXT_EXTS:
            return "text"

    ft = (filetype or "").lower()
    if ft in IMAGE_EXTS or ext in IMAGE_EXTS:
        return "image"
    if ft == "pdf" or ext == "pdf":
        return "pdf"
    if ft in HTML_EXTS or ext in HTML_EXTS:
        return "html"
    if ft in TEXT_EXTS or ext in TEXT_EXTS:
        return "text"

    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        if guessed.startswith("image/"):
            return "image"
        if guessed.endswith("/pdf"):
            return "pdf"
        if guessed.endswith("/html"):
            return "html"
        if guessed.startswith("text/"):
            return "text"

    return "unknown"


def analyze_image(path: Path) -> dict:
    try:
        with Image.open(path) as img:
            width, height = img.size
            fmt = img.format
        logger.info(f"[file_ingestor] Image analysis: {path} ({fmt}, {width}x{height})")
        return {
            "kind": "image",
            "description": f"Image ({fmt}, {width}x{height})",
            "path": str(path)
        }
    except Exception as e:
        logger.error(f"[file_ingestor] Image analysis failed for {path}: {e}")
        return {"error": str(e), "kind": "image"}


def extract_pdf_text(path: Path) -> dict:
    try:
        text = extract_text(str(path))
        snippet = text[:5000] if text else ""
        logger.info(f"[file_ingestor] PDF text extracted: {path} ({len(text)} chars)")
        return {"kind": "pdf", "text": snippet, "path": str(path)}
    except Exception as e:
        logger.error(f"[file_ingestor] PDF extraction failed for {path}: {e}")
        return {"error": str(e), "kind": "pdf"}


def extract_html_text(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        text = soup.get_text(separator="\n")
        snippet = text[:5000] if text else ""
        logger.info(f"[file_ingestor] HTML text extracted: {path} ({len(text)} chars)")
        return {"kind": "html", "text": snippet, "path": str(path)}
    except Exception as e:
        logger.error(f"[file_ingestor] HTML extraction failed for {path}: {e}")
        return {"error": str(e), "kind": "html"}


def extract_text_file(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        snippet = text[:5000] if text else ""
        logger.info(f"[file_ingestor] TEXT read: {path} ({len(text)} chars)")
        return {"kind": "text", "text": snippet, "path": str(path)}
    except Exception as e:
        logger.error(f"[file_ingestor] TEXT read failed for {path}: {e}")
        return {"error": str(e), "kind": "text"}


def handle_file_event(event: dict):
    filename = (event or {}).get("filename")
    filetype = (event or {}).get("filetype") or (event or {}).get("type")
    url_path = (event or {}).get("url")

    if not filename:
        logger.warning("[file_ingestor] Incomplete file event: missing filename")
        return

    local_path = Path(UPLOAD_DIR) / filename
    kind = _classify(filetype, filename)
    logger.info(f"[file_ingestor] Handling upload: {local_path} kind={kind} src_url={url_path}")

    if not local_path.exists():
        logger.warning(f"[file_ingestor] Uploaded file not found on disk: {local_path}")
        return

    result = {}
    content = "File uploaded."

    try:
        if kind == "image":
            result = analyze_image(local_path)
            public_url = f"{PUBLIC_URL_BASE}/{filename}"
            content = describe_image_from_url(public_url)
            result["vision_description"] = content
        elif kind == "pdf":
            result = extract_pdf_text(local_path)
            content = result.get("text", "")[:400] or "PDF uploaded."
        elif kind == "html":
            result = extract_html_text(local_path)
            content = result.get("text", "")[:400] or "HTML file uploaded."
        elif kind == "text":
            result = extract_text_file(local_path)
            content = result.get("text", "")[:400] or "Text file uploaded."
        else:
            result = {"kind": "unknown", "note": "Unhandled file type", "path": str(local_path)}
            content = "Unsupported file uploaded."
    except Exception as e:
        content = f"[file_ingestor] Processing failed: {e}"
        result = {"kind": "error", "error": str(e)}

    # Build final injection
    injection = {
        "source": filename,
        "url": f"{PUBLIC_URL_BASE}/{filename}",
        "type": kind,
        "tags": ["upload", kind, "gpt4o-vision"] if kind == "image" else ["upload", kind],
        "content": content,
        "ingested": result
    }

    inject_path = Path(INJECTION_PATH) / f"file_{filename}.json"
    try:
        inject_path.parent.mkdir(parents=True, exist_ok=True)
        with open(inject_path, "w", encoding="utf-8") as f:
            json.dump(injection, f)
        logger.info(f"[file_ingestor] Injected context: {inject_path}")
    except Exception as e:
        logger.error(f"[file_ingestor] Failed to write injection file {inject_path}: {e}")

    event_bus.emit("EMIT_PREVIEW_URL", {"url": f"/uploads/{filename}"})

    # Bridge upload analysis into normal assistant response flow (UI + TTS).
    event_bus.emit(
        "EMIT_TOOL_RESULT",
        {
            "function_name": "file_ingestor",
            "output": {
                "filename": filename,
                "type": kind,
                "summary": content,
                "url": f"{PUBLIC_URL_BASE}/{filename}",
            },
        },
    )


def start_module(event_bus):
    event_bus.subscribe("EMIT_FILE_READY", handle_file_event)
    logger.info("[file_ingestor] Module started. Listening for EMIT_FILE_READY.")
