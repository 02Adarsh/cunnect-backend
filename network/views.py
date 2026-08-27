from datetime import timedelta
import os
import re

from django.db import OperationalError
from django.db.models import Q

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from myapp.models import UserProfile

from .models import (
    ChatRoom,
    RoomJoinRequest,
    Message,
    Reaction,
    Poll,
    PollOption,
    PollVote,
    TypingStatus,
    RoomMemberSettings,
    MessageReceipt,
    MessageMention,
)


def room_is_admin(room, user):
    return room.is_admin(user)


def serialize_room(room, user):
    is_member = room.members.filter(id=user.id).exists()
    is_admin = room.is_admin(user)
    pending = RoomJoinRequest.objects.filter(
        room=room,
        user=user,
        status="pending"
    ).exists()

    return {
        "name": room.name,
        "privacy": room.privacy,
        "description": room.description,
        "members_count": room.members.count(),
        "is_member": is_member,
        "is_admin": is_admin,
        "pending": pending,
        "room_url": reverse("chat_room", kwargs={"room_name": room.name}),
        "request_url": reverse("request_join_room", kwargs={"room_name": room.name}),
    }


def broadcast_room_created(room):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "chat_directory",
        {
            "type": "directory.room_created",
            "room": {
                "name": room.name,
                "privacy": room.privacy,
                "description": room.description,
                "members_count": room.members.count(),
                "room_url": reverse("chat_room", kwargs={"room_name": room.name}),
                "request_url": reverse("request_join_room", kwargs={"room_name": room.name}),
            },
        }
    )


def get_profile_photo_url(user):
    try:
        profile = user.userprofile
        return profile.profile_photo.url if profile.profile_photo else ""
    except Exception:
        return ""


def get_user_course(user):
    """Branch/course is saved in UserProfile during profile completion."""
    try:
        profile = user.userprofile
        course = (profile.branch or "").strip()
        year = (profile.year or "").strip()

        if course and year:
            return f"{course} · {year}"
        return course or "Campus Member"
    except Exception:
        return "Campus Member"


def message_expiry_for_room(room):
    if not room.disappearing_after:
        return None
    return timezone.now() + timedelta(seconds=room.disappearing_after)


def create_message_receipts(message):
    """Create one receipt for every recipient currently in the room."""
    recipient_ids = list(
        message.room.members.exclude(id=message.user_id).values_list("id", flat=True)
    )
    MessageReceipt.objects.bulk_create(
        [MessageReceipt(message=message, user_id=user_id) for user_id in recipient_ids],
        ignore_conflicts=True,
    )


def mark_room_read(room, user):
    """Best-effort receipt update used by the explicit /read/ endpoint only.

    SQLite permits one writer at a time. A locked database must never prevent
    opening a chat room, so lock errors are ignored and the next UI poll retries.
    """
    now = timezone.now()

    try:
        MessageReceipt.objects.filter(
            message__room=room,
            user=user,
        ).filter(
            Q(delivered_at__isnull=True) | Q(read_at__isnull=True)
        ).update(delivered_at=now, read_at=now)

        # Do not use update_or_create here: it takes a longer SQLite write lock.
        RoomMemberSettings.objects.filter(
            room=room,
            user=user,
        ).update(last_read_at=now)
    except OperationalError:
        # Another tab/WebSocket write owns SQLite briefly. The next read request
        # will retry; the chat page should still open normally.
        return


def create_mentions(message):
    """Save @username mentions for room members only."""
    usernames = set(re.findall(r"(?<![\w.])@([\w.-]{1,150})", message.content or ""))
    if not usernames:
        return
    users = message.room.members.filter(username__in=usernames)
    MessageMention.objects.bulk_create(
        [MessageMention(message=message, user=user) for user in users],
        ignore_conflicts=True,
    )


def message_receipt_summary(message):
    receipts = message.receipts.all()
    return {
        "delivered_count": sum(1 for item in receipts if item.delivered_at),
        "read_count": sum(1 for item in receipts if item.read_at),
    }


def serialize_poll(poll, user=None):
    selected_option_id = None

    if user and user.is_authenticated:
        vote = PollVote.objects.filter(poll=poll, user=user).first()
        if vote:
            selected_option_id = vote.option_id

    options = []
    total_votes = poll.votes.count()

    for option in poll.options.all():
        votes = option.votes.count()
        percentage = round((votes / total_votes) * 100) if total_votes else 0
        options.append({
            "id": option.id,
            "text": option.text,
            "image_url": option.image.url if option.image else "",
            "votes": votes,
            "percentage": percentage,
        })

    return {
        "id": poll.id,
        "question": poll.question,
        "image_url": poll.image.url if poll.image else "",
        "is_active": poll.is_active,
        "total_votes": total_votes,
        "selected_option_id": selected_option_id,
        "vote_url": reverse("vote_poll", kwargs={"poll_id": poll.id}),
        "options": options,
    }


def serialize_message(message, user=None):
    receipt = message_receipt_summary(message)
    content = "This message was deleted" if message.deleted_for_everyone else message.content

    return {
        "id": message.id,
        "username": message.user.username,
        "full_name": message.user.get_full_name() or message.user.username,
        "profile_photo_url": get_profile_photo_url(message.user),
        "course": get_user_course(message.user),
        "content": content,
        "image_url": message.image.url if message.image and not message.deleted_for_everyone else "",
        "video_url": message.video.url if message.video and not message.deleted_for_everyone else "",
        "audio_url": message.audio.url if message.audio and not message.deleted_for_everyone else "",
        "attachment_url": message.attachment.url if message.attachment and not message.deleted_for_everyone else "",
        "attachment_name": message.attachment_name,
        "parent_id": message.parent_id,
        "created_at": timezone.localtime(message.created_at).strftime("%d %b, %I:%M %p"),
        "created_at_iso": message.created_at.isoformat(),
        "like_url": reverse("like_message", kwargs={"id": message.id}),
        "edit_url": reverse("edit_message", kwargs={"id": message.id}),
        "delete_url": reverse("delete_message", kwargs={"id": message.id}),
        "pin_url": reverse("toggle_pin_message", kwargs={"id": message.id}),
        "forward_url": reverse("forward_message", kwargs={"id": message.id}),
        "star_url": reverse("toggle_star_message", kwargs={"id": message.id}),
        "is_pinned": message.is_pinned,
        "is_forwarded": bool(message.forwarded_from_id),
        "is_deleted": message.deleted_for_everyone,
        "is_starred": bool(user and user.is_authenticated and message.starred_by.filter(id=user.id).exists()),
        "receipt": receipt,
        "poll": serialize_poll(message.poll, user) if hasattr(message, "poll") else None,
    }


def broadcast_message(message):
    async_to_sync(get_channel_layer().group_send)(
        f"chat_room_{message.room_id}",
        {"type": "chat.message", "message": serialize_message(message)}
    )


def broadcast_reaction(message):
    async_to_sync(get_channel_layer().group_send)(
        f"chat_room_{message.room_id}",
        {"type": "chat.reaction", "message_id": message.id, "count": message.reactions.count()}
    )


def broadcast_message_edit(message):
    async_to_sync(get_channel_layer().group_send)(
        f"chat_room_{message.room_id}",
        {
            "type": "chat.message_edit",
            "message_id": message.id,
            "content": message.content,
            "deleted_for_everyone": message.deleted_for_everyone,
        }
    )


def broadcast_message_delete(room_id, message_id):
    async_to_sync(get_channel_layer().group_send)(
        f"chat_room_{room_id}",
        {"type": "chat.message_delete", "message_id": message_id}
    )


def broadcast_poll_update(poll):
    async_to_sync(get_channel_layer().group_send)(
        f"chat_room_{poll.room_id}",
        {"type": "chat.poll_update", "poll": serialize_poll(poll)}
    )


def serialize_pinned_message(message):
    if not message:
        return None

    return {
        "id": message.id,
        "content": message.content or "[Image or attachment]",
        "author": message.user.get_full_name() or message.user.username,
        "image_url": message.image.url if message.image else "",
        "pin_url": reverse("toggle_pin_message", kwargs={"id": message.id}),
    }


def get_latest_pinned_message(room):
    return (
        room.messages
        .filter(is_pinned=True, parent__isnull=True)
        .select_related("user")
        .order_by("-pinned_at", "-created_at")
        .first()
    )


def broadcast_pinned_update(room):
    async_to_sync(get_channel_layer().group_send)(
        f"chat_room_{room.id}",
        {
            "type": "chat.pinned_update",
            "pinned_message": serialize_pinned_message(
                get_latest_pinned_message(room)
            ),
        }
    )


@login_required(login_url="login_step1")
def home(request):
    # Show a one-time flash after an admin approves the student's join request.
    approved_requests = RoomJoinRequest.objects.filter(
        user=request.user,
        status="approved",
        notified=False
    ).select_related("room")

    for join_request in approved_requests:
        messages.success(
            request,
            f"You have joined {join_request.room.name}."
        )
        join_request.notified = True
        join_request.save(update_fields=["notified"])

    # All rooms appear in the directory. Private rooms show Request Access.
    rooms = ChatRoom.objects.all().prefetch_related(
        "members",
        "admins"
    ).order_by("name")

    room_cards = [serialize_room(room, request.user) for room in rooms]

    return render(request, "network/chat_home.html", {
        "rooms": rooms,
        "room_cards": room_cards,
    })


@login_required(login_url="login_step1")
@require_POST
def create_room(request):
    room_name = request.POST.get("room_name", "").strip()
    privacy = request.POST.get("privacy", "public")

    if privacy not in {"public", "private"}:
        privacy = "public"

    if not room_name:
        messages.error(request, "Room name is required.")
        return redirect("chat_home")

    room, created = ChatRoom.objects.get_or_create(
        name=room_name,
        defaults={
            "created_by": request.user,
            "privacy": privacy,
        }
    )

    if not created:
        messages.error(request, "A room with this name already exists.")
        return redirect("chat_home")

    room.members.add(request.user)
    room.admins.add(request.user)
    broadcast_room_created(room)

    return redirect("chat_room", room_name=room.name)


@login_required(login_url="login_step1")
@require_POST
def create_poll(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)

    if room.privacy == "private" and not room.members.filter(id=request.user.id).exists() and not room_is_admin(room, request.user):
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    question = request.POST.get("question", "").strip()
    options = [value.strip() for value in request.POST.getlist("options[]") if value.strip()]
    poll_image = request.FILES.get("image")

    if not question or len(options) < 2:
        return JsonResponse({"ok": False, "error": "Question and at least 2 options are required"}, status=400)

    image_files = [poll_image] + [
        request.FILES.get(f"option_image_{index}")
        for index in range(4)
    ]

    for image_file in image_files:
        if not image_file:
            continue
        if not (image_file.content_type or "").startswith("image/"):
            return JsonResponse({"ok": False, "error": "Poll images must be valid image files."}, status=400)
        if image_file.size > 5 * 1024 * 1024:
            return JsonResponse({"ok": False, "error": "Each poll image must be 5 MB or smaller."}, status=400)

    message = Message.objects.create(
        room=room,
        user=request.user,
        content=""
    )

    poll = Poll.objects.create(
        message=message,
        room=room,
        created_by=request.user,
        question=question,
        image=poll_image,
    )

    # Use create() instead of bulk_create so optional image files are saved correctly.
    for index, option_text in enumerate(options[:4]):
        PollOption.objects.create(
            poll=poll,
            text=option_text,
            image=request.FILES.get(f"option_image_{index}"),
        )

    # Reload the message with its newly created poll/options before broadcasting.
    # Without this reload, realtime clients can receive a poll-less blank message.
    message.expires_at = message_expiry_for_room(room)
    message.save(update_fields=["expires_at"])
    create_message_receipts(message)

    message = (
        Message.objects
        .select_related("user", "poll")
        .prefetch_related("poll__options", "poll__votes", "receipts", "starred_by")
        .get(id=message.id)
    )

    broadcast_message(message)
    return JsonResponse({"ok": True, "message": serialize_message(message, request.user)})


@login_required(login_url="login_step1")
@require_POST
def vote_poll(request, poll_id):
    poll = get_object_or_404(Poll, id=poll_id, is_active=True)

    if poll.room.privacy == "private" and not poll.room.members.filter(id=request.user.id).exists() and not room_is_admin(poll.room, request.user):
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    option_id = request.POST.get("option_id")
    option = get_object_or_404(PollOption, id=option_id, poll=poll)

    PollVote.objects.update_or_create(
        poll=poll,
        user=request.user,
        defaults={"option": option}
    )

    poll = Poll.objects.prefetch_related("options", "votes").get(id=poll.id)
    broadcast_poll_update(poll)

    return JsonResponse({"ok": True, "poll": serialize_poll(poll, request.user)})


@login_required(login_url="login_step1")
@require_POST
def request_join_room(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)

    if room.members.filter(id=request.user.id).exists():
        return redirect("chat_room", room_name=room.name)

    if room.privacy == "public":
        room.members.add(request.user)
        return redirect("chat_room", room_name=room.name)

    join_request, created = RoomJoinRequest.objects.get_or_create(
        room=room,
        user=request.user,
        defaults={"status": "pending"}
    )

    if not created and join_request.status == "rejected":
        join_request.status = "pending"
        join_request.reviewed_by = None
        join_request.notified = False
        join_request.save(update_fields=["status", "reviewed_by", "notified"])

    messages.success(request, "Join request sent to room admin.")
    return redirect("chat_home")


@login_required(login_url="login_step1")
def room(request, room_name):
    chat_room = get_object_or_404(ChatRoom, name=room_name)

    is_member = chat_room.members.filter(id=request.user.id).exists()
    is_admin = room_is_admin(chat_room, request.user)

    if chat_room.privacy == "private" and not is_member and not is_admin:
        messages.error(request, "You need admin approval to join this private room.")
        return redirect("chat_home")

    chat_room.members.add(request.user)

    if request.method == "POST":
        content = request.POST.get("content", "").strip()
        parent_id = request.POST.get("parent_id")
        image = request.FILES.get("image")
        video = request.FILES.get("video")
        audio = request.FILES.get("audio")
        attachment = request.FILES.get("attachment") or request.FILES.get("file")

        if video:
            allowed_extensions = {".mp4", ".webm", ".mov"}
            extension = os.path.splitext(video.name.lower())[1]
            is_video_type = (video.content_type or "").startswith("video/")

            if extension not in allowed_extensions or not is_video_type:
                error = "Please upload an MP4, WebM, or MOV video file."
                if request.headers.get("x-requested-with") == "XMLHttpRequest":
                    return JsonResponse({"ok": False, "error": error}, status=400)
                messages.error(request, error)
                return redirect("chat_room", room_name=chat_room.name)

            if video.size > 25 * 1024 * 1024:
                error = "Video must be 25 MB or smaller."
                if request.headers.get("x-requested-with") == "XMLHttpRequest":
                    return JsonResponse({"ok": False, "error": error}, status=400)
                messages.error(request, error)
                return redirect("chat_room", room_name=chat_room.name)

        if audio:
            allowed_audio_extensions = {".webm", ".ogg", ".mp3", ".m4a", ".wav"}
            extension = os.path.splitext(audio.name.lower())[1]
            is_audio_type = (audio.content_type or "").startswith("audio/")

            if extension not in allowed_audio_extensions or not is_audio_type:
                error = "Please upload a valid audio or voice message file."
                if request.headers.get("x-requested-with") == "XMLHttpRequest":
                    return JsonResponse({"ok": False, "error": error}, status=400)
                messages.error(request, error)
                return redirect("chat_room", room_name=chat_room.name)

            if audio.size > 10 * 1024 * 1024:
                error = "Voice message must be 10 MB or smaller."
                if request.headers.get("x-requested-with") == "XMLHttpRequest":
                    return JsonResponse({"ok": False, "error": error}, status=400)
                messages.error(request, error)
                return redirect("chat_room", room_name=chat_room.name)

        if content or image or video or audio or attachment:
            parent = None
            if parent_id:
                parent = Message.objects.filter(id=parent_id, room=chat_room).first()

            new_message = Message.objects.create(
                room=chat_room,
                user=request.user,
                content=content,
                parent=parent,
                image=image,
                video=video,
                audio=audio,
                attachment=attachment,
                expires_at=message_expiry_for_room(chat_room),
            )
            create_message_receipts(new_message)
            create_mentions(new_message)
            broadcast_message(new_message)

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": True, "message": serialize_message(new_message)})

        return redirect("chat_room", room_name=chat_room.name)

    # Keep this GET request read-only. The frontend calls /read/ after render.
    # This avoids SQLite write contention when multiple chat tabs are open.
    chat_messages = (
        chat_room.messages
        .filter(parent__isnull=True)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .select_related("user", "poll")
        .prefetch_related("replies__user", "reactions", "poll__options", "poll__votes")
        .order_by("created_at")
    )

    user_reactions = {
        reaction.message_id: reaction.emoji
        for reaction in Reaction.objects.filter(user=request.user, message__in=chat_messages)
    }

    for message in chat_messages:
        message.user_emoji = user_reactions.get(message.id, "")
        message.profile_photo_url = get_profile_photo_url(message.user)
        message.user_course = get_user_course(message.user)
        message.receipt_data = message_receipt_summary(message)
        message.is_starred_for_user = message.starred_by.filter(id=request.user.id).exists()
        message.poll_data = serialize_poll(message.poll, request.user) if hasattr(message, "poll") else None

        for reply in message.replies.all():
            reply.profile_photo_url = get_profile_photo_url(reply.user)
            reply.user_course = get_user_course(reply.user)

    return render(request, "network/chat_room.html", {
        "room": chat_room,
        "messages": chat_messages,
        "is_room_admin": is_admin,
        "pinned_message": get_latest_pinned_message(chat_room),
        "forward_rooms": (
            ChatRoom.objects.filter(members=request.user)
            .exclude(id=chat_room.id)
            .order_by("name")
        ),
        "room_settings": RoomMemberSettings.objects.filter(
            room=chat_room,
            user=request.user,
        ).first(),
    })


@login_required(login_url="login_step1")
def room_manage(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)

    if not room_is_admin(room, request.user):
        messages.error(request, "Only room admins can manage this group.")
        return redirect("chat_room", room_name=room.name)

    return render(request, "network/room_manage.html", {
        "room": room,
        "pending_requests": room.join_requests.filter(status="pending").select_related("user"),
        "members": room.members.all().order_by("username"),
        "admins": room.admins.all(),
    })


@login_required(login_url="login_step1")
@require_POST
def update_room_privacy(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)

    if not room_is_admin(room, request.user):
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    privacy = request.POST.get("privacy", "public")

    if privacy not in {"public", "private"}:
        messages.error(request, "Invalid privacy option.")
        return redirect("room_manage", room_name=room.name)

    room.privacy = privacy
    room.save(update_fields=["privacy"])

    messages.success(
        request,
        f"Room privacy changed to {room.get_privacy_display()}."
    )

    return redirect("room_manage", room_name=room.name)


@login_required(login_url="login_step1")
@require_POST
def approve_join_request(request, request_id):
    join_request = get_object_or_404(RoomJoinRequest, id=request_id, status="pending")

    if not room_is_admin(join_request.room, request.user):
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    join_request.room.members.add(join_request.user)
    join_request.status = "approved"
    join_request.reviewed_by = request.user
    join_request.notified = False
    join_request.save(update_fields=["status", "reviewed_by", "notified"])

    messages.success(request, f"{join_request.user.username} added to room.")
    return redirect("room_manage", room_name=join_request.room.name)


@login_required(login_url="login_step1")
@require_POST
def reject_join_request(request, request_id):
    join_request = get_object_or_404(RoomJoinRequest, id=request_id, status="pending")

    if not room_is_admin(join_request.room, request.user):
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    join_request.status = "rejected"
    join_request.reviewed_by = request.user
    join_request.save(update_fields=["status", "reviewed_by"])

    return redirect("room_manage", room_name=join_request.room.name)


@login_required(login_url="login_step1")
@require_POST
def admin_add_member(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)

    if not room_is_admin(room, request.user):
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    username = request.POST.get("username", "").strip()
    user = User.objects.filter(username=username).first()

    if not user:
        messages.error(request, "User ID not found.")
        return redirect("room_manage", room_name=room.name)

    room.members.add(user)
    messages.success(request, f"{user.username} added to room.")
    return redirect("room_manage", room_name=room.name)


@login_required(login_url="login_step1")
@require_POST
def promote_room_admin(request, room_name, user_id):
    room = get_object_or_404(ChatRoom, name=room_name)

    if not room_is_admin(room, request.user):
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    user = get_object_or_404(User, id=user_id)
    room.members.add(user)
    room.admins.add(user)

    messages.success(request, f"{user.username} is now a room admin.")
    return redirect("room_manage", room_name=room.name)


@login_required(login_url="login_step1")
@require_POST
def remove_room_member(request, room_name, user_id):
    room = get_object_or_404(ChatRoom, name=room_name)

    if not room_is_admin(room, request.user):
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    user = get_object_or_404(User, id=user_id)

    if user == room.created_by:
        messages.error(request, "Room creator cannot be removed.")
        return redirect("room_manage", room_name=room.name)

    room.members.remove(user)
    room.admins.remove(user)
    messages.success(request, f"{user.username} removed from room.")
    return redirect("room_manage", room_name=room.name)


@login_required(login_url="login_step1")
@require_POST
def like_message(request, id):
    message = get_object_or_404(Message, id=id)
    emoji = request.POST.get("emoji", "👍").strip()[:8]
    allowed = {item[0] for item in Reaction.EMOJI_CHOICES}

    if emoji not in allowed:
        emoji = "👍"

    reaction, created = Reaction.objects.get_or_create(
        message=message,
        user=request.user,
        defaults={"emoji": emoji}
    )

    liked = True
    user_emoji = emoji

    if not created:
        if reaction.emoji == emoji:
            reaction.delete()
            message.likes.remove(request.user)
            liked = False
            user_emoji = ""
        else:
            reaction.emoji = emoji
            reaction.save(update_fields=["emoji"])
    else:
        message.likes.add(request.user)

    if liked:
        message.likes.add(request.user)
    else:
        message.likes.remove(request.user)

    broadcast_reaction(message)

    return JsonResponse({
        "liked": liked,
        "count": message.reactions.count(),
        "user_emoji": user_emoji,
    })


@login_required(login_url="login_step1")
@require_POST
def toggle_pin_message(request, id):
    message = get_object_or_404(Message, id=id, parent__isnull=True)

    if not room_is_admin(message.room, request.user):
        return JsonResponse(
            {"ok": False, "error": "Only room admins can pin messages."},
            status=403,
        )

    if message.is_pinned:
        # Clicking Unpin removes the only currently pinned message.
        message.is_pinned = False
        message.pinned_at = None
        message.pinned_by = None
        message.save(update_fields=["is_pinned", "pinned_at", "pinned_by"])
    else:
        # One room can have only one pinned message at a time.
        Message.objects.filter(
            room=message.room,
            is_pinned=True
        ).exclude(id=message.id).update(
            is_pinned=False,
            pinned_at=None,
            pinned_by=None
        )

        message.is_pinned = True
        message.pinned_at = timezone.now()
        message.pinned_by = request.user
        message.save(update_fields=["is_pinned", "pinned_at", "pinned_by"])

    broadcast_pinned_update(message.room)

    latest_pin = get_latest_pinned_message(message.room)
    return JsonResponse({
        "ok": True,
        "is_pinned": message.is_pinned,
        "pinned_message": serialize_pinned_message(latest_pin),
    })


@login_required(login_url="login_step1")
@require_POST
def edit_message(request, id):
    message = get_object_or_404(Message, id=id)

    if message.user != request.user:
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    content = request.POST.get("content", "").strip()
    if not content:
        return JsonResponse({"ok": False, "error": "Message cannot be empty"}, status=400)

    message.content = content
    message.edited = True
    message.save(update_fields=["content", "edited"])
    broadcast_message_edit(message)

    return JsonResponse({"ok": True, "message_id": message.id, "content": message.content})


@login_required(login_url="login_step1")
@require_POST
def delete_message(request, id):
    message = get_object_or_404(Message, id=id)

    if message.user != request.user:
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    scope = request.POST.get("scope", "everyone")
    age = timezone.now() - message.created_at

    if scope == "everyone":
        if age > timedelta(days=2, hours=12):
            return JsonResponse(
                {"ok": False, "error": "Delete for everyone is no longer available."},
                status=400,
            )
        message.content = ""
        message.deleted_for_everyone = True
        message.deleted_at = timezone.now()
        message.deleted_by = request.user
        message.save(update_fields=[
            "content", "deleted_for_everyone", "deleted_at", "deleted_by"
        ])
        broadcast_message_edit(message)
        return JsonResponse({"ok": True, "deleted_for_everyone": True})

    # Delete for me can be added with a per-user hidden-message model in the UI phase.
    return JsonResponse({"ok": False, "error": "Delete for me will be added in the next UI phase."}, status=400)


@login_required(login_url="login_step1")
@require_POST
def forward_message(request, id):
    source = get_object_or_404(Message, id=id, parent__isnull=True)
    room_name = request.POST.get("room_name", "").strip()
    target_room = get_object_or_404(ChatRoom, name=room_name)

    if (
        target_room.privacy == "private"
        and not target_room.members.filter(id=request.user.id).exists()
        and not room_is_admin(target_room, request.user)
    ):
        return JsonResponse({"ok": False, "error": "Not allowed"}, status=403)

    if source.deleted_for_everyone:
        return JsonResponse({"ok": False, "error": "Deleted messages cannot be forwarded."}, status=400)

    forwarded = Message.objects.create(
        room=target_room,
        user=request.user,
        content=source.content,
        image=source.image.name if source.image else None,
        video=source.video.name if source.video else None,
        audio=source.audio.name if source.audio else None,
        attachment=source.attachment.name if source.attachment else None,
        forwarded_from=source,
        expires_at=message_expiry_for_room(target_room),
    )
    create_message_receipts(forwarded)
    create_mentions(forwarded)
    broadcast_message(forwarded)
    return JsonResponse({"ok": True, "message": serialize_message(forwarded, request.user)})


@login_required(login_url="login_step1")
@require_POST
def toggle_star_message(request, id):
    message = get_object_or_404(Message, id=id)
    if message.starred_by.filter(id=request.user.id).exists():
        message.starred_by.remove(request.user)
        starred = False
    else:
        message.starred_by.add(request.user)
        starred = True
    return JsonResponse({"ok": True, "starred": starred})


@login_required(login_url="login_step1")
@require_POST
def mark_messages_read(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)
    room.members.add(request.user)
    mark_room_read(room, request.user)
    return JsonResponse({"ok": True})


@login_required(login_url="login_step1")
@require_POST
def set_room_mute(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)
    try:
        minutes = int(request.POST.get("minutes", "0"))
    except (TypeError, ValueError):
        minutes = 0

    muted_until = timezone.now() + timedelta(minutes=minutes) if minutes > 0 else None
    settings, _ = RoomMemberSettings.objects.get_or_create(room=room, user=request.user)
    settings.muted_until = muted_until
    settings.save(update_fields=["muted_until"])
    return JsonResponse({"ok": True, "muted_until": muted_until.isoformat() if muted_until else None})


@login_required(login_url="login_step1")
@require_POST
def toggle_archive_room(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)
    settings, _ = RoomMemberSettings.objects.get_or_create(room=room, user=request.user)
    settings.is_archived = not settings.is_archived
    settings.save(update_fields=["is_archived"])
    return JsonResponse({"ok": True, "is_archived": settings.is_archived})


@login_required(login_url="login_step1")
@require_POST
def set_disappearing_messages(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)
    if not room_is_admin(room, request.user):
        return JsonResponse({"ok": False, "error": "Only room admins can change this."}, status=403)

    try:
        seconds = int(request.POST.get("seconds", "0"))
    except (TypeError, ValueError):
        seconds = 0

    allowed = {choice[0] for choice in ChatRoom.DISAPPEARING_CHOICES}
    if seconds not in allowed:
        return JsonResponse({"ok": False, "error": "Invalid timer."}, status=400)

    room.disappearing_after = seconds
    room.save(update_fields=["disappearing_after"])
    return JsonResponse({"ok": True, "seconds": seconds})


@login_required(login_url="login_step1")
@require_POST
def set_typing(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)
    room.members.add(request.user)

    typing_status, _ = TypingStatus.objects.get_or_create(room=room, user=request.user)
    typing_status.is_typing = request.POST.get("is_typing") == "true"
    typing_status.save()
    return JsonResponse({"ok": True})


@login_required(login_url="login_step1")
def typing_users(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)
    cutoff = timezone.now() - timedelta(seconds=3)

    users = [
        status.user.get_full_name() or status.user.username
        for status in TypingStatus.objects.filter(
            room=room,
            is_typing=True,
            updated_at__gte=cutoff
        ).exclude(user=request.user).select_related("user")
    ]

    return JsonResponse({"users": users})
