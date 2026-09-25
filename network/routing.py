from django.urls import re_path

from channels.generic.websocket import AsyncWebsocketConsumer

from .consumers import ChatConsumer, DirectoryConsumer


class _WsNotFound(AsyncWebsocketConsumer):
    """⭐ v93 security audit (L-03): unknown /ws/* paths used to raise a
    500 inside Channels. This catch-all accepts the handshake and closes
    it immediately with 4404 (custom "not found") — no traceback, no log
    spam, and the route stays unauthenticated-free."""

    async def connect(self):
        await self.close(code=4404)


websocket_urlpatterns = [
    re_path(
        r"ws/chat-directory/$",
        DirectoryConsumer.as_asgi()
    ),
    re_path(
        r"ws/chat/(?P<room_name>[^/]+)/$",
        ChatConsumer.as_asgi()
    ),
    # ⭐ v93: catch-all — must stay LAST.
    re_path(r"ws/.*", _WsNotFound.as_asgi()),
]
