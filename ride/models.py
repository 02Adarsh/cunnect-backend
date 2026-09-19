"""ride app — ⭐ CUnnect Ride (v66).

Campus ride booking:
  student books (pickup -> drop, vehicle type, time)
      -> ride partners (vendors with vendor_type="ride") get an alert
      -> rider accepts  -> fare locked to that rider's own rate
      -> student pays (full OR 50-50 with a 5% split fee) via UPI QR
      -> rider reaches pickup, taps "I'm on location" -> OTP to student
      -> student shares OTP -> ride starts
      -> rider completes -> student is notified

⭐ VPS-friendly by design:
  * distance = haversine (straight line) x road factor — NO Google /
    Mapbox API key, NO billing, NO external quota. Runs on any VPS.
  * only the Django ORM is used; no extra services required.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

# ---------------------------------------------------------------------
# Vehicle catalogue (Uber-like, but NO bike — the owner asked for
# Auto / Mini / Sedan / XL only).
# ---------------------------------------------------------------------
VEHICLE_TYPES = [
    ("auto", "Auto", "🛺", 3, 25.0, 12.0),
    ("mini", "Mini", "🚗", 4, 30.0, 14.0),
    ("sedan", "Sedan", "🚙", 4, 45.0, 18.0),
    ("xl", "Car XL", "🚐", 6, 60.0, 23.0),
]

VEHICLE_KEYS = [v[0] for v in VEHICLE_TYPES]
VEHICLE_LABEL = {v[0]: v[1] for v in VEHICLE_TYPES}
VEHICLE_ICON = {v[0]: v[2] for v in VEHICLE_TYPES}
VEHICLE_SEATS = {v[0]: v[3] for v in VEHICLE_TYPES}
# Platform fallback rates (used only when no ride partner has set one).
VEHICLE_DEFAULT_BASE = {v[0]: v[4] for v in VEHICLE_TYPES}
VEHICLE_DEFAULT_PER_KM = {v[0]: v[5] for v in VEHICLE_TYPES}

# Straight-line distance under-estimates the real road, so it is scaled.
ROAD_FACTOR = 1.25

# ⭐ v66: 50-50 payments carry a 5% add-on (full payment has no extra).
SPLIT_FEE_PERCENT = 5.0


class RideVendor(models.Model):
    """A ride partner — a vendor (vendor_type="ride") who drives.

    The partner sets their OWN price (base fare + per km) for every
    vehicle they offer; the student's estimate is built from the best
    available rate and the final fare locks to the accepting rider.
    """

    vendor = models.OneToOneField(
        "myapp.VendorProfile",
        on_delete=models.CASCADE,
        related_name="ride_profile",
    )

    vehicle_number = models.CharField(max_length=24, blank=True, default="")
    vehicle_model = models.CharField(max_length=80, blank=True, default="")

    # ⭐ rates + availability, one block per vehicle type
    auto_active = models.BooleanField(default=True)
    auto_base = models.DecimalField(max_digits=8, decimal_places=2, default=25.0)
    auto_per_km = models.DecimalField(max_digits=8, decimal_places=2, default=12.0)

    mini_active = models.BooleanField(default=True)
    mini_base = models.DecimalField(max_digits=8, decimal_places=2, default=30.0)
    mini_per_km = models.DecimalField(max_digits=8, decimal_places=2, default=14.0)

    sedan_active = models.BooleanField(default=False)
    sedan_base = models.DecimalField(max_digits=8, decimal_places=2, default=45.0)
    sedan_per_km = models.DecimalField(max_digits=8, decimal_places=2, default=18.0)

    xl_active = models.BooleanField(default=False)
    xl_base = models.DecimalField(max_digits=8, decimal_places=2, default=60.0)
    xl_per_km = models.DecimalField(max_digits=8, decimal_places=2, default=23.0)

    # ⭐ online = receiving ride alerts right now
    is_online = models.BooleanField(default=False)

    total_rides = models.PositiveIntegerField(default=0)
    total_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ride partner"
        verbose_name_plural = "Ride partners"

    def __str__(self):
        return f"{self.vendor.business_name} ({self.vehicle_number or 'no vehicle'})"

    # -- rate table -------------------------------------------------
    def rate_for(self, vehicle_type):
        """(active, base, per_km) for a vehicle type."""
        if vehicle_type == "auto":
            return self.auto_active, self.auto_base, self.auto_per_km
        if vehicle_type == "mini":
            return self.mini_active, self.mini_base, self.mini_per_km
        if vehicle_type == "sedan":
            return self.sedan_active, self.sedan_base, self.sedan_per_km
        if vehicle_type == "xl":
            return self.xl_active, self.xl_base, self.xl_per_km
        return False, 0, 0

    def set_rate(self, vehicle_type, active, base, per_km):
        if vehicle_type == "auto":
            self.auto_active, self.auto_base, self.auto_per_km = active, base, per_km
        elif vehicle_type == "mini":
            self.mini_active, self.mini_base, self.mini_per_km = active, base, per_km
        elif vehicle_type == "sedan":
            self.sedan_active, self.sedan_base, self.sedan_per_km = active, base, per_km
        elif vehicle_type == "xl":
            self.xl_active, self.xl_base, self.xl_per_km = active, base, per_km

    def active_vehicles(self):
        return [v for v in VEHICLE_KEYS if self.rate_for(v)[0]]

    def offers(self, vehicle_type):
        """Vehicle offered AND partner online."""
        active, _b, _p = self.rate_for(vehicle_type)
        return bool(active)


class RideRejection(models.Model):
    """A ride partner said "no" — the ride disappears from their list
    but stays available for everyone else."""

    ride = models.ForeignKey(
        "Ride", on_delete=models.CASCADE, related_name="rejections")
    vendor = models.ForeignKey(
        RideVendor, on_delete=models.CASCADE, related_name="rejections")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("ride", "vendor")


class Ride(models.Model):
    """One ride booking."""

    STATUS_CHOICES = [
        ("requested", "Requested"),      # waiting for a rider
        ("accepted", "Accepted"),        # rider accepted, awaiting payment
        ("paid", "Paid"),                # student paid (full or 1st half)
        ("arrived", "Arrived"),          # rider at pickup, OTP sent
        ("ongoing", "Ongoing"),          # OTP verified, ride running
        ("completed", "Completed"),      # ride finished
        ("rejected", "Rejected"),        # rider said no -> back to requested
        ("cancelled", "Cancelled"),
    ]

    PAYMENT_MODE_CHOICES = [
        ("full", "Full payment"),
        ("split", "50-50 split (+5%)"),
    ]

    ride_code = models.CharField(max_length=20, unique=True, editable=False)

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rides",
    )
    rider = models.ForeignKey(
        RideVendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rides",
    )

    # -- trip -------------------------------------------------------
    vehicle_type = models.CharField(max_length=16, default="auto")
    pickup_text = models.CharField(max_length=200)
    pickup_lat = models.FloatField(null=True, blank=True)
    pickup_lng = models.FloatField(null=True, blank=True)
    drop_text = models.CharField(max_length=200)
    drop_lat = models.FloatField(null=True, blank=True)
    drop_lng = models.FloatField(null=True, blank=True)

    distance_km = models.FloatField(default=0.0)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True, default="")

    # -- fare (locked when a rider accepts) --------------------------
    base_fare = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    per_km = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    fare = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    split_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    payment_mode = models.CharField(
        max_length=10, choices=PAYMENT_MODE_CHOICES, blank=True, default=""
    )
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    balance_due = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    txn_first = models.CharField(max_length=120, blank=True, default="")
    txn_second = models.CharField(max_length=120, blank=True, default="")
    payment_done = models.BooleanField(default=False)

    # -- OTP (rider arrival -> ride start) ---------------------------
    # ⭐ v68: the OTP is sent to the STUDENT; the student reads it out
    # and the RIDER types it into his console to start the ride.
    otp = models.CharField(max_length=6, blank=True, default="")

    # -- live rider position (student sees the rider move on the map) --
    rider_lat = models.FloatField(null=True, blank=True)
    rider_lng = models.FloatField(null=True, blank=True)
    rider_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default="requested", db_index=True
    )
    cancel_reason = models.CharField(max_length=200, blank=True, default="")

    student_name = models.CharField(max_length=120, blank=True, default="")
    student_phone = models.CharField(max_length=20, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.ride_code} · {self.vehicle_type} · {self.status}"

    # -- derived ----------------------------------------------------
    @property
    def rider_name(self):
        return self.rider.vendor.business_name if self.rider_id else ""

    @property
    def rider_phone(self):
        return (self.rider.vendor.phone or "") if self.rider_id else ""

    @property
    def vehicle_label(self):
        return VEHICLE_LABEL.get(self.vehicle_type, self.vehicle_type.title())

    @property
    def vehicle_icon(self):
        return VEHICLE_ICON.get(self.vehicle_type, "🚗")

    def amount_now(self):
        """What the student must pay at this moment."""
        if self.payment_done:
            return 0
        if self.payment_mode == "split":
            return self.balance_due if self.amount_paid > 0 else (self.total / 2)
        return self.total

    def save(self, *args, **kwargs):
        if not self.ride_code:
            self.ride_code = _new_ride_code()
        super().save(*args, **kwargs)


def _new_ride_code():
    import random
    import string

    alphabet = string.ascii_uppercase + string.digits
    for _ in range(12):
        code = "RIDE-" + "".join(random.choice(alphabet) for _ in range(5))
        if not Ride.objects.filter(ride_code=code).exists():
            return code
    return "RIDE-" + str(int(timezone.now().timestamp()))[-8:]
