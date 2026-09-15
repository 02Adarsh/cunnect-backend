import random
import uuid

from decimal import Decimal

from django.conf import settings
from django.db import models


class FoodItem(models.Model):
    CATEGORY_CHOICES = [
        ("wrap", "Wrap"),
        ("burger", "Burger"),
        ("dosa", "Dosa"),
        ("pasta", "Pasta"),
        ("bowl", "Bowl"),
        ("dessert", "Dessert"),
    ]

    name = models.CharField(max_length=100)

    price = models.DecimalField(
        max_digits=6,
        decimal_places=2
    )

    description = models.CharField(max_length=250)

    image = models.ImageField(
        upload_to="food_images/",
        blank=True,
        null=True
    )

    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        default="wrap"
    )

    vendor = models.ForeignKey(
        "myapp.VendorProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="food_items"
    )

    is_available = models.BooleanField(default=True)

    # ⭐ VEG (True) / NON-VEG (False) — standard green/red mark ke liye
    is_veg = models.BooleanField(default=True)

    # ⭐ 0 = unlimited (tracking off); >0 = live stock, 0 hote hi auto-unavailable
    stock = models.IntegerField(default=0)

    created_at = models.DateTimeField(
        auto_now_add=True,
        null=True,
        blank=True
    )

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


class HeroSlide(models.Model):
    title = models.CharField(max_length=100)
    subtitle = models.CharField(max_length=200)

    image = models.ImageField(
        upload_to="hero_slides/"
    )

    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.title


class Cart(models.Model):
    session_key = models.CharField(max_length=64, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Cart - {self.session_key}"


class CartItem(models.Model):
    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name="items"
    )

    food_item = models.ForeignKey(
        FoodItem,
        on_delete=models.CASCADE
    )

    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = ("cart", "food_item")

    @property
    def subtotal(self):
        return self.food_item.price * self.quantity

    def __str__(self):
        return f"{self.food_item.name} × {self.quantity}"


class Coupon(models.Model):
    DISCOUNT_TYPE_CHOICES = [
        ("percentage", "Percentage"),
        ("fixed", "Fixed Amount"),
    ]

    code = models.CharField(
        max_length=30,
        unique=True
    )

    discount_type = models.CharField(
        max_length=12,
        choices=DISCOUNT_TYPE_CHOICES,
        default="percentage"
    )

    discount_value = models.DecimalField(
        max_digits=8,
        decimal_places=2
    )

    minimum_order_value = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=0
    )

    one_time_per_user = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    valid_from = models.DateTimeField(
        null=True,
        blank=True
    )

    valid_until = models.DateTimeField(
        null=True,
        blank=True
    )

    class Meta:
        ordering = ["code"]

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)

    @property
    def discount_label(self):
        if self.discount_type == "percentage":
            return f"{self.discount_value:g}% OFF"

        return f"₹{self.discount_value:g} OFF"

    def discount_for(self, cart_total):
        if self.discount_type == "percentage":
            discount = (
                cart_total * self.discount_value
            ) / Decimal("100")
        else:
            discount = self.discount_value

        return min(discount, cart_total)

    def __str__(self):
        return self.code


class CouponUsage(models.Model):
    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.CASCADE,
        related_name="usages"
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )

    session_key = models.CharField(
        max_length=64,
        blank=True
    )

    used_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-used_at"]

    def __str__(self):
        return f"{self.coupon.code} used at {self.used_at}"


class Order(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("preparing", "Preparing"),
        ("ready", "Ready for Pickup"),
        ("out_for_delivery", "Out for Delivery"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    PAYMENT_STATUS_CHOICES = [
        ("pending", "Pending"),
        ("paid", "Paid"),
        ("failed", "Failed"),
    ]

    order_number = models.CharField(
        max_length=20,
        unique=True,
        editable=False
    )

    vendor = models.ForeignKey(
        "myapp.VendorProfile",
        on_delete=models.CASCADE,
        related_name="orders"
    )

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="food_orders"
    )

    delivery_partner = models.ForeignKey(
        "myapp.DeliveryProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_orders"
    )

    customer_name = models.CharField(max_length=120)
    customer_phone = models.CharField(max_length=15)

    delivery_address = models.TextField()
    landmark = models.CharField(max_length=200, blank=True)
    customer_upi = models.CharField(max_length=120, blank=True, default="")
    txn_last4 = models.CharField(max_length=4, blank=True, default="")
    txn_id = models.CharField(max_length=64, blank=True, default="")

    payment_method = models.CharField(
        max_length=30,
        default="cash"
    )

    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default="pending"
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )

    subtotal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    order_note = models.TextField(blank=True)

    # Customer ko Food Dashboard / My Orders page par ye OTP dikhega
    delivery_otp = models.CharField(
        max_length=4,
        blank=True,
        default=""
    )

    otp_verified = models.BooleanField(default=False)

    delivered_at = models.DateTimeField(
        null=True,
        blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = (
                "CU-" + uuid.uuid4().hex[:8].upper()
            )

        if not self.delivery_otp:
            self.delivery_otp = str(
                random.randint(1000, 9999)
            )

        super().save(*args, **kwargs)

    def __str__(self):
        return self.order_number


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items"
    )

    food_item = models.ForeignKey(
        FoodItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    item_name = models.CharField(max_length=150)

    price = models.DecimalField(
        max_digits=8,
        decimal_places=2
    )

    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["id"]

    @property
    def subtotal(self):
        return self.price * self.quantity

    def __str__(self):
        return f"{self.item_name} × {self.quantity}"
    
class Notification(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="food_notifications"
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications"
    )

    title = models.CharField(max_length=150)
    message = models.CharField(max_length=300)

    # ⭐ kis portal ke liye hai — student app ya vendor app
    AUDIENCE_CHOICES = (
        ("student", "Student"),
        ("vendor", "Vendor"),
    )
    audience = models.CharField(
        max_length=10, choices=AUDIENCE_CHOICES,
        default="student", db_index=True,
    )

    is_read = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} - {self.title}"

class FoodOffer(models.Model):
    vendor = models.ForeignKey(
        "myapp.VendorProfile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="offers"
    )

    title = models.CharField(max_length=120)

    description = models.CharField(
        max_length=250,
        blank=True
    )

    image = models.ImageField(
        upload_to="food_offers/",
        blank=True,
        null=True
    )

    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    is_active = models.BooleanField(default=True)

    valid_until = models.DateTimeField(
        null=True,
        blank=True
    )

    order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "-created_at"]

    def __str__(self):
        return self.title