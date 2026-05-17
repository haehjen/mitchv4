import os
import re
from difflib import SequenceMatcher
from core.system_log import get_logger

logger = get_logger("intent_registry")

GENERIC_INTENT_TERMS = {
    "add", "create", "set", "task", "goal", "show", "list", "check", "get", "do",
    "help", "info", "information", "find", "search", "update", "change", "new",
    "make", "tell", "what", "how", "please", "can", "could", "would", "should",
}

STRICT_INTENT_MODULE_ALLOWLIST = {
    part.strip()
    for part in os.getenv(
        "MITCH_INTENT_GUARD_ALLOWLIST",
        (
            "modules.weather_fetcher,modules.web_search,modules.proxmon,"
            "modules.news_digester,modules.task_automator,modules.goal_tracker,"
            "modules.user_prompt_inbox,modules.tarkov_wiki_intent,"
            "modules.youtube_music_skill"
        ),
    ).split(",")
    if part.strip()
}

DISABLE_CONTEXTUAL_INTENTS = os.getenv(
    "MITCH_DISABLE_CONTEXTUAL_INTENTS", "1"
).lower() in {"1", "true", "yes"}

CONTEXTUAL_INTENT_MODULE_ALLOWLIST = {
    part.strip()
    for part in os.getenv("MITCH_CONTEXTUAL_INTENT_ALLOWLIST", "").split(",")
    if part.strip()
}

MIN_INTENT_SCORE = int(os.getenv("MITCH_MIN_INTENT_SCORE", "26"))
INTENT_PRIORITY_WEIGHT = float(os.getenv("MITCH_INTENT_PRIORITY_WEIGHT", "0.6"))
INTENT_PRIORITY_CAP = int(os.getenv("MITCH_INTENT_PRIORITY_CAP", "35"))


def _keyword_is_too_generic(keyword: str) -> bool:
    norm = Intent._normalize_text(keyword)
    if not norm:
        return True
    toks = norm.split()
    if len(toks) == 1 and toks[0] in GENERIC_INTENT_TERMS:
        return True
    if len(toks) <= 2 and all(tok in GENERIC_INTENT_TERMS for tok in toks):
        return True
    return False


class Intent:
    def __init__(self, name, handler, keywords=None, objects=None, priority=0, patterns=None):
        self.name = name
        self.handler = handler
        self.keywords = keywords or []
        self.objects = objects or []
        self.priority = int(priority or 0)
        self.patterns = patterns or []

    def score(self, text):
        text = self._normalize_text(text)
        if not text or not any(c.isalpha() for c in text):
            return -1

        text_tokens = text.split()
        text_token_set = set(text_tokens)
        score = 0

        for pattern in self.patterns:
            try:
                if re.search(pattern, text):
                    score = max(score, 70)
            except re.error:
                logger.warning(f"Invalid intent pattern for '{self.name}': {pattern}")

        for kw in self.keywords:
            norm_kw = self._normalize_text(kw)
            if not norm_kw:
                continue
            kw_tokens = norm_kw.split()
            kw_len = len(kw_tokens)
            kw_score = 0

            if text == norm_kw:
                kw_score = max(kw_score, 90 + (kw_len * 8))

            if re.search(rf"(?<![a-z0-9]){re.escape(norm_kw)}(?![a-z0-9])", text):
                kw_score = max(kw_score, 48 + (kw_len * 6))

            overlap = sum(1 for tok in kw_tokens if tok in text_token_set)
            ratio = (overlap / kw_len) if kw_len else 0.0
            in_order = self._contains_tokens_in_order(text_tokens, kw_tokens)

            if kw_len == 1 and overlap == 1:
                kw_score = max(kw_score, 20)
            elif overlap >= 2 and ratio >= 0.75:
                # Weighted overlap captures human speech with fillers:
                # "play ... on youtube" still maps to keyword "play on youtube".
                overlap_score = 14 + (overlap * 6) + int(ratio * 10)
                if in_order:
                    overlap_score += 6
                kw_score = max(kw_score, overlap_score)

            if kw_len >= 2 and overlap == kw_len and in_order:
                kw_score = max(kw_score, 32 + (kw_len * 5))

            if kw_len >= 2 and len(text_tokens) >= 2:
                fuzzy = self._best_window_similarity(text_tokens, kw_tokens)
                if fuzzy >= 0.90:
                    kw_score = max(kw_score, 34 + (kw_len * 4))
                elif fuzzy >= 0.86:
                    kw_score = max(kw_score, 26 + (kw_len * 3))

            score += kw_score

        return score

    @staticmethod
    def _contains_tokens_in_order(text_tokens, kw_tokens):
        if not kw_tokens:
            return False
        pos = 0
        for tok in kw_tokens:
            found = False
            while pos < len(text_tokens):
                if text_tokens[pos] == tok:
                    found = True
                    pos += 1
                    break
                pos += 1
            if not found:
                return False
        return True

    @staticmethod
    def _best_window_similarity(text_tokens, kw_tokens):
        k = len(kw_tokens)
        if k == 0:
            return 0.0
        if len(text_tokens) < k:
            return 0.0
        kw_joined = " ".join(kw_tokens)
        best = 0.0
        for i in range(0, len(text_tokens) - k + 1):
            win = " ".join(text_tokens[i:i + k])
            sim = SequenceMatcher(a=kw_joined, b=win).ratio()
            if sim > best:
                best = sim
            if best >= 0.98:
                break
        return best

    @staticmethod
    def _normalize_text(text):
        text = str(text or "").lower()
        text = re.sub(r"[_\-/]+", " ", text)
        text = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text)
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text


class IntentRegistry:
    intents = []

    @classmethod
    def register_intent(cls, name, handler, keywords=None, objects=None, priority=0, patterns=None):
        source_module = getattr(handler, "__module__", "unknown")
        keywords = keywords or []

        if (
            DISABLE_CONTEXTUAL_INTENTS
            and source_module.startswith("modules.contextual_")
            and source_module not in CONTEXTUAL_INTENT_MODULE_ALLOWLIST
        ):
            logger.info(
                f"Skipped contextual intent '{name}' from {source_module} (contextual intents disabled)."
            )
            return

        if source_module not in STRICT_INTENT_MODULE_ALLOWLIST:
            filtered = []
            rejected = []
            for kw in keywords:
                kws = str(kw or "").strip()
                if not kws:
                    rejected.append(kws)
                    continue
                if _keyword_is_too_generic(kws):
                    rejected.append(kws)
                    continue
                filtered.append(kws)
            if rejected:
                logger.info(
                    f"Rejected generic keywords for intent '{name}' from {source_module}: {rejected}"
                )
            keywords = filtered

        if not keywords:
            logger.info(
                f"Skipped intent '{name}' from {source_module} (no valid keywords after guard)."
            )
            return

        cls.intents.append(Intent(name, handler, keywords, objects, priority=priority, patterns=patterns))

    @classmethod
    def match_intent(cls, text):
        best_intent = None
        best_rank = None
        best_score = -1
        for intent in cls.intents:
            score = intent.score(text)
            if score <= 0:
                continue
            priority_bonus = int(intent.priority * INTENT_PRIORITY_WEIGHT)
            priority_bonus = max(-INTENT_PRIORITY_CAP, min(INTENT_PRIORITY_CAP, priority_bonus))
            rank = (score + priority_bonus, score, intent.priority)
            if best_rank is None or rank > best_rank:
                best_intent = intent
                best_rank = rank
                best_score = score

        if best_intent is None:
            return None

        if best_score < MIN_INTENT_SCORE:
            return None
        return best_intent

    @classmethod
    def get_intent(cls, name):
        for intent in cls.intents:
            if intent.name == name:
                return intent
        return None


# === Optional example intents (disabled by default) ===
# Keep demo intents opt-in so they never shadow real module handlers.
if os.getenv("MITCH_ENABLE_EXAMPLE_INTENTS", "0").lower() in {"1", "true", "yes"}:
    IntentRegistry.register_intent(
        "launch_drone",
        lambda text: print("Launching drone..."),
        keywords=["launch", "drone"],
        priority=-100,
    )

    IntentRegistry.register_intent(
        "get_weather",
        lambda text: print("Fetching weather..."),
        keywords=["weather", "forecast"],
        priority=-100,
    )
