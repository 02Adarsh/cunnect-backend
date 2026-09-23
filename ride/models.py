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
# Mini / Sedan / SUV).
# ---------------------------------------------------------------------
# ⭐ v73: the Auto rickshaw option has been removed from CUnnect Ride.
# ⭐ v77: "Car XL" is now called SUV everywhere.
VEHICLE_TYPES = [
    ("mini", "Mini", "🚗", 4, 30.0, 14.0),
    ("sedan", "Sedan", "🚙", 4, 45.0, 18.0),
    ("suv", "SUV", "🚐", 6, 60.0, 23.0),
]

VEHICLE_KEYS = [v[0] for v in VEHICLE_TYPES]
VEHICLE_LABEL = {v[0]: v[1] for v in VEHICLE_TYPES}
VEHICLE_ICON = {v[0]: v[2] for v in VEHICLE_TYPES}
# ⭐ v77: rides booked before the rename still carry "xl" in the DB
VEHICLE_LABEL["xl"] = "SUV"
VEHICLE_ICON["xl"] = "🚐"
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

    # ⭐ v77: "xl" is SUV now (the columns were renamed in migration 0009)
    suv_active = models.BooleanField(default=False)
    suv_base = models.DecimalField(max_digits=8, decimal_places=2, default=60.0)
    suv_per_km = models.DecimalField(max_digits=8, decimal_places=2, default=23.0)

    # ⭐ online = receiving ride alerts right now
    is_online = models.BooleanField(default=False)

    # ⭐ v79: an AUTO partner. He is not part of the car booking flow at
    # all — the student taps the AUTO button on the Ride screen and every
    # auto partner is alerted at once. No fare, no payment, no OTP, no
    # ride record: it is a plain "come to the main gate" call.
    is_auto = models.BooleanField(default=False)

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
        if vehicle_type in ("suv", "xl"):
            return self.suv_active, self.suv_base, self.suv_per_km
        return False, 0, 0

    def set_rate(self, vehicle_type, active, base, per_km):
        if vehicle_type == "auto":
            self.auto_active, self.auto_base, self.auto_per_km = active, base, per_km
        elif vehicle_type == "mini":
            self.mini_active, self.mini_base, self.mini_per_km = active, base, per_km
        elif vehicle_type == "sedan":
            self.sedan_active, self.sedan_base, self.sedan_per_km = active, base, per_km
        elif vehicle_type in ("suv", "xl"):
            self.suv_active, self.suv_base, self.suv_per_km = active, base, per_km

    def active_vehicles(self):
        return [v for v in VEHICLE_KEYS if self.rate_for(v)[0]]

    def offers(self, vehicle_type):
        """Vehicle offered AND partner online."""
        active, _b, _p = self.rate_for(vehicle_type)
        return bool(active)


class RideVehicle(models.Model):
    """⭐ v77: one of the ride partner's saved vehicles.

    A partner can keep several cars — a Mini, a Sedan, an SUV — each
    with its own name and number plate. While accepting a request he
    picks which one he is driving for that ride.
    """

    rider = models.ForeignKey(
        RideVendor, on_delete=models.CASCADE, related_name="vehicles")
    vehicle_type = models.CharField(max_length=12, default="mini")
    name = models.CharField(max_length=80, blank=True, default="")
    plate = models.CharField(max_length=24, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["vehicle_type", "name"]

    def __str__(self):
        return f"{self.name or self.vehicle_type} · {self.plate}"

    def as_dict(self):
        return {
            "id": self.id,
            "vehicle_type": self.vehicle_type,
            "vehicle_label": VEHICLE_LABEL.get(self.vehicle_type,
                                               self.vehicle_type.title()),
            "vehicle_icon": VEHICLE_ICON.get(self.vehicle_type, "🚗"),
            "name": self.name,
            "plate": self.plate,
        }


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
    # ⭐ v75: the rider must CONFIRM the payment himself. Nothing
    # advances automatically once the student has paid.
    payment_confirmed = models.BooleanField(default=False)
    # ⭐ v78: a 50-50 ride CANNOT be closed while the second half is
    # unpaid. The rider taps COMPLETE RIDE and the ride parks here until
    # the student pays the balance (with its own transaction id).
    awaiting_balance = models.BooleanField(default=False)

    # ⭐ v77: which car the rider is driving for THIS ride (chosen while
    # accepting). The plate stays hidden from the student until the
    # payment has been verified.
    vehicle_name = models.CharField(max_length=80, blank=True, default="")
    vehicle_plate = models.CharField(max_length=24, blank=True, default="")

    # -- OTP (rider arrival -> ride start) ---------------------------
    # ⭐ v68: the OTP is sent to the STUDENT; the student reads it out
    # and the RIDER types it into his console to start the ride.
    otp = models.CharField(max_length=6, blank=True, default="")

    # -- live rider position (student sees the rider move on the map) --
    rider_lat = models.FloatField(null=True, blank=True)
    rider_lng = models.FloatField(null=True, blank=True)
    rider_at = models.DateTimeField(null=True, blank=True)
    # ⭐ v72: the student can share their own live position too, so the
    # rider sees exactly where to pick them up.
    student_lat = models.FloatField(null=True, blank=True)
    student_lng = models.FloatField(null=True, blank=True)
    student_at = models.DateTimeField(null=True, blank=True)
    share_location = models.BooleanField(default=False)
    # ⭐ v73: the number the rider will actually call. It is the student's
    # own number unless they ticked "booking for someone else".
    contact_phone = models.CharField(max_length=20, blank=True, default="")
    booking_for_other = models.BooleanField(default=False)
    other_name = models.CharField(max_length=80, blank=True, default="")

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


class RiderBlock(models.Model):
    """⭐ v73: when a ride partner is NOT available.

    Two kinds, both fully editable from the rider portal:
      * "daily"  — repeats every week (e.g. every Monday 09:00-11:00)
      * "date"   — one specific calendar date (e.g. 2026-10-02 14:00-18:00)
    Rides requested inside a blocked window are automatically answered
    with "rider unavailable right now, please book for another time".
    """

    DAILY = "daily"
    DATE = "date"

    rider = models.ForeignKey(
        RideVendor, on_delete=models.CASCADE, related_name="blocks")
    kind = models.CharField(max_length=8, default=DATE)

    # daily blocks
    weekday = models.IntegerField(
        default=0, help_text="0 = Monday … 6 = Sunday")
    start_min = models.IntegerField(default=0, help_text="minutes past midnight")
    end_min = models.IntegerField(default=1439, help_text="minutes past midnight")

    # one-off blocks
    date = models.DateField(null=True, blank=True)

    label = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "weekday", "date", "start_min"]
        verbose_name = "Rider unavailability"
        verbose_name_plural = "Rider unavailability slots"

    def __str__(self):
        if self.kind == self.DAILY:
            return (f"{self.rider} · every {self.weekday_name} "
                    f"{_hhmm(self.start_min)}-{_hhmm(self.end_min)}")
        return (f"{self.rider} · {self.date} "
                f"{_hhmm(self.start_min)}-{_hhmm(self.end_min)}")

    @property
    def weekday_name(self):
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
            self.weekday if 0 <= self.weekday <= 6 else 0]

    def covers(self, when):
        """True when `when` (aware datetime) falls inside this block."""
        from django.utils import timezone

        if when is None:
            return False
        try:
            local = timezone.localtime(when)
        except Exception:
            local = when
        minutes = local.hour * 60 + local.minute
        if self.kind == self.DAILY:
            return local.weekday() == self.weekday and (
                self.start_min <= minutes <= self.end_min)
        if self.date is None:
            return False
        if local.date() != self.date:
            return False
        return self.start_min <= minutes <= self.end_min


class RidePax(models.Model):
    """⭐ v74: splitting a ride fare with the people travelling along.

    The student adds each co-passenger and how much of the fare is
    theirs; the app then shows exactly who has paid and who still owes,
    which no other campus app does.
    """

    ride = models.ForeignKey(
        "Ride", on_delete=models.CASCADE, related_name="pax")
    name = models.CharField(max_length=80, blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Ride co-passenger"
        verbose_name_plural = "Ride co-passengers"

    def __str__(self):
        return f"{self.ride_id} · {self.name or 'co-passenger'} ₹{self.amount}"

    @property
    def amount_f(self):
        try:
            return float(self.amount or 0)
        except (TypeError, ValueError):
            return 0.0


def _hhmm(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def rider_is_blocked(rider, when):
    """Is this partner unavailable at `when`? (aware or naive datetime)"""
    if rider is None or when is None:
        return False
    from django.utils import timezone

    try:
        local = timezone.localtime(when)
    except Exception:
        local = when
    for block in RiderBlock.objects.filter(rider_id=rider.id):
        if block.covers(local):
            return True
    return False
