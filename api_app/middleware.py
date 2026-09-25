"""CORS for the browser builds (Flutter web) — ⭐ v93 security audit:
was a blanket ``Access-Control-Allow-Origin: *`` on EVERY response, which
turned the whole site into a wildcard API for any origin. Now the caller's
Origin is echoed back only when it is on the allowlist (our own domains +
local dev). Native apps (Android APK / iOS) never send Origin headers and
are unaffected either way."""

# Own domains + local Flutter-web dev servers.
_ALLOWED_ORIGINS = {
    "https://cunnect.online",
    "https://www.cunnect.online",
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5000",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5000",
    "http://127.0.0.1:8000",
}


class SimpleCorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = (request.headers.get("Origin") or "").strip()
        allowed = origin in _ALLOWED_ORIGINS

        if request.method == "OPTIONS" and allowed:
            from django.http import HttpResponse

            response = HttpResponse("")
        else:
            response = self.get_response(request)

        if allowed:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            )
            response["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Accept"
            )
            response["Access-Control-Max-Age"] = "86400"
            response["Vary"] = "Origin"
        return response
