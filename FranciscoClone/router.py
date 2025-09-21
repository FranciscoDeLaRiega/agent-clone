import re
from typing import Literal
from a2a.server.agent_execution.context import RequestContext

Route = Literal["web", "hash", "vision", "code", "math", "memory", "default"]

_URL_REGEXES = [
    re.compile(r"https?://[^\s]+", re.IGNORECASE),
    re.compile(r"\bwww\.[^\s/]+\.[^\s]+", re.IGNORECASE),
    re.compile(r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?:/[^\s]*)?\b", re.IGNORECASE),
]
_KEYWORD_REGEXES = [re.compile(p, re.IGNORECASE) for p in [
    r"\bgo to\b", r"\bvisit\b", r"\bopen (?:site|page|link)\b", r"\bclick\b",
    r"\bnavigate\b", r"\bfill\b", r"\bsubmit\b", r"\blog ?in\b", r"\bsign ?up\b",
    r"\bplay\b", r"\btic-?tac-?toe\b", r"\badd to cart\b", r"\bcheckout\b",
    r"\bscrape\b", r"\bdownload\b", r"\bupload\b", r"\bgoogle docs\b",
    r"\bsalesforce\b", r"\blinked?in\b", r"\bbrowser\b",
]]

def _is_image_part(part) -> bool:
    try:
        p = getattr(part, "root", part)
        f = getattr(p, "file", None)
        if not f:
            return False
        mime = getattr(f, "mime_type", None) or getattr(f, "mimeType", None)
        name = getattr(f, "name", None) or getattr(f, "filename", None)
        has_bytes = getattr(f, "bytes", None) is not None
        # accept if mime is image/* OR filename looks like an image OR we have bytes
        if isinstance(mime, str) and mime.startswith("image/"):
            return True
        if isinstance(name, str) and name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
            return True
        return bool(has_bytes)
    except Exception:
        return False

def route_request(ctx: RequestContext) -> Route:
    text = (ctx.get_user_input() or "").lower()

    #Vision
    try:
        if ctx.message and ctx.message.parts:
            for p in ctx.message.parts:
                kind = getattr(p, "kind", None) or getattr(p, "type", None)
                if (kind == "file") and _is_image_part(p):
                    return "vision"
    except Exception:
        pass

    #Web
    if any(rx.search(text) for rx in _KEYWORD_REGEXES) or any(rx.search(text) for rx in _URL_REGEXES):
        return "web"

    #Hash
    if re.search(r"\b(md5|sha512)\b", text):
        return "hash"

    #Math
    if re.search(r"\d", text) and re.search(r"[+\-*/^%()]", text):
        return "math"

    #Code
    if re.search(r"\b(code|python|script|algorithm|function)\b", text):
        return "code"

    #Memory
    if re.search(r"\b(remember|memory note|save this)\b", text):
        return "memory"

    return "default"
