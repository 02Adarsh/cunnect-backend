"""api_app.security — central security layer (v65).

1. Sliding-window rate limiter (per-IP + per-target) — stops brute force
   and bot floods on auth endpoints without touching normal app traffic.
2. Upload validation — size caps + image/video type allowlists.
3. Input sanitising helpers (user_id format, HTML escaping is done at
   the call sites with django.utils.html.escape).

In-memory by design: the server runs as a SINGLE instance (in-memory UMS
sessions already require that), so no Redis is needed.
"""

import threading
import time
from collections import defaultdict, deque
from functools import wraps

from django.http import JsonResponse

# ---------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------

_BUCKETS = defaultdict(deque)   # key -> deque[timestamps]
_LOCK = threading.Lock()
_MAX_BUCKETS = 60000            # hard memory bound under botnet floods


def client_ip(request):
    """Real client IP behind the Render proxy (first X-Forwarded-For)."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return request.META.get("REMOTE_ADDR", "?")


def allow(key, limit, window):
    """True if this key may act again (sliding window of `window` sec)."""
    now = time.time()
    with _LOCK:
        if len(_BUCKETS) > _MAX_BUCKETS:
            # Bots created too many keys — drop the stalest half so the
            # process memory stays bounded (worst case: some limits reset).
            cutoff = now - 3600
            for k in list(_BUCKETS.keys()):
                q = _BUCKETS[k]
                if not q or q[-1] < cutoff:
                    _BUCKETS.pop(k, None)
            if len(_BUCKETS) > _MAX_BUCKETS:
                for k in list(_BUCKETS.keys())[: _MAX_BUCKETS // 2]:
                    _BUCKETS.pop(k, None)
        q = _BUCKETS[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def retry_after(key, window):
    with _LOCK:
        q = _BUCKETS.get(key)
        if not q:
            return 0
        return max(0, int(window - (time.time() - q[0])) + 1)


def _too_many(seconds=60):
    resp = JsonResponse(
        {"ok": False,
         "error": "Too many attempts. Please wait a moment and try again."},
        status=429)
    resp["Retry-After"] = str(max(1, seconds))
    return resp


def throttle(scope, limit, window, body_field=None,
             target_limit=None, target_window=None):
    """Decorator: per-IP limit, plus optional per-target limit keyed on a
    JSON body field (uid/username/phone/email) so a botnet spread across
    many IPs still cannot hammer ONE account."""

    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            ip_key = f"{scope}:ip:{client_ip(request)}"
            if not allow(ip_key, limit, window):
                return _too_many(retry_after(ip_key, window))
            if body_field:
                try:
                    import json
                    body = json.loads(request.body.decode("utf-8") or "{}")
                except Exception:
                    body = {}
                val = str(body.get(body_field, "")).strip().lower()[:80]
                if val:
                    t_limit = target_limit or limit
                    t_window = target_window or window
                    t_key = f"{scope}:tgt:{val}"
                    if not allow(t_key, t_limit, t_window):
                        return _too_many(retry_after(t_key, t_window))
            return view(request, *args, **kwargs)

        return wrapper

    return deco


# ---------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".3gp", ".mkv", ".avi"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
            ".txt", ".rtf", ".odt", ".jpg", ".jpeg", ".png", ".webp"}
# Never allowed anywhere (server/client execution risk):
BLOCKED_EXTS = {".exe", ".bat", ".cmd", ".sh", ".ps1", ".msi", ".apk",
                ".js", ".jar", ".py", ".php", ".dll", ".com", ".scr",
                ".vbs", ".html", ".htm", ".svg"}

MB = 1024 * 1024


def _ext(name):
    name = (name or "").lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def check_upload(f, kind="image", max_mb=8):
    """Returns an error string, or None if the file is acceptable.
    kind: 'image' | 'video' | 'media' (image or video) | 'doc' | 'any'."""
    if f is None:
        return "No file sent."
    size = getattr(f, "size", 0) or 0
    if size > max_mb * MB:
        return f"File too large — keep it under {max_mb} MB."
    if size <= 0:
        return "The file is empty."
    ext = _ext(getattr(f, "name", ""))
    if ext in BLOCKED_EXTS:
        return "This file type is not allowed."
    if kind == "image" and ext not in IMAGE_EXTS:
        return "Please upload an image (jpg / png / webp)."
    if kind == "video" and ext not in VIDEO_EXTS:
        return "Please upload a video (mp4 / mov / webm)."
    if kind == "media" and ext not in (IMAGE_EXTS | VIDEO_EXTS):
        return "Please upload an image or a video."
    if kind == "doc" and ext not in DOC_EXTS:
        return "Please upload a document (pdf / doc / ppt / image)."
    return None


# ---------------------------------------------------------------------
# Input sanitising
# ---------------------------------------------------------------------

import re as _re

_USER_ID_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,29}$")


def valid_user_id(uid):
    """College UIDs: letters/digits (dots, dash, underscore inside),
    3-30 chars. Blocks <script>, spaces, path tricks, emoji floods."""
    return bool(_USER_ID_RE.match(uid or ""))


def clean_name(name, max_len=60):
    """Displayable name: strip control chars + HTML brackets."""
    name = _re.sub(r"[<>\x00-\x1f\x7f]", "", str(name or "")).strip()
    return name[:max_len]
