import random

from django.contrib.auth.models import User
from django.db import models


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    full_name = models.CharField(
        max_length=150,
        blank=True,
        null=True
    )

    is_verified = models.BooleanField(default=False)
    otp = models.CharField(max_length=6, blank=True, null=True)
    otp_created_at = models.DateTimeField(
        auto_now_add=True,
        null=True,
        blank=True
    )

    phone = models.CharField(max_length=15, blank=True, null=True)
    dob = models.DateField(blank=True, null=True)
    gender = models.CharField(max_length=10, blank=True, null=True)
    branch = models.CharField(max_length=100, blank=True, null=True)
    year = models.CharField(max_length=20, blank=True, null=True)
    stay_type = models.CharField(max_length=20, blank=True, null=True)
    profile_photo = models.ImageField(
        upload_to="profile_photos/",
        blank=True,
        null=True
    )
    consent = models.BooleanField(default=False)

    def __str__(self):
        return self.user.username

    @staticmethod
    def generate_otp():
        return str(random.randint(100000, 999999))


class VendorProfile(models.Model):
    VENDOR_TYPE_CHOICES = [
        ("food", "Food Vendor"),
        ("printout", "Printout Vendor"),
        ("hostel", "Hostel Essentials Vendor"),
    ]

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="vendor_profile"
    )

    business_name = models.CharField(max_length=150)

    vendor_type = models.CharField(
        max_length=20,
        choices=VENDOR_TYPE_CHOICES,
        default="food"
    )

    phone = models.CharField(
        max_length=15,
        unique=True,
        null=True,
        blank=True
    )

    # Printout vendor price settings.
    # Student automatic total = pages × copies × selected price.
    bw_price_per_page = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=1.00
    )

    color_price_per_page = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=5.00
    )

    is_approved = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    # ⭐ Hostel Essentials vendor ka UPI ID (students ko payment ke liye dikhta hai)
    upi_id = models.CharField(max_length=120, blank=True, default="")
    upi_qr_image = models.ImageField(
        upload_to="vendor_qr/", blank=True, null=True)
    logo = models.ImageField(
        upload_to="vendor_logo/", blank=True, null=True)

    def __str__(self):
        return f"{self.business_name} - {self.vendor_type}"


class DeliveryProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="delivery_profile"
    )

    phone = models.CharField(
        max_length=15,
        unique=True,
        null=True,
        blank=True
    )

    is_approved = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"Delivery - {self.user.username}"


class ChatRoom(models.Model):
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class ChatMessage(models.Model):
    room = models.ForeignKey(
        ChatRoom,
        on_delete=models.CASCADE,
        related_name="messages"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    message = models.TextField()
    image = models.ImageField(
        upload_to="chat_images/",
        blank=True,
        null=True
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["timestamp"]

    def __str__(self):
        return f"{self.user.username}: {self.message[:50]}"


class Banner(models.Model):
    title = models.CharField(max_length=200)
    subtitle = models.TextField(blank=True, null=True)

    image = models.ImageField(
        upload_to="banners/images/",
        blank=True,
        null=True
    )

    video = models.FileField(
        upload_to="banners/videos/",
        blank=True,
        null=True
    )

    button_text = models.CharField(
        max_length=100,
        default="Explore Now"
    )

    button_link = models.CharField(
        max_length=200,
        default="#"
    )

    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.title


class SupportRequest(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("in_progress", "In Progress"),
        ("resolved", "Resolved"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="support_requests"
    )

    email = models.EmailField()
    subject = models.CharField(max_length=180)
    message = models.TextField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} - {self.subject}"


class PrintOrder(models.Model):
    PRINT_TYPE_CHOICES = [
        ("bw", "Black & White"),
        ("color", "Color Print"),
        ("mixed", "Mixed B&W + Color"),
    ]

    PAPER_SIZE_CHOICES = [
        ("a4", "A4"),
        ("a3", "A3"),
    ]

    PRINT_SIDE_CHOICES = [
        ("single", "One Side"),
        ("double", "Double Side"),
    ]

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("printing", "Printing"),
        ("ready", "Ready for Pickup"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    vendor = models.ForeignKey(
        VendorProfile,
        on_delete=models.CASCADE,
        related_name="print_orders"
    )

    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="print_orders"
    )

    document = models.FileField(
        upload_to="print_orders/%Y/%m/"
    )

    pages = models.PositiveIntegerField()
    copies = models.PositiveIntegerField(default=1)

    # For mixed print orders, B&W and Color pages are saved separately.
    bw_pages = models.PositiveIntegerField(default=0)
    color_pages = models.PositiveIntegerField(default=0)

    # Examples: "1-4, 8" and "5-7, 9-10"
    bw_page_ranges = models.CharField(max_length=500, blank=True)
    color_page_ranges = models.CharField(max_length=500, blank=True)

    print_type = models.CharField(
        max_length=10,
        choices=PRINT_TYPE_CHOICES,
        default="bw"
    )

    paper_size = models.CharField(
        max_length=5,
        choices=PAPER_SIZE_CHOICES,
        default="a4"
    )

    print_side = models.CharField(
        max_length=10,
        choices=PRINT_SIDE_CHOICES,
        default="single"
    )

    binding = models.BooleanField(default=False)
    lamination = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    final_amount = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=0
    )

    status = models.CharField(
        max_length=15,
        choices=STATUS_CHOICES,
        default="pending"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Print #{self.id} - {self.student.username}"

    @property
    def file_name(self):
        return self.document.name.split("/")[-1]


class UmsSaved(models.Model):
    """⭐ UMS saved password+cookies — DB me taaki Render restart pe
    captcha dobara na maangna pade."""

    uid = models.CharField(max_length=60, unique=True)
    payload = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class HostelOrder(models.Model):
    """⭐ Hostel Essentials 8-in-1 pack order (₹1799)."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]

    order_no = models.CharField(max_length=24, unique=True)

    # jis app-id se order hua (auto)
    student = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="hostel_orders")
    orderer_uid = models.CharField(max_length=60, blank=True, default="")
    orderer_name = models.CharField(max_length=120, blank=True, default="")
    orderer_mobile = models.CharField(max_length=20, blank=True, default="")

    # jiske liye order hua (manual)
    recipient_name = models.CharField(max_length=120)
    recipient_mobile = models.CharField(max_length=20)

    address = models.TextField(blank=True, default="Chandigarh University")

    payment_ref = models.CharField(max_length=120, blank=True, default="")
    customer_upi = models.CharField(max_length=120, blank=True, default="")
    txn_last4 = models.CharField(max_length=4, blank=True, default="")
    paid = models.BooleanField(default=False)

    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default="pending")
    total = models.DecimalField(max_digits=8, decimal_places=2, default=1799)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.order_no} ({self.recipient_name})"


class DeviceToken(models.Model):
    """⭐ FCM push token per user (screen-off notifications)."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="device_tokens")
    token = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)
