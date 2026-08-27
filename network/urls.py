from django.urls import path

from . import views


urlpatterns = [
    # Chat directory and room creation
    path("", views.home, name="chat_home"),
    path("create/", views.create_room, name="create_room"),

    # Individual chat room
    path("room/<str:room_name>/", views.room, name="chat_room"),
    path(
        "room/<str:room_name>/request-join/",
        views.request_join_room,
        name="request_join_room"
    ),

    # Polls
    path(
        "room/<str:room_name>/poll/create/",
        views.create_poll,
        name="create_poll"
    ),
    path("poll/<int:poll_id>/vote/", views.vote_poll, name="vote_poll"),

    # Room admin panel
    path(
        "room/<str:room_name>/manage/",
        views.room_manage,
        name="room_manage"
    ),
    path(
        "room/<str:room_name>/privacy/",
        views.update_room_privacy,
        name="update_room_privacy"
    ),
    path(
        "room/<str:room_name>/add-member/",
        views.admin_add_member,
        name="admin_add_member"
    ),
    path(
        "room/<str:room_name>/member/<int:user_id>/promote/",
        views.promote_room_admin,
        name="promote_room_admin"
    ),
    path(
        "room/<str:room_name>/member/<int:user_id>/remove/",
        views.remove_room_member,
        name="remove_room_member"
    ),
    path(
        "join-request/<int:request_id>/approve/",
        views.approve_join_request,
        name="approve_join_request"
    ),
    path(
        "join-request/<int:request_id>/reject/",
        views.reject_join_request,
        name="reject_join_request"
    ),

    # Standard message actions
    path("message/<int:id>/like/", views.like_message, name="like_message"),
    path(
        "message/<int:id>/pin/",
        views.toggle_pin_message,
        name="toggle_pin_message"
    ),
    path("message/<int:id>/edit/", views.edit_message, name="edit_message"),
    path(
        "message/<int:id>/delete/",
        views.delete_message,
        name="delete_message"
    ),

    # WhatsApp-style message actions
    path(
        "message/<int:id>/forward/",
        views.forward_message,
        name="forward_message"
    ),
    path(
        "message/<int:id>/star/",
        views.toggle_star_message,
        name="toggle_star_message"
    ),

    # WhatsApp-style room controls
    path(
        "room/<str:room_name>/read/",
        views.mark_messages_read,
        name="mark_messages_read"
    ),
    path(
        "room/<str:room_name>/mute/",
        views.set_room_mute,
        name="set_room_mute"
    ),
    path(
        "room/<str:room_name>/archive/",
        views.toggle_archive_room,
        name="toggle_archive_room"
    ),
    path(
        "room/<str:room_name>/disappearing/",
        views.set_disappearing_messages,
        name="set_disappearing_messages"
    ),

    # Typing indicator
    path(
        "room/<str:room_name>/typing/",
        views.set_typing,
        name="set_typing"
    ),
    path(
        "room/<str:room_name>/typing-users/",
        views.typing_users,
        name="typing_users"
    ),
]
