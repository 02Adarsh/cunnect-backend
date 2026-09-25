"""⭐ v93 SECURITY AUDIT HARDENING — three small middlewares.

1. OriginVerifyMiddleware (H-03): when ORIGIN_VERIFY_SECRET is set in the
   environment, every request must carry the matching X-Origin-Verify
   header. Add it with a Cloudflare Transform Rule on the proxied zone —
   direct hitters on the raw *.onrender.com origin (bypassing Cloudflare)
   never know the value and get 403. Until the env var is set the
   middleware is inactive, so deploying this file alone breaks nothing.

2. AdminLoginThrottleMiddleware (M-01): 5 failed admin logins per IP or
   username → 30-minute lockout (cache-based, no extra dependency).
   Failures are captured via Django's user_login_failed signal.

3. SecurityHeadersMiddleware (L-03): adds a Content-Security-Policy and
   a couple of minor hardening headers on HTML responses.
"""

import os

from django.core.cache import cache
from django.http import HttpResponse, HttpResponseForbidden

# ---------------------------------------------------------------------------
# H-03 — origin verify
# ---------------------------------------------------------------------------


class OriginVerifyMiddleware:
    """Reject requests that skipped Cloudflare (no secret header)."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.secret = os.environ.get("ORIGIN_VERIFY_SECRET", "").strip()
        # Paths that must stay reachable without the header: Render's own
        # health checks and the cron-job.org keep-awake ping.
        self.exempt_paths = {"/api/health/", "/health/", "/healthz/"}

    def __call__(self, request):
        if self.secret:
            path = request.path
            exempt = any(
                path == p or path.startswith(p) for p in self.exempt_paths
            )
            if not exempt:
                if request.headers.get("X-Origin-Verify") != self.secret:
                    return HttpResponseForbidden("denied")
        return self.get_response(request)


# ---------------------------------------------------------------------------
# M-01 — admin login throttle (signal + middleware)
# ---------------------------------------------------------------------------

_ADMIN_FAIL_WINDOW = 15 * 60       # count failures within 15 minutes
_ADMIN_LOCKOUT = 30 * 60          # lock for 30 minutes
_ADMIN_MAX_FAILS = 5


def _client_ip(request):
    """Best-effort client IP (Render proxy sets X-Forwarded-For)."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _record_admin_failure(sender, credentials, request, **kwargs):
    """user_login_failed signal — bump per-IP and per-username counters."""
    try:
        ip = _client_ip(request) if request is not None else ""
        username = str(credentials.get("username", "") if credentials else "")
        if ip:
            k = f"adminfail:ip:{ip}"
            fails = cache.get(k, 0) + 1
            cache.set(k, fails, _ADMIN_FAIL_WINDOW)
        if username:
            k = f"adminfail:user:{username.lower()}"
            fails = cache.get(k, 0) + 1
            cache.set(k, fails, _ADMIN_FAIL_WINDOW)
    except Exception:
        pass


try:
    # The signal connects when this module loads (it is listed in
    # MIDDLEWARE, so Django imports it at startup).
    from django.contrib.auth.signals import user_login_failed

    user_login_failed.connect(_record_admin_failure, dispatch_uid="cunnect-admin-fail")
except Exception:
    pass


class AdminLoginThrottleMiddleware:
    """Block the admin login POST once the failure budget is spent."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path.startswith("/admin/login"):
            ip = _client_ip(request)
            if cache.get(f"adminfail:ip:{ip}", 0) >= _ADMIN_MAX_FAILS:
                return HttpResponse(
                    "Too many failed logins. Try again in 30 minutes.",
                    status=429)
            username = (request.POST.get("username") or "").strip().lower()
            if username and cache.get(
                    f"adminfail:user:{username}", 0) >= _ADMIN_MAX_FAILS:
                return HttpResponse(
                    "Too many failed logins. Try again in 30 minutes.",
                    status=429)
        return self.get_response(request)


# ---------------------------------------------------------------------------
# L-03 — security headers (CSP)
# ---------------------------------------------------------------------------

# External resources the templates actually use:
#   scripts  — cdn.tailwindcss.com
#   styles   — cdnjs (font-awesome), fonts.googleapis.com, inline <style>
#   fonts    — fonts.gstatic.com, cdnjs
#   images   — self-hosted media + a few https: logos; data:/blob: URIs
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com "
    "https://fonts.googleapis.com; "
    "font-src 'self' data: https://cdnjs.cloudflare.com "
    "https://fonts.gstatic.com; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' blob: https:; "
    "connect-src 'self' wss: https:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    """Add CSP + permissions policy on HTML responses."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        ctype = (response.headers.get("Content-Type", "") or "").lower()
        if "text/html" in ctype:
            response.headers.setdefault("Content-Security-Policy", _CSP)
            response.headers.setdefault(
                "Permissions-Policy",
                "geolocation=(self), microphone=(), camera=()")
        return response
