# core/keys_loader.py
import os
from pathlib import Path

def load_keys():
    key_path = Path(__file__).resolve().parent.parent / "mitchskeys"
    if not key_path.exists():
        return
    for line in key_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue

        # Support both:
        #   KEY=value
        #   export KEY=value
        if raw.lower().startswith("export "):
            raw = raw[7:].strip()

        if "=" not in raw:
            continue

        k, v = raw.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and not os.getenv(k):
            os.environ[k] = v
