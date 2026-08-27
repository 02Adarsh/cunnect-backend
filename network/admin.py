from django.contrib import admin

from .models import (
    ChatRoom,
    RoomJoinRequest,
    Message,
    Reaction,
    UserStatus,
    TypingStatus,
)


@admin.register(ChatRoom)
class ChatRoomAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "privacy",
        "created_by",
        "member_count",
        "created_at",
    )

    list_filter = (
        "privacy",
        "created_at",
    )

    search_fields = (
        "name",
        "description",
        "created_by__username",
    )

    filter_horizontal = (
        "members",
        "admins",
    )

    def member_count(self, obj):
        return obj.members.count()

    member_count.short_description = "Members"


@admin.register(RoomJoinRequest)
class RoomJoinRequestAdmin(admin.ModelAdmin):
    list_display = (
        "room",
        "user",
        "status",
        "requested_at",
        "reviewed_by",
    )

    list_filter = (
        "status",
        "room",
    )

    search_fields = (
        "room__name",
        "user__username",
    )

    actions = (
        "approve_requests",
        "reject_requests",
    )

    @admin.action(
        description="Approve selected join requests"
    )
    def approve_requests(self, request, queryset):
        for join_request in queryset.filter(
            status="pending"
        ):
            join_request.room.members.add(
                join_request.user
            )

            join_request.status = "approved"
            join_request.reviewed_by = request.user
            join_request.notified = False

            join_request.save(
                update_fields=[
                    "status",
                    "reviewed_by",
                    "notified",
                ]
            )

        self.message_user(
            request,
            "Selected join requests approved."
        )

    @admin.action(
        description="Reject selected join requests"
    )
    def reject_requests(self, request, queryset):
        queryset.filter(
            status="pending"
        ).update(
            status="rejected",
            reviewed_by=request.user
        )

        self.message_user(
            request,
            "Selected join requests rejected."
        )


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "room",
        "user",
        "content",
        "created_at",
        "edited",
    )

    list_filter = (
        "room",
        "edited",
        "created_at",
    )

    search_fields = (
        "content",
        "user__username",
        "room__name",
    )


@admin.register(Reaction)
class ReactionAdmin(admin.ModelAdmin):
    list_display = (
        "message",
        "user",
        "emoji",
        "created_at",
    )

    list_filter = (
        "emoji",
        "created_at",
    )


@admin.register(UserStatus)
class UserStatusAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "online",
        "last_seen",
    )

    list_filter = (
        "online",
    )

    search_fields = (
        "user__username",
    )


@admin.register(TypingStatus)
class TypingStatusAdmin(admin.ModelAdmin):
    list_display = (
        "room",
        "user",
        "is_typing",
        "updated_at",
    )

    list_filter = (
        "room",
        "is_typing",
    )

    search_fields = (
        "room__name",
        "user__username",
    )