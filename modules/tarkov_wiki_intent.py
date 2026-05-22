import re
from html import unescape
from urllib.parse import unquote

import requests
from ddgs import DDGS

from core.event_bus import event_bus
from core.intent_registry import IntentRegistry
from core.system_log import get_logger

logger = get_logger("tarkov_wiki_intent")

EVENT_NAME = "EMIT_TARKOV_WIKI_QUERY"
WIKI_DOMAIN = "escapefromtarkov.fandom.com"
SEARCH_TIMEOUT_SECONDS = 10
PAGE_TIMEOUT_SECONDS = 10
MAX_SNIPPET_CHARS = 1800
MAX_QUERY_TERMS = 8

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "can", "do", "for", "from", "how",
    "i", "in", "is", "it", "me", "of", "on", "or", "tell", "the", "to", "what",
    "when", "where", "which", "who", "why", "with", "you", "about", "please",
}


def _clean_query(text: str) -> str:
    cleaned = re.sub(
        r"\b(escape from tarkov|eft|tarkov|wiki|please|can you|could you|tell me|what is|what's|web search|web page|page)\b",
        " ",
        text,
        flags=re.I,
    )
    cleaned = re.sub(r"[^\w\s\-']", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or text.strip()


def _extract_terms(query: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'\-]+", query.lower())
    terms = [w for w in words if len(w) > 2 and w not in STOPWORDS]
    return terms[:MAX_QUERY_TERMS]


def _search_wiki(query: str) -> dict | None:
    # Prefer the wiki's own search API. The old web-search-first path could
    # return a valid Tarkov page that was nonetheless unrelated to the request.
    try:
        api_resp = requests.get(
            f"https://{WIKI_DOMAIN}/api.php",
            timeout=SEARCH_TIMEOUT_SECONDS,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
            },
            headers={"User-Agent": "Mitch/4.0 (+TarkovWikiIntent)"},
        )
        api_resp.raise_for_status()
        api_payload = api_resp.json()
        hits = (((api_payload or {}).get("query") or {}).get("search") or [])
        if hits:
            top = hits[0] or {}
            title = str(top.get("title") or "").strip()
            if title:
                slug = title.replace(" ", "_")
                return {
                    "title": title,
                    "href": f"https://{WIKI_DOMAIN}/wiki/{slug}",
                    "body": str(top.get("snippet") or ""),
                }
    except Exception as e:
        logger.warning(f"Tarkov wiki API search failed: {e}")

    search_query = f"site:{WIKI_DOMAIN} {query}".strip()
    logger.info(f"Tarkov wiki search query: {search_query}")
    terms = _extract_terms(query)
    try:
        with DDGS() as ddgs:
            best_result = None
            best_score = -1
            for result in ddgs.text(search_query, max_results=8):
                result = result or {}
                href = (result.get("href") or result.get("url") or "").strip()
                if WIKI_DOMAIN not in href:
                    continue
                title = str(result.get("title") or "")
                body = str(result.get("body") or "")
                haystack = f"{title} {body} {href}".lower()
                score = sum(1 for term in terms if term in haystack)
                # Bias toward exact quest-page URLs like /wiki/Operation_Aquarius_-_Part_1.
                if "/wiki/" in href:
                    score += 2
                if score > best_score:
                    best_score = score
                    best_result = result
            if best_result:
                return best_result
    except Exception as e:
        logger.warning(f"Tarkov wiki search failed: {e}")
    return None


def _strip_html_to_text(html: str) -> str:
    no_script = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    no_style = re.sub(r"<style[\s\S]*?</style>", " ", no_script, flags=re.I)
    no_tags = re.sub(r"<[^>]+>", " ", no_style)
    text = unescape(no_tags)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_wikitext_markup(text: str) -> str:
    # Basic MediaWiki markup cleanup for readable prompt text.
    text = re.sub(r"\{\{[^{}]*\}\}", " ", text)  # templates
    text = re.sub(r"<ref[^>]*>[\s\S]*?</ref>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)  # [[A|B]] -> B
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)  # [[A]] -> A
    text = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", text)  # [url text] -> text
    text = re.sub(r"''+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_lines_from_html_block(html: str, max_items: int = 24) -> list[str]:
    lines: list[str] = []
    if not html:
        return lines

    # Prefer list items and table cells for quest requirements/objectives.
    for snippet in re.findall(r"<li[^>]*>([\s\S]*?)</li>", html, flags=re.I):
        clean = _strip_html_to_text(snippet)
        if clean and len(clean) > 4:
            lines.append(clean)
            if len(lines) >= max_items:
                return lines

    for snippet in re.findall(r"<td[^>]*>([\s\S]*?)</td>", html, flags=re.I):
        clean = _strip_html_to_text(snippet)
        if clean and len(clean) > 4:
            lines.append(clean)
            if len(lines) >= max_items:
                return lines

    # Fallback to paragraph text chunks if list/table extraction is empty.
    if not lines:
        for snippet in re.findall(r"<p[^>]*>([\s\S]*?)</p>", html, flags=re.I):
            clean = _strip_html_to_text(snippet)
            if clean and len(clean) > 20:
                lines.append(clean)
                if len(lines) >= max_items:
                    return lines
    return lines


def _extract_page_title_from_url(url: str) -> str:
    try:
        marker = "/wiki/"
        idx = url.find(marker)
        if idx == -1:
            return ""
        slug = url[idx + len(marker):].split("?", 1)[0].split("#", 1)[0].strip()
        return unquote(slug)
    except Exception:
        return ""


def _fetch_wiki_excerpt_from_api(url: str) -> str:
    title = _extract_page_title_from_url(url)
    if not title:
        return ""

    api_url = f"https://{WIKI_DOMAIN}/api.php"
    try:
        sections_resp = requests.get(
            api_url,
            timeout=PAGE_TIMEOUT_SECONDS,
            params={
                "action": "parse",
                "page": title,
                "prop": "sections",
                "format": "json",
                "formatversion": "2",
            },
            headers={"User-Agent": "Mitch/1.0 (+TarkovWikiIntent)"},
        )
        sections_resp.raise_for_status()
        sections_payload = sections_resp.json()
    except Exception as e:
        logger.warning(f"Wiki API sections fetch failed for '{title}': {e}")
        sections_payload = {}

    # Target likely quest sections first.
    target_indexes: list[str] = []
    sections = ((sections_payload or {}).get("parse") or {}).get("sections") or []
    for section in sections:
        line = str((section or {}).get("line", "")).strip().lower()
        index = str((section or {}).get("index", "")).strip()
        if not index:
            continue
        if line in {"requirements", "objectives", "guide", "walkthrough"}:
            target_indexes.append(index)

    extracted_lines: list[str] = []
    for index in target_indexes[:4]:
        try:
            sec_resp = requests.get(
                api_url,
                timeout=PAGE_TIMEOUT_SECONDS,
                params={
                    "action": "parse",
                    "page": title,
                    "prop": "text",
                    "section": index,
                    "format": "json",
                    "formatversion": "2",
                },
                headers={"User-Agent": "Mitch/1.0 (+TarkovWikiIntent)"},
            )
            sec_resp.raise_for_status()
            sec_payload = sec_resp.json()
            html = (((sec_payload or {}).get("parse") or {}).get("text") or ""
)
            extracted_lines.extend(_extract_lines_from_html_block(html, max_items=16))
        except Exception as e:
            logger.warning(f"Wiki API section fetch failed for '{title}' section {index}: {e}")

    # Fallback: parse full rendered page HTML and pull content lines.
    if not extracted_lines:
        try:
            full_resp = requests.get(
                api_url,
                timeout=PAGE_TIMEOUT_SECONDS,
                params={
                    "action": "parse",
                    "page": title,
                    "prop": "text",
                    "format": "json",
                    "formatversion": "2",
                },
                headers={"User-Agent": "Mitch/1.0 (+TarkovWikiIntent)"},
            )
            full_resp.raise_for_status()
            full_payload = full_resp.json()
            full_html = (((full_payload or {}).get("parse") or {}).get("text") or "")
            extracted_lines = _extract_lines_from_html_block(full_html, max_items=24)
        except Exception as e:
            logger.warning(f"Wiki API full-page fetch failed for '{title}': {e}")

    # De-duplicate while preserving order.
    unique_lines = []
    seen = set()
    for line in extracted_lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_lines.append(line)

    if unique_lines:
        return " | ".join(unique_lines)[:MAX_SNIPPET_CHARS]

    # Last fallback to raw wikitext.
    try:
        raw_resp = requests.get(
            api_url,
            timeout=PAGE_TIMEOUT_SECONDS,
            params={
                "action": "parse",
                "page": title,
                "prop": "wikitext",
                "format": "json",
                "formatversion": "2",
            },
            headers={"User-Agent": "Mitch/1.0 (+TarkovWikiIntent)"},
        )
        raw_resp.raise_for_status()
        raw_payload = raw_resp.json()
        wikitext = (((raw_payload or {}).get("parse") or {}).get("wikitext") or "")
        if wikitext:
            return _strip_wikitext_markup(wikitext[:MAX_SNIPPET_CHARS])[:MAX_SNIPPET_CHARS]
    except Exception as e:
        logger.warning(f"Wiki API wikitext fallback failed for '{title}': {e}")

    return ""


def _extract_objectives_section(html: str) -> str:
    # Prefer the quest objective block from the page before generic text scoring.
    try:
        m = re.search(
            r"(<h2[^>]*>[\s\S]*?Objectives[\s\S]*?</h2>)([\s\S]*?)(<h2[^>]*>|$)",
            html,
            flags=re.I,
        )
        if not m:
            return ""
        block = m.group(2)
        # Keep list/table content where quest requirements usually appear.
        list_items = re.findall(r"<li[^>]*>([\s\S]*?)</li>", block, flags=re.I)
        if list_items:
            text_items = []
            for item in list_items[:12]:
                clean = _strip_html_to_text(item)
                if clean:
                    text_items.append(clean)
            if text_items:
                return " | ".join(text_items)[:MAX_SNIPPET_CHARS]

        table_cells = re.findall(r"<td[^>]*>([\s\S]*?)</td>", block, flags=re.I)
        if table_cells:
            text_cells = []
            for cell in table_cells[:20]:
                clean = _strip_html_to_text(cell)
                if clean:
                    text_cells.append(clean)
            if text_cells:
                return " | ".join(text_cells)[:MAX_SNIPPET_CHARS]
    except Exception as e:
        logger.warning(f"Failed to parse objectives section: {e}")
    return ""


def _extract_relevant_excerpt(text: str, query_terms: list[str]) -> str:
    if not text:
        return ""
    if not query_terms:
        return text[:MAX_SNIPPET_CHARS]

    # Score sentence fragments by term overlap and keep the best few.
    parts = re.split(r"(?<=[\.\!\?])\s+", text)
    scored = []
    for part in parts:
        lower = part.lower()
        score = sum(1 for term in query_terms if term in lower)
        if score > 0:
            scored.append((score, part))
    scored.sort(key=lambda item: item[0], reverse=True)

    selected = []
    total = 0
    for _, fragment in scored[:10]:
        fragment = fragment.strip()
        if not fragment:
            continue
        if total + len(fragment) + 1 > MAX_SNIPPET_CHARS:
            break
        selected.append(fragment)
        total += len(fragment) + 1

    if not selected:
        return text[:MAX_SNIPPET_CHARS]
    return " ".join(selected)[:MAX_SNIPPET_CHARS]


def _fetch_wiki_excerpt(url: str, query: str) -> str:
    api_excerpt = _fetch_wiki_excerpt_from_api(url)
    if api_excerpt:
        return api_excerpt[:MAX_SNIPPET_CHARS]

    try:
        resp = requests.get(
            url,
            timeout=PAGE_TIMEOUT_SECONDS,
            headers={"User-Agent": "Mitch/1.0 (+TarkovWikiIntent)"},
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Failed to fetch wiki page: {e}")
        return ""

    objectives = _extract_objectives_section(resp.text)
    if objectives:
        return objectives[:MAX_SNIPPET_CHARS]

    text = _strip_html_to_text(resp.text)
    terms = _extract_terms(query)
    excerpt = _extract_relevant_excerpt(text, terms)
    return excerpt


def _emit_augmented_chat(prompt: str, context: str = "") -> None:
    payload = {"prompt": prompt, "force_openai_minimal": True, "request_type": "tarkov_rag"}
    if context.strip():
        payload["retrieved_context"] = context
    event_bus.emit("EMIT_CHAT_REQUEST", payload)


def handle_tarkov_wiki_query(data):
    if isinstance(data, dict):
        prompt = (data.get("prompt") or "").strip()
    elif isinstance(data, str):
        prompt = data.strip()
    else:
        prompt = ""
    if not prompt:
        return

    query = _clean_query(prompt)
    result = _search_wiki(query)
    if not result:
        logger.info("Tarkov wiki: no result; continuing without retrieval.")
        _emit_augmented_chat(prompt)
        return

    url = result.get("href") or result.get("url") or ""
    title = result.get("title", "Tarkov Wiki")
    if not url:
        logger.info("Tarkov wiki: result missing URL; continuing without retrieval.")
        _emit_augmented_chat(prompt)
        return

    excerpt = _fetch_wiki_excerpt(url, query)
    if not excerpt:
        body = (result.get("body") or "").strip()
        excerpt = body[:MAX_SNIPPET_CHARS]

    if not excerpt:
        logger.info("Tarkov wiki: no usable excerpt; continuing without retrieval.")
        _emit_augmented_chat(prompt)
        return

    retrieved_context = (
        "Tarkov wiki retrieval:\n"
        f"Source: {title}\n"
        f"URL: {url}\n"
        f"Excerpt: {excerpt}"
    )
    logger.info(
        f"Tarkov wiki retrieval ok: url={url}, excerpt_chars={len(excerpt)}, prompt_chars={len(prompt)}"
    )
    # Restore legacy behavior: open the retrieved page in the visual workspace iframe.
    event_bus.emit("EMIT_OPEN_URL", {"url": url, "source": "tarkov_wiki_intent"})
    _emit_augmented_chat(prompt, context=retrieved_context)


def _tarkov_intent_handler(text: str):
    event_bus.emit(EVENT_NAME, {"prompt": text, "intent": "tarkov_wiki_lookup"})


def start_module(event_bus):
    event_bus.subscribe(EVENT_NAME, handle_tarkov_wiki_query)
    IntentRegistry.register_intent(
        name="tarkov_wiki_lookup",
        handler=_tarkov_intent_handler,
        keywords=[
            "tarkov",
            "escape from tarkov",
            "eft",
            "prapor",
            "therapist",
            "jaeger",
            "mechanic",
            "ragman",
            "peacekeeper",
            "kappa",
        ],
        objects=[],
        priority=50,
    )
    logger.info("Tarkov wiki intent module started.")
