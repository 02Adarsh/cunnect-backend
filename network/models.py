from django.contrib.auth.models import User
from django.db import models


def chat_image_path(instance, filename):
    return f"chat/{instance.room.id}/images/{filename}"


def chat_file_path(instance, filename):
    return f"chat/{instance.room.id}/files/{filename}"


def chat_video_path(instance, filename):
    return f"chat/{instance.room.id}/videos/{filename}"


def chat_audio_path(instance, filename):
    return f"chat/{instance.room.id}/audio/{filename}"


def poll_image_path(instance, filename):
    room_id = getattr(instance, "room_id", None)

    if not room_id and getattr(instance, "poll_id", None):
        room_id = instance.poll.room_id

    return f"polls/{room_id}/images/{filename}"


class ChatRoom(models.Model):
    PRIVACY_CHOICES = [
        ("public", "Campus Public"),
        ("private", "Private - Approval Required"),
    ]

    DISAPPEARING_CHOICES = [
        (0, "Off"),
        (86400, "24 hours"),
        (604800, "7 days"),
        (7776000, "90 days"),
    ]

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="created_rooms"
    )
    members = models.ManyToManyField(
        User,
        blank=True,
        related_name="chat_rooms"
    )
    admins = models.ManyToManyField(
        User,
        blank=True,
        related_name="admin_chat_rooms"
    )
    privacy = models.CharField(
        max_length=10,
        choices=PRIVACY_CHOICES,
        default="public"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # 0 means regular messages do not disappear automatically.
    disappearing_after = models.PositiveIntegerField(
        choices=DISAPPEARING_CHOICES,
        default=0
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def is_admin(self, user):
        return user.is_authenticated and (
            self.created_by_id == user.id
            or self.admins.filter(id=user.id).exists()
        )


class RoomMemberSettings(models.Model):
    """Per-user WhatsApp-style chat controls for a room."""

    room = models.ForeignKey(
        ChatRoom,
        on_delete=models.CASCADE,
        related_name="member_settings"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="room_chat_settings"
    )
    muted_until = models.DateTimeField(null=True, blank=True)
    is_archived = models.BooleanField(default=False)
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("room", "user")
        indexes = [models.Index(fields=["user", "is_archived"])]

    def __str__(self):
        return f"{self.user.username} settings for {self.room.name}"


class RoomJoinRequest(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    room = models.ForeignKey(
        ChatRoom,
        on_delete=models.CASCADE,
        related_name="join_requests"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default="pending"
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_join_requests"
    )
    notified = models.BooleanField(default=False)

    class Meta:
        unique_together = ("room", "user")
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.user.username} -> {self.room.name} ({self.status})"


class Message(models.Model):
    room = models.ForeignKey(
        ChatRoom,
        on_delete=models.CASCADE,
        related_name="messages"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    content = models.TextField(blank=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="replies",
        on_delete=models.CASCADE
    )
    image = models.ImageField(upload_to=chat_image_path, blank=True, null=True)
    video = models.FileField(upload_to=chat_video_path, blank=True, null=True)
    audio = models.FileField(upload_to=chat_audio_path, blank=True, null=True)
    attachment = models.FileField(upload_to=chat_file_path, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    edited = models.BooleanField(default=False)
    # Kept for backward compatibility. Per-user delivered/read information
    # is stored in MessageReceipt below.
    seen = models.BooleanField(default=False)

    # WhatsApp-style forward, star and delete-for-everyone support.
    forwarded_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="forwarded_copies"
    )
    starred_by = models.ManyToManyField(
        User,
        blank=True,
        related_name="starred_chat_messages"
    )
    deleted_for_everyone = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deleted_chat_messages"
    )
    expires_at = models.DateTimeField(null=True, blank=True)

    is_pinned = models.BooleanField(default=False)
    pinned_at = models.DateTimeField(null=True, blank=True)
    pinned_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pinned_chat_messages"
    )
    likes = models.ManyToManyField(
        User,
        blank=True,
        related_name="liked_messages"
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.user.username}: {self.content[:30]}"

    @property
    def has_attachment(self):
        return bool(
            self.image
            or self.video
            or self.audio
            or self.attachment
        )

    @property
    def attachment_name(self):
        return self.attachment.name.split("/")[-1] if self.attachment else ""


class MessageReceipt(models.Model):
    """One delivery/read receipt per message and room member."""

    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="receipts"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="message_receipts"
    )
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("message", "user")
        indexes = [models.Index(fields=["user", "read_at"])]


class MessageMention(models.Model):
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="mentions"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="chat_mentions"
    )

    class Meta:
        unique_together = ("message", "user")



class Reaction(models.Model):
    EMOJI_CHOICES = [
        ("❤️", "Heart"),
        ("😂", "Laugh"),
        ("🔥", "Fire"),
        ("😍", "Love"),
        ("😮", "Wow"),
        ("😢", "Sad"),
        ("👍", "Like"),
        ("👎", "Dislike"),
    ]

    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="reactions"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    emoji = models.CharField(
        max_length=8,
        choices=EMOJI_CHOICES,
        default="👍"
    )
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("message", "user")
        indexes = [models.Index(fields=["message", "user"])]

    def __str__(self):
        return f"{self.user.username} {self.emoji} on {self.message.id}"


class Poll(models.Model):
    message = models.OneToOneField(
        Message,
        on_delete=models.CASCADE,
        related_name="poll"
    )
    room = models.ForeignKey(
        ChatRoom,
        on_delete=models.CASCADE,
        related_name="polls"
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="created_polls"
    )
    question = models.CharField(max_length=240)
    image = models.ImageField(upload_to=poll_image_path, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.question


class PollOption(models.Model):
    poll = models.ForeignKey(
        Poll,
        on_delete=models.CASCADE,
        related_name="options"
    )
    text = models.CharField(max_length=160)
    image = models.ImageField(upload_to=poll_image_path, blank=True, null=True)

    def __str__(self):
        return self.text


class PollVote(models.Model):
    poll = models.ForeignKey(
        Poll,
        on_delete=models.CASCADE,
        related_name="votes"
    )
    option = models.ForeignKey(
        PollOption,
        on_delete=models.CASCADE,
        related_name="votes"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("poll", "user")

    def __str__(self):
        return f"{self.user.username} voted on {self.poll.id}"


class UserStatus(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.username


class TypingStatus(models.Model):
    room = models.ForeignKey(
        ChatRoom,
        on_delete=models.CASCADE,
        related_name="typing_statuses"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    is_typing = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("room", "user")

    def __str__(self):
        return f"{self.user.username} typing in {self.room.name}"
