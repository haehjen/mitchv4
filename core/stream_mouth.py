import os
import queue
import threading
import wave
import time
import tempfile
import base64
import re
from pathlib import Path
import numpy as np
import soundfile as sf
import pyaudio
from collections import defaultdict
from core.event_bus import EventBus
from piper import PiperVoice
from core.config import MITCH_ROOT
from core.system_log import get_logger

logger = get_logger("stream_mouth")

# === CONFIG ===
MODEL_PATH = Path(MITCH_ROOT) / "modules/voice/en_GB-northern_english_male-medium.onnx"
CONFIG_PATH = MODEL_PATH.with_suffix(".onnx.json")
CHUNK_TRIGGER_LEN = 40
CHUNK_BREAK_CHARS = {'.', '?', '!', '\n'}
CHUNK_STRONG_BREAK_CHARS = {'.', '?', '!', '\n'}
CHUNK_WEAK_BREAK_CHARS = {',', ';', ':'}
MIN_WORDS_PER_CHUNK = 6
MAX_CHUNK_CHARS = 220
SOFT_FLUSH_SECONDS = 0.28

DEBUG_SPEAKER = os.getenv("MITCH_SPEAKER_DEBUG", "false").lower() == "true"
BROWSER_AUDIO_STREAM = os.getenv("MITCH_BROWSER_AUDIO_STREAM", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}

# === Speaker Class ===
class StreamMouth:
    def __init__(self):
        self.voice = PiperVoice.load(
            model_path=MODEL_PATH,
            config_path=CONFIG_PATH,
            use_cuda=False
        )
        self.audio_queue = queue.Queue()
        self.audio_lock = threading.Lock()
        self.is_playing = threading.Event()
        self.buffered_texts = defaultdict(str)
        self.pending_flush_timers = {}
        self.last_token_spoken = None
        self.lock = threading.Lock()

        self.stream_thread = threading.Thread(target=self._audio_loop, daemon=True, name="StreamMouthAudioLoop")
        self.stream_thread.start()

    def _audio_loop(self):
        while True:
            wav_path = self.audio_queue.get()
            if wav_path is None:
                break

            if not os.path.exists(wav_path):
                if DEBUG_SPEAKER:
                    logger.debug(f"File not found: {wav_path}")
                continue

            try:
                with self.audio_lock:
                    self.is_playing.set()
                    EventBus.get_instance().emit("MUTE_EARS", {})
                    self._play_with_pyaudio(wav_path)
            except Exception as e:
                if DEBUG_SPEAKER:
                    logger.debug(f"Playback error: {e}")
            finally:
                self.is_playing.clear()
                EventBus.get_instance().emit("UNMUTE_EARS", {})
                try:
                    os.unlink(wav_path)
                except Exception as cleanup_error:
                    if DEBUG_SPEAKER:
                        logger.debug(f"Failed to delete temp file: {cleanup_error}")

    def _play_with_pyaudio(self, wav_path):
        try:
            audio_array, samplerate = sf.read(wav_path, dtype="float32")
            if audio_array.ndim > 1:
                audio_array = audio_array.mean(axis=1)

            if samplerate != 44100:
                if DEBUG_SPEAKER:
                    logger.debug(f"Resampling from {samplerate} Hz to 44100 Hz")
                resampled = np.interp(
                    np.linspace(0, len(audio_array), int(len(audio_array) * 44100 / samplerate), endpoint=False),
                    np.arange(len(audio_array)),
                    audio_array
                ).astype(np.float32)
            else:
                resampled = audio_array

            pcm_data = (resampled * 32767).astype(np.int16).tobytes()
            p = pyaudio.PyAudio()
            stream = p.open(format=pyaudio.paInt16, channels=1, rate=44100, output=True)
            stream.write(pcm_data)
            stream.stop_stream()
            stream.close()
            p.terminate()

            if DEBUG_SPEAKER:
                logger.debug(f"Finished playing: {wav_path}")

        except Exception as e:
            if DEBUG_SPEAKER:
                logger.debug(f"PyAudio error: {e}")

    def synthesize_and_queue(self, text, token=None):
        try:
            speakable = self._sanitize_for_tts(text)
            if not speakable:
                return

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir="/tmp") as temp_wav:
                wav_path = temp_wav.name

            with wave.open(wav_path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(22050)
                self.voice.synthesize(speakable, wav_file)

            if BROWSER_AUDIO_STREAM:
                try:
                    wav_bytes = Path(wav_path).read_bytes()
                    EventBus.get_instance().emit(
                        "BROWSER_AUDIO_CHUNK",
                        {
                            "token": token,
                            "audio_b64": base64.b64encode(wav_bytes).decode("ascii"),
                            "mime": "audio/wav",
                            "text": speakable,
                        },
                    )
                except Exception as stream_error:
                    if DEBUG_SPEAKER:
                        logger.debug(f"Browser audio emit failed: {stream_error}")

            if DEBUG_SPEAKER:
                logger.debug(f"Synthesized and queued audio: {wav_path}")
            self.audio_queue.put(wav_path)
        except Exception as e:
            if DEBUG_SPEAKER:
                logger.debug(f"Synthesis error: {e}")

    def speak_chunk(self, data):
        text = data.get("chunk", "")
        token = data.get("token")
        if not text or not token:
            return

        to_speak = ""
        with self.lock:
            self.buffered_texts[token] += text
            current = self.buffered_texts[token]

            # New content arrived; cancel any pending soft flush for this token.
            self._cancel_soft_flush_locked(token)

            if self._should_emit_immediate(current):
                to_speak = current.strip()
                self.buffered_texts[token] = ""
            elif self._should_schedule_soft_flush(current):
                self._schedule_soft_flush_locked(token)

        if to_speak:
            if DEBUG_SPEAKER:
                logger.debug(f"Speaking buffered chunk (token={token}): {to_speak}")
            self.synthesize_and_queue(to_speak, token=token)

    def speak_full(self, data):
        text = data.get("text", "")
        token = data.get("token")
        if not text or not token:
            if DEBUG_SPEAKER:
                logger.debug("Missing text or token; ignoring EMIT_SPEAK")
            return

        with self.lock:
            if token == self.last_token_spoken:
                if DEBUG_SPEAKER:
                    logger.debug(f"Duplicate token {token} - ignoring.")
                return
            self.last_token_spoken = token

        if DEBUG_SPEAKER:
            logger.debug(f"Speaking full (token={token}): {text}")
        self.synthesize_and_queue(text, token=token)
        EventBus.get_instance().emit("EMIT_SPEAK_END", {"token": token, "full_text": text})

    def on_speak_end(self, data):
        token = data.get("token")
        if not token:
            return

        trailing = ""
        with self.lock:
            self._cancel_soft_flush_locked(token)
            trailing = self.buffered_texts.pop(token, "").strip()

        # Flush any trailing words so sentence tails are not lost.
        if trailing:
            if DEBUG_SPEAKER:
                logger.debug(f"Flushing trailing chunk (token={token}): {trailing}")
            self.synthesize_and_queue(trailing, token=token)

    def _should_emit(self, text):
        text = text.strip()
        return len(text) >= CHUNK_TRIGGER_LEN or any(text.endswith(c) for c in CHUNK_BREAK_CHARS)

    def _word_count(self, text: str) -> int:
        return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", text or ""))

    def _ends_with_strong_boundary(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if t[-1] in {"?", "!", "\n"}:
            return True
        if not t.endswith("."):
            return False

        lower = t.lower()
        # Avoid chopping on common abbreviations/initialisms.
        if re.search(r"\b(?:e\.g|i\.e|mr|mrs|ms|dr|prof|sr|jr|vs|etc)\.$", lower):
            return False
        if re.search(r"\b[a-z]\.$", lower):
            return False
        return True

    def _ends_with_weak_boundary(self, text: str) -> bool:
        t = (text or "").rstrip()
        return bool(t) and t[-1] in CHUNK_WEAK_BREAK_CHARS

    def _should_emit_immediate(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        wc = self._word_count(t)

        if len(t) >= MAX_CHUNK_CHARS:
            return True
        if self._ends_with_strong_boundary(t):
            return wc >= 3 or len(t) >= CHUNK_TRIGGER_LEN
        if wc >= 18:
            return True
        return False

    def _should_schedule_soft_flush(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        wc = self._word_count(t)
        if self._ends_with_weak_boundary(t):
            return wc >= MIN_WORDS_PER_CHUNK
        if wc >= 10 and len(t) >= CHUNK_TRIGGER_LEN * 2:
            return True
        return False

    def _schedule_soft_flush_locked(self, token: str):
        if token in self.pending_flush_timers:
            return
        timer = threading.Timer(SOFT_FLUSH_SECONDS, self._soft_flush_token, args=(token,))
        timer.daemon = True
        self.pending_flush_timers[token] = timer
        timer.start()

    def _cancel_soft_flush_locked(self, token: str):
        timer = self.pending_flush_timers.pop(token, None)
        if timer:
            timer.cancel()

    def _soft_flush_token(self, token: str):
        to_speak = ""
        with self.lock:
            self.pending_flush_timers.pop(token, None)
            current = self.buffered_texts.get(token, "")
            if not current.strip():
                return
            if self._word_count(current) < MIN_WORDS_PER_CHUNK and len(current.strip()) < CHUNK_TRIGGER_LEN:
                return
            to_speak = current.strip()
            self.buffered_texts[token] = ""

        if to_speak:
            if DEBUG_SPEAKER:
                logger.debug(f"Soft flush chunk (token={token}): {to_speak}")
            self.synthesize_and_queue(to_speak, token=token)

    def _sanitize_for_tts(self, text: str) -> str:
        if not text:
            return ""

        t = str(text)
        # Remove fenced code blocks entirely; they sound terrible read aloud.
        t = re.sub(r"```[\s\S]*?```", " ", t)
        # Convert markdown links to visible label only.
        t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1", t)
        # Drop raw URLs and www links.
        t = re.sub(r"https?://\S+", " ", t)
        t = re.sub(r"\bwww\.\S+", " ", t)
        # Remove markdown emphasis and formatting symbols.
        t = re.sub(r"[`*_#~|<>]+", " ", t)
        # Reduce punctuation noise that gets spelled out in TTS.
        t = re.sub(r"[\\/]{2,}", " ", t)
        t = re.sub(r"\s*/\s*", " ", t)
        t = re.sub(r"\s*[-–—]\s*", ", ", t)
        t = re.sub(r"([!?.,;:])\1+", r"\1", t)
        # Collapse excessive whitespace/newlines.
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def shutdown(self):
        self.audio_queue.put(None)

# === Init + Bindings ===
speaker = StreamMouth()

def start_stream_mouth():
    if DEBUG_SPEAKER:
        logger.debug("Unified streaming TTS module online.")
    bus = EventBus.get_instance()
    bus.subscribe("EMIT_SPEAK", speaker.speak_full)
    bus.subscribe("EMIT_SPEAK_CHUNK", speaker.speak_chunk)
    bus.subscribe("EMIT_SPEAK_END", speaker.on_speak_end)
