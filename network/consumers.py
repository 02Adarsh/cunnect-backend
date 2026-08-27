from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import ChatRoom, UserStatus


class ChatConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.room_name = self.scope["url_route"]["kwargs"]["room_name"]
        self.user = self.scope["user"]

        if not self.user.is_authenticated:
            await self.close(code=4401)
            return

        room = await self.get_room(self.room_name)
        if not room:
            await self.close(code=4404)
            return

        self.room_id = room.id
        self.room_group_name = f"chat_room_{self.room_id}"

        await self.add_member(room.id, self.user.id)
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

        count = await self.set_online(room.id, self.user.id, True)
        await self.channel_layer.group_send(
            self.room_group_name,
            {"type": "online.status", "count": count}
        )

    async def disconnect(self, close_code):
        if not hasattr(self, "room_group_name"):
            return

        count = await self.set_online(self.room_id, self.user.id, False)
        await self.channel_layer.group_send(
            self.room_group_name,
            {"type": "online.status", "count": count}
        )
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def receive_json(self, content, **kwargs):
        if content.get("type") == "typing":
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat.typing",
                    "username": self.user.username,
                    "full_name": self.user.get_full_name() or self.user.username,
                    "is_typing": bool(content.get("is_typing")),
                }
            )

    async def chat_message(self, event):
        await self.send_json({
            "type": "message",
            "message": event["message"],
        })

    async def chat_reaction(self, event):
        await self.send_json({
            "type": "reaction",
            "message_id": event["message_id"],
            "count": event["count"],
        })

    async def chat_message_edit(self, event):
        await self.send_json({
            "type": "message_edit",
            "message_id": event["message_id"],
            "content": event["content"],
            "deleted_for_everyone": event.get("deleted_for_everyone", False),
        })

    async def chat_message_delete(self, event):
        await self.send_json({
            "type": "message_delete",
            "message_id": event["message_id"],
        })

    async def chat_poll_update(self, event):
        await self.send_json({
            "type": "poll_update",
            "poll": event["poll"],
        })

    async def chat_pinned_update(self, event):
        await self.send_json({
            "type": "pinned_update",
            "pinned_message": event["pinned_message"],
        })

    async def chat_typing(self, event):
        await self.send_json({
            "type": "typing",
            "username": event["username"],
            "full_name": event["full_name"],
            "is_typing": event["is_typing"],
        })

    async def online_status(self, event):
        await self.send_json({
            "type": "online", "count": event["count"]
        })

    @database_sync_to_async
    def get_room(self, room_name):
        try:
            return ChatRoom.objects.get(name=room_name)
        except ChatRoom.DoesNotExist:
            return None

    @database_sync_to_async
    def add_member(self, room_id, user_id):
        room = ChatRoom.objects.get(id=room_id)
        room.members.add(user_id)

    @database_sync_to_async
    def set_online(self, room_id, user_id, online):
        status, _ = UserStatus.objects.get_or_create(user_id=user_id)
        status.online = online
        status.save(update_fields=["online", "last_seen"])

        room = ChatRoom.objects.get(id=room_id)
        return room.members.filter(userstatus__online=True).count()


class DirectoryConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        if not self.scope["user"].is_authenticated:
            await self.close(code=4401)
            return

        self.group_name = "chat_directory"
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name
            )

    async def directory_room_created(self, event):
        await self.send_json({
            "type": "room_created",
            "room": event["room"],
        })
