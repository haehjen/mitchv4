import os
import time
from pathlib import Path
from openai import OpenAI
from core.config import MITCH_ROOT
from core.system_log import get_logger

logger = get_logger("vision_ai")

# Public image URL served by Flask + NGINX
CAMERA_URL = "https://mitch.andymitchell.online/latest.jpg"
BROWSER_CAMERA_PATH = Path(MITCH_ROOT) / "uploads" / "browser_camera_latest.jpg"
BROWSER_CAMERA_MAX_AGE_SECONDS = int(os.getenv("MITCH_BROWSER_CAMERA_MAX_AGE", "180"))

class VisionAI:
    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("[VisionAI] OPENAI_API_KEY not set; vision features may fail.")
        self.client = OpenAI(api_key=api_key)

    def _has_fresh_browser_camera(self) -> bool:
        try:
            if not BROWSER_CAMERA_PATH.exists():
                return False
            age = time.time() - BROWSER_CAMERA_PATH.stat().st_mtime
            return age <= BROWSER_CAMERA_MAX_AGE_SECONDS
        except Exception:
            return False

    def _prepare_frame_for_analysis(self):
        """
        Browser camera is the only supported live vision source in v4.
        """
        if self._has_fresh_browser_camera():
            logger.info("[VisionAI] Using fresh browser camera frame for analysis.")
            return
        logger.info("[VisionAI] No fresh browser camera frame is available.")

    async def capture_and_describe(self):
        try:
            self._prepare_frame_for_analysis()
        except Exception as e:
            logger.error(f"Failed to capture image: {e}")
            return f"[VisionAI] Failed to capture image: {e}"

        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Please describe this image."},
                        {"type": "image_url", "image_url": {"url": CAMERA_URL}}
                    ]
                }
            ],
            max_tokens=800,
            temperature=0.7
        )

        return response.choices[0].message.content

    async def detect_objects(self):
        try:
            self._prepare_frame_for_analysis()
        except Exception as e:
            logger.error(f"Failed to capture image: {e}")
            return f"[VisionAI] Failed to capture image: {e}"

        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "List all objects you can identify in this image."},
                        {"type": "image_url", "image_url": {"url": CAMERA_URL}}
                    ]
                }
            ],
            max_tokens=800,
            temperature=0.7
        )

        return response.choices[0].message.content

def describe_image_from_url(image_url: str) -> str:
    try:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return "[VisionAI] Error: OPENAI_API_KEY not set."
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Please describe this image clearly and concisely."},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                }
            ],
            max_tokens=800,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"[VisionAI] Failed to describe image: {e}")
        return f"[VisionAI] Error: {e}"
