"""api_app — REST API layer for the Flutter app.

Every endpoint returns JSON:
    {"ok": true, "data": {...}}   ya   {"ok": false, "error": "..."}

Auth: `Authorization: Token <key>` header (rest_framework.authtoken).
Ye layer existing Django apps (food, myapp, network, scraper_app) ke
reuses the existing models/logic — no duplicated data.
"""

import base64
import json
import os
import random
import re
import string
import threading
import time
from datetime import timedelta
from functools import wraps

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework.authtoken.models import Token

from .security import (
    allow as _rl_allow,
    check_upload,
    clean_name,
    client_ip,
    throttle,
    valid_user_id,
)
from django.utils.html import escape as _esc

from food.models import (
    Coupon,
    CouponUsage,
    FoodItem,
    FoodOffer,
    HeroSlide,
    Notification,
    Order,
    OrderItem,
)
from myapp.models import (
    Banner,
    DeliveryProfile,
    PrintOrder,
    SupportRequest,
    UserProfile,
    VendorProfile,
)
from network.models import (
    ChatRoom,
    Message,
    Poll,
    PollOption,
    PollVote,
    RoomJoinRequest,
)

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

VENDOR_SESSION = {"kitchen": {}, "alerts": {}}


def ok(data=None):
    return JsonResponse({"ok": True, "data": data or {}})


def fail(message, status=400):
    return JsonResponse({"ok": False, "error": message}, status=status)


def iso(dt):
    return dt.astimezone().isoformat() if dt else None


def json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


# ⭐ Live user tracking — user_id -> last request timestamp. In-memory,
# refreshed on every authenticated API call (the app polls frequently,
# so this closely mirrors who has the app open right now).
_LAST_SEEN = {}
LIVE_WINDOW_SECONDS = 180

# ⭐ Traffic stats — impressions are buffered in memory and flushed to the
# DB at most once every few seconds (keeps request latency unaffected).
_IMPRESSION_BUFFER = {"count": 0, "last_flush": 0.0}
_VISIT_SEEN_TODAY = set()  # (date_iso, user_id) — cleared on date change
_VISIT_SEEN_DATE = [""]


def _record_traffic(user):
    """Count an impression + a unique daily visit for the stats page."""
    from myapp.models import TrafficStat, DailyVisit

    now = time.time()
    _IMPRESSION_BUFFER["count"] += 1
    today = timezone.localdate()
    today_iso = today.isoformat()
    # reset the per-day visit cache at midnight
    if _VISIT_SEEN_DATE[0] != today_iso:
        _VISIT_SEEN_DATE[0] = today_iso
        _VISIT_SEEN_TODAY.clear()
    key = (today_iso, user.id)
    if key not in _VISIT_SEEN_TODAY:
        _VISIT_SEEN_TODAY.add(key)
        try:
            DailyVisit.objects.get_or_create(date=today, user=user)
        except Exception:
            pass
    # flush impressions every 5 seconds at most
    if now - _IMPRESSION_BUFFER["last_flush"] >= 5:
        pending = _IMPRESSION_BUFFER["count"]
        _IMPRESSION_BUFFER["count"] = 0
        _IMPRESSION_BUFFER["last_flush"] = now
        try:
            from django.db.models import F
            row, created = TrafficStat.objects.get_or_create(
                date=today, defaults={"impressions": pending})
            if not created:
                TrafficStat.objects.filter(pk=row.pk).update(
                    impressions=F("impressions") + pending)
        except Exception:
            pass


def _mark_seen(user):
    try:
        _LAST_SEEN[user.id] = time.time()
        _record_traffic(user)
    except Exception:
        pass
    # ⭐ v70: keep the single-device session alive (throttled — at most
    # one DB write per user every 2 minutes).
    try:
        now = time.time()
        last = _SESSION_SEEN.get(user.id, 0)
        if now - last >= 120:
            _SESSION_SEEN[user.id] = now
            from myapp.models import LoginSession

            LoginSession.objects.filter(user_id=user.id).update(
                last_seen=timezone.now())
    except Exception:
        pass


# ⭐ v70: single-device login -----------------------------------------
_SESSION_SEEN = {}

SESSION_BUSY_MSG = ("This UID is already logged in on another device. "
                    "Log out there first, then sign in here.")


def _device_id(request, body=None):
    """Per-install id sent by the app (falls back to the token)."""
    did = ""
    try:
        did = str((body or {}).get("device_id", "")).strip()
    except Exception:
        did = ""
    if not did:
        did = str(request.headers.get("X-Device-Id", "")).strip()
    return did[:64]


def _session_guard(user, device_id):
    """Block a second phone while the account is live on another one.

    Returns None when the login may proceed, else the error string.
    """
    from myapp.models import LoginSession

    sess = LoginSession.objects.filter(user_id=user.id).first()
    if sess is None:
        return None
    if device_id and sess.device_id and device_id == sess.device_id:
        return None                      # same phone re-logging in
    if sess.is_stale():                  # long inactivity -> release
        sess.delete()
        return None
    return SESSION_BUSY_MSG


def _session_start(user, device_id, token_key=""):
    from myapp.models import LoginSession

    LoginSession.objects.update_or_create(
        user_id=user.id,
        defaults={
            "device_id": device_id or "",
            "token_key": token_key or "",
            "last_seen": timezone.now(),
        },
    )


def _session_end(user):
    """Logout: free the account so another phone can sign in."""
    from myapp.models import LoginSession

    LoginSession.objects.filter(user_id=user.id).delete()


def user_from_token(request):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Token "):
        return None
    key = header[6:].strip()
    try:
        user = Token.objects.select_related("user").get(key=key).user
    except Token.DoesNotExist:
        return None
    # ⭐ Disabled accounts are locked out instantly (token already deleted
    # on disable, but this also covers any stale token edge case).
    if not user.is_active:
        return None
    _mark_seen(user)
    return user


def student_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = user_from_token(request)
        if user is None:
            return fail("Login required.", status=401)
        return view(request, user, *args, **kwargs)

    return wrapper


def vendor_user(request):
    """User resolved from the vendor token — with VendorProfile."""
    user = user_from_token(request)
    if user is None:
        return None, None
    profile = VendorProfile.objects.filter(user=user).select_related("user").first()
    return user, profile


def delivery_user(request):
    user = user_from_token(request)
    if user is None:
        return None, None
    profile = DeliveryProfile.objects.filter(user=user).select_related("user").first()
    return user, profile


def media_url(field_file):
    try:
        if field_file:
            return field_file.url
    except Exception:
        pass
    return ""


def serialize_order(order, include_items=True, include_otp=False,
                   reveal_mobile=True):
    data = {
        "id": order.id,
        "order_number": order.order_number,
        "vendor_id": order.vendor_id,
        "vendor_name": order.vendor.business_name if order.vendor else "",
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone if reveal_mobile else "",
        "customer_uid": order.customer.username if order.customer else "",
        "customer_upi": order.customer_upi,
        "txn_last4": order.txn_last4,
        "txn_id": order.txn_id,
        # ⭐ v62: the correct reverse accessor is `userprofile` (joined via
        # select_related, zero extra queries) — `profile` never existed.
        "customer_branch": getattr(
            getattr(order.customer, "userprofile", None), "branch", "") or "",
        "customer_year": getattr(
            getattr(order.customer, "userprofile", None), "year", "") or "",
        "customer_hostel": getattr(
            getattr(order.customer, "userprofile", None), "hostel", "") or "",
        "customer_room": getattr(
            getattr(order.customer, "userprofile", None), "room", "") or "",

        "delivery_address": order.delivery_address,
        "landmark": order.landmark,
        "payment_method": order.payment_method,
        "note": order.order_note,
        "subtotal": float(order.subtotal),
        "discount": float(order.discount),
        "total": float(order.total_amount),
        "status": order.status,
        "delivery_otp": (
            order.delivery_otp
            if include_otp
            and order.status == "out_for_delivery"
            and not order.otp_verified
            else ""
        ),
        "created_at_iso": iso(order.created_at),
        "updated_at_iso": iso(order.updated_at),
    }
    if include_items:
        data["items"] = [
            {
                "name": item.item_name,
                "price": float(item.price),
                "quantity": item.quantity,
            }
            for item in order.items.all()
        ]
    return data


def serialize_food_item(item):
    return {
        "id": item.id,
        "name": item.name,
        "price": float(item.price),
        "description": item.description,
        "image_url": media_url(item.image),
        "category": item.category,
        "vendor_id": item.vendor_id,
        "vendor_name": item.vendor.business_name if item.vendor else "",
        "vendor_logo": (media_url(item.vendor.logo)
                        if item.vendor and item.vendor.logo else ""),
        "is_available": item.is_available,
        "is_veg": item.is_veg,
        "stock": item.stock,
    }


def serialize_message(message, user):
    if message.video:
        kind = "video"
    elif message.image:
        kind = "image"
    elif message.attachment:
        kind = "attachment"
    else:
        kind = "text"
    return {
        "id": message.id,
        "username": message.user.username,
        "display_name": message.user.get_full_name() or message.user.username,
        "content": message.content,
        "kind": kind,
        "image_url": media_url(message.image),
        "video_url": media_url(message.video),
        "like_count": message.likes.count(),
        "liked_by_me": message.likes.filter(id=user.id).exists(),
        "pinned": message.is_pinned,
        "created_at_iso": iso(message.created_at),
    }


# ---------------------------------------------------------------------
# ⭐ ORIGINAL MAIN-AUTH (JSON): login step1/2 + register + OTP + step3 —
# Exact mirror of the myapp/views web flow, for Flutter.
# ---------------------------------------------------------------------
_LOGIN_CAPTCHA = {}  # uid -> captcha code
_REG_OTP = {}  # user_id -> {full_name,email,password,otp,created_at}
# ⭐ Mobile-friendly captcha: no 0/O/1/I/L confusion, case-insensitive match
_CAPTCHA_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _new_login_captcha(uid):
    code = "".join(random.choices(_CAPTCHA_CHARS, k=4))
    _LOGIN_CAPTCHA[uid] = code
    return code


def _find_user_any_case(uid):
    """⭐ UID case-insensitive: 25LBCS3056 / 25lbcs3056 same account."""
    user = User.objects.filter(username=uid).first()
    if user:
        return user
    return User.objects.filter(username__iexact=uid).first()


@csrf_exempt
@require_http_methods(["POST"])
@throttle("login1", 600, 600, body_field="uid", target_limit=30)
def api_login_step1(request):
    body = json_body(request)
    uid = str(body.get("uid", "")).strip()
    if not uid:
        return fail("Enter your User ID.")
    if not _find_user_any_case(uid):
        return ok({"registered": False})
    code = _new_login_captcha(uid)
    return ok({"registered": True, "captcha": code})


@csrf_exempt
@require_http_methods(["POST"])
@throttle("login2", 600, 600, body_field="uid", target_limit=12)
def api_login_step2(request):
    body = json_body(request)
    uid = str(body.get("uid", "")).strip()
    password = str(body.get("password", ""))
    captcha = str(body.get("captcha", "")).strip()
    user = _find_user_any_case(uid)
    if user is None:
        return fail("User not registered. Please register first.")
    correct = _LOGIN_CAPTCHA.get(uid, "")
    # ⭐ case-insensitive: mobile keyboards auto-capitalise input
    pw_ok = user.check_password(password)
    cap_ok = bool(captcha) and bool(correct) and \
        captcha.upper() == correct.upper()
    # ⭐ v65 security: never log captcha values or auth outcomes in detail
    print(f"[AUTH] login2 uid={uid!r} ok={pw_ok and cap_ok}")
    if pw_ok and cap_ok:
        _LOGIN_CAPTCHA.pop(uid, None)
        from .security import _BUCKETS, _LOCK
        with _LOCK:  # successful login clears the failed-attempt counter
            _BUCKETS.pop(f"login2:tgt:{uid.lower()[:80]}", None)
        # ⭐ v70: one phone at a time
        device_id = _device_id(request, body)
        busy = _session_guard(user, device_id)
        if busy:
            return fail(busy)
        token, _ = Token.objects.get_or_create(user=user)
        _session_start(user, device_id, token.key)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        return ok({
            "token": token.key,
            "uid": user.username,
            "email": user.email,
            "name": (profile.full_name if profile and profile.full_name
                     else user.get_full_name() or user.username),
            "need_step3": not (profile.phone and profile.branch),
            "photo_url": media_url(profile.profile_photo),
            "phone": (profile.phone if profile else "") or "",
            "branch": (profile.branch if profile else "") or "",
            "year": (profile.year if profile else "") or "",
        })
    code = _new_login_captcha(uid)
    if cap_ok and not pw_ok:
        msg = "Incorrect password - please check again."
    elif pw_ok and not cap_ok:
        msg = "Incorrect captcha - try the new captcha."
    else:
        msg = "Invalid Password or Captcha."
    return JsonResponse(
        {"ok": False, "error": msg,
         "captcha": code}, status=400)


# ---------------------------------------------------------------------
# ⭐ HOSTEL ESSENTIALS 8-in-1 PACK (₹1799) — store card + order + vendor
# ---------------------------------------------------------------------
HOSTEL_PACK_ITEMS = [
    "Mattress", "Pillow", "Bucket", "Bathing Jug",
    "Rope", "Clothes Clips", "Hanger", "Foot Mat",
]
HOSTEL_PACK_PRICE = 1799


def _hostel_vendor():
    from myapp.models import VendorProfile

    return VendorProfile.objects.filter(
        vendor_type="hostel", is_active=True).first()


def _serialize_hostel_order(o, reveal_mobile=False):
    """The student's number stays hidden from the vendor until reveal_mobile=True."""
    return {
        "id": o.id,
        "order_no": o.order_no,
        "orderer_uid": o.orderer_uid,
        "orderer_name": o.orderer_name,
        "orderer_mobile": o.orderer_mobile if reveal_mobile else "",
        "recipient_name": o.recipient_name,
        "recipient_mobile": o.recipient_mobile if reveal_mobile else "",
        "address": o.address,
        "payment_ref": o.payment_ref,
        "customer_upi": o.customer_upi,
        "txn_last4": o.txn_last4,
        "txn_id": o.txn_id,
        "paid": o.paid,
        "status": o.status,
        "total": float(o.total),
        "items": o.items or [],
        "created_at": o.created_at.strftime("%d %b, %I:%M %p"),
    }


# ⭐ v60: default products seeded once so the redesigned store is never
# empty — the admin portal can edit/delete/extend them freely afterwards.
_HOSTEL_DEFAULT_PRODUCTS = [
    ("Mattress", 649, "🛏️",
     "Single-bed hostel mattress — comfortable, durable foam that fits "
     "the standard CU hostel bed frame."),
    ("Pillow", 149, "🛌",
     "Soft fibre pillow with a breathable cover — ready to use from "
     "night one."),
    ("Bucket", 129, "🪣",
     "Sturdy 18L plastic bucket for bathing and laundry."),
    ("Bathing Jug", 49, "🚿",
     "Strong plastic bathing jug that pairs with the bucket."),
    ("Rope", 59, "🪢",
     "5-metre clothesline rope for drying clothes in your room or "
     "balcony."),
    ("Clothes Clips", 49, "🧷",
     "Pack of 12 strong clothes clips that grip in wind."),
    ("Hanger", 99, "👕",
     "Set of 6 durable hangers for shirts, jackets and trousers."),
    ("Foot Mat", 79, "🩴",
     "Anti-slip doormat that keeps your room dust-free."),
]


def _ensure_hostel_products():
    """Seeds the default hostel products once (idempotent)."""
    from myapp.models import HostelProduct
    if HostelProduct.objects.exists():
        return
    for i, (name, mrp, emoji, desc) in enumerate(_HOSTEL_DEFAULT_PRODUCTS):
        HostelProduct.objects.create(
            name=name, mrp=mrp, emoji=emoji, description=desc, order=i)


def _serialize_hostel_product(p):
    return {
        "id": p.id,
        "name": p.name,
        "mrp": float(p.mrp),
        "description": p.description,
        "emoji": p.emoji or "🛒",
        "is_active": p.is_active,
        # ⭐ v61: live stock (0 = unlimited/off, like food items)
        "stock": p.stock,
        "order": p.order,
        "photos": [
            {"id": ph.id, "url": media_url(ph.image)}
            for ph in p.photos.all()],
    }


@student_required
def store_hostel(request, user):
    """Product list + logged-in user's AUTO details + vendor UPI."""
    from myapp.models import HostelProduct
    _ensure_hostel_products()
    profile = getattr(user, "userprofile", None)
    vendor = _hostel_vendor()
    return ok({
        # legacy keys kept so older app versions keep working
        "items": HOSTEL_PACK_ITEMS,
        "price": HOSTEL_PACK_PRICE,
        "worth": 2500,
        "freebie": "FREE Chilled Diet Coke",
        # ⭐ v61: vendor-controlled storefront
        "store_open": vendor.kitchen_open if vendor else True,
        "store_name": vendor.business_name if vendor else "Hostel Essentials",
        "store_description": (vendor.store_description
                              if vendor else ""),
        # ⭐ v60: individual products (vendor-managed since v61)
        "products": [
            _serialize_hostel_product(p)
            for p in HostelProduct.objects.filter(
                is_active=True).prefetch_related("photos")],
        "upi_id": (vendor.upi_id.strip()
                   if vendor and vendor.upi_id.strip() else ""),
        # ⭐ v55: uploaded QR counts as payment-enabled without a UPI ID.
        "has_qr": bool(vendor and vendor.upi_qr_image),
        "vendor_id": vendor.id if vendor else None,
        "auto": {
            "uid": user.username,
            "name": (profile.full_name
                     if profile and profile.full_name
                     else user.get_full_name() or user.username),
            "mobile": (profile.phone if profile and profile.phone else ""),
            "address": "Chandigarh University",
        },
    })


@csrf_exempt
@student_required
def store_hostel_order(request, user):
    """Order place — orderer auto (login), recipient manual."""
    from myapp.models import HostelOrder

    if not _rl_allow(f"hostelorder:{user.id}", 6, 600):
        return fail("Too many orders in a short time — please wait a "
                    "few minutes.", status=429)

    body = json_body(request)
    recipient_name = str(body.get("recipient_name", "")).strip()
    recipient_mobile = str(body.get("recipient_mobile", "")).strip()
    address = str(body.get("address", "")).strip() or "Chandigarh University"
    payment_ref = str(body.get("payment_ref", "")).strip()
    customer_upi = str(body.get("customer_upi", "")).strip()
    txn_id = str(body.get("txn_id", "")).strip()[:64]
    txn_last4 = (txn_id[-4:] if txn_id
                 else str(body.get("txn_last4", "")).strip()[:4])
    if not recipient_name:
        return fail("Enter the recipient's name.")
    if len(recipient_mobile) < 10:
        return fail("Enter a valid recipient mobile number.")

    # ⭐ v61: store closed = no orders (vendor controls this switch).
    hostel_vendor = _hostel_vendor()
    if hostel_vendor is not None and not hostel_vendor.kitchen_open:
        return fail("The Hostel Essentials store is closed right now — "
                    "please try again later.")

    # ⭐ v60: cart items [{product_id, qty}] — validated server-side
    # against the live product list; the total is computed here, never
    # trusted from the client.
    from myapp.models import HostelProduct
    raw_items = body.get("items") or []
    items = []
    total = 0.0
    picked = []  # (product, qty) for stock deduction after validation
    if raw_items:
        for entry in raw_items:
            try:
                pid = int(entry.get("product_id"))
                qty = max(1, min(99, int(entry.get("qty", 1))))
            except (TypeError, ValueError, AttributeError):
                continue
            p = HostelProduct.objects.filter(id=pid, is_active=True).first()
            if p is None:
                continue
            # ⭐ v61: live stock guard (0 = unlimited, like food items)
            if p.stock > 0 and qty > p.stock:
                return fail(f"Only {p.stock} left of \"{p.name}\" — "
                            "please lower the quantity.")
            items.append({"name": p.name, "mrp": float(p.mrp), "qty": qty})
            total += float(p.mrp) * qty
            picked.append((p, qty))
        if not items:
            return fail("Your cart is empty — add at least one product.")
        # deduct stock; auto-unavailable at 0 (mirrors the food section)
        for p, qty in picked:
            if p.stock > 0:
                p.stock = max(0, p.stock - qty)
                if p.stock == 0:
                    p.is_active = False
                p.save(update_fields=["stock", "is_active"])
    else:
        # legacy pack order from an older app version
        total = float(HOSTEL_PACK_PRICE)

    profile = getattr(user, "userprofile", None)
    order_no = f"HE{int(time.time() * 1000) % 1000000000}"
    order = HostelOrder.objects.create(
        order_no=order_no,
        student=user,
        orderer_uid=user.username,
        orderer_name=(profile.full_name
                      if profile and profile.full_name
                      else user.get_full_name() or user.username),
        orderer_mobile=(profile.phone if profile and profile.phone else ""),
        recipient_name=recipient_name,
        recipient_mobile=recipient_mobile,
        address=address,
        payment_ref=payment_ref,
        customer_upi=customer_upi,
        txn_id=txn_id,
        txn_last4=txn_last4,
        paid=True,
        total=round(total, 2),
        items=items,
    )
    return ok({"order": _serialize_hostel_order(order)})


@student_required
def store_hostel_my_orders(request, user):
    """All of the student's own hostel pack orders."""
    from myapp.models import HostelOrder

    orders = HostelOrder.objects.filter(student=user).order_by("-created_at")
    return ok({"orders": [_serialize_hostel_order(o, reveal_mobile=True) for o in orders]})


@require_http_methods(["GET"])
def vendor_hostel_orders(request):
    """⭐ Hostel vendor portal: all pack orders."""
    from myapp.models import HostelOrder

    user, profile = vendor_user(request)
    if profile is None or profile.vendor_type != "hostel":
        return fail("Login with a hostel vendor account.", status=403)
    orders = HostelOrder.objects.order_by("-created_at")[:200]
    return ok({"orders": [
        _serialize_hostel_order(
            o, reveal_mobile=o.status in ("accepted", "delivered"))
        for o in orders]})


@csrf_exempt
@require_http_methods(["POST"])
def vendor_hostel_order_status(request):
    user, profile = vendor_user(request)
    if profile is None or profile.vendor_type != "hostel":
        return fail("Login with a hostel vendor account.", status=403)
    body = json_body(request)
    oid = body.get("id")
    status = str(body.get("status", "")).strip()
    if status not in ("pending", "accepted", "delivered", "cancelled"):
        return fail("Invalid status.")
    from myapp.models import HostelOrder

    order = HostelOrder.objects.filter(id=oid).first()
    if order is None:
        return fail("Order not found.")
    order.status = status
    order.save(update_fields=["status"])
    if order.user_id:
        _notify(
            user_id=order.user_id,
            title=f"Hostel order {status}",
            message=f"Your hostel essentials order is now {status}.",
        )
    return ok({"order": _serialize_hostel_order(order)})


@csrf_exempt
@require_http_methods(["POST"])
@throttle("forgot", 60, 600, body_field="uid", target_limit=3,
          target_window=600)
def api_forgot_password(request):
    """⭐ Forgot password: registered email pe OTP."""
    from myapp.views import send_cunnect_otp_email

    body = json_body(request)
    uid = str(body.get("uid", "")).strip()
    user = _find_user_any_case(uid)
    if user is None:
        return fail("No account found for this User ID.")
    email = user.email
    if not email:
        return fail("No email on this account - password cannot be reset.")
    otp = UserProfile.generate_otp()
    _REG_OTP[f"reset:{user.username}"] = {
        "otp": otp, "created_at": time.time(), "reset": True}
    try:
        send_cunnect_otp_email(recipient=email, otp=otp,
                               full_name=user.first_name or user.username,
                               user_id=user.username)
    except Exception as exc:
        print(f"[API-RESET] otp email failed: {exc}")
        return fail("Error sending email. Please try again.")
    return ok({"otp_sent": True, "email": email})


@csrf_exempt
@require_http_methods(["POST"])
@throttle("resetpw", 120, 600, body_field="uid", target_limit=8)
def api_reset_password(request):
    """⭐ Verify the OTP and set a new password."""
    body = json_body(request)
    uid = str(body.get("uid", "")).strip()
    entered = str(body.get("otp", "")).strip()
    new_password = str(body.get("password", ""))
    user = _find_user_any_case(uid)
    if user is None:
        return fail("Account not found.")
    key = f"reset:{user.username}"
    temp = _REG_OTP.get(key)
    if not temp:
        return fail("Tap 'Send OTP' first.")
    if time.time() - float(temp.get("created_at", 0)) > 300:
        _REG_OTP.pop(key, None)
        return fail("OTP expired - send a new one.")
    if temp["otp"] != entered:
        temp["tries"] = int(temp.get("tries", 0)) + 1
        if temp["tries"] >= 6:
            _REG_OTP.pop(key, None)
            return fail("Too many wrong attempts - send a new OTP.")
        return fail("OTP is wrong")
    if len(new_password) < 6:
        return fail("Keep the password at least 6 characters long.")
    user.set_password(new_password)
    user.save()
    _REG_OTP.pop(key, None)
    return ok({"reset": True})


@csrf_exempt
@require_http_methods(["POST"])
@throttle("forgotlink", 60, 600, body_field="uid", target_limit=3,
          target_window=600)
def api_forgot_link(request):
    """⭐ v58: forgot password — email a secure reset LINK (not an OTP).
    Accepts a student User ID or a vendor's registered phone number."""
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.http import urlsafe_base64_encode
    from django.utils.encoding import force_bytes
    from django.core.mail import EmailMultiAlternatives
    from django.conf import settings

    body = json_body(request)
    identifier = str(body.get("identifier",
                              body.get("uid", ""))).strip()
    if not identifier:
        return fail("Enter your User ID (or registered phone).")
    user = _find_user_any_case(identifier)
    if user is None:
        vp = VendorProfile.objects.filter(
            phone=identifier).select_related("user").first()
        if vp is not None:
            user = vp.user
    # ⭐ v59: people often type their EMAIL here — accept that too.
    if user is None and "@" in identifier:
        user = User.objects.filter(email__iexact=identifier).first()
    if user is None:
        return fail("No account found for this User ID / phone / email.")
    email_addr = (user.email or "").strip()
    if not email_addr:
        return fail("No email on this account — contact support.")

    token = default_token_generator.make_token(user)
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    public_url = getattr(settings, "CUNNECT_PUBLIC_URL",
                         "https://cunnect-backend.onrender.com").rstrip("/")
    link = f"{public_url}/api/auth/reset/{uidb64}/{token}/"
    logo_url = f"{public_url}/static/images/cunnect_email_logo_black.png"
    name = user.first_name or user.username

    plain = (f"Hi {name},\n\nTap the link below to set a new CUnnect "
             f"password:\n{link}\n\nThis link is valid for 24 hours and "
             "can be used once.\n\nIf you didn't ask for this, you can "
             "safely ignore this email.")
    html = f"""
    <!doctype html>
    <html>
      <body style="margin:0;padding:0;background:#000000;
                   font-family:Arial,sans-serif;color:#ffffff;">
        <table role="presentation" width="100%" cellspacing="0"
               cellpadding="0" style="background:#000000;padding:28px 12px;">
          <tr><td align="center">
            <table role="presentation" width="100%" cellspacing="0"
                   cellpadding="0" style="max-width:560px;background:#000000;
                   border:0;overflow:hidden;">
              <tr><td>
                <img src="{logo_url}" alt="CUnnect" width="560"
                     style="display:block;width:100%;max-width:560px;
                            height:auto;">
              </td></tr>
              <tr><td style="padding:28px 30px 32px;">
                <p style="margin:0 0 8px;font-size:16px;line-height:1.55;
                          color:#f2f2f2;">
                  Locked out? Happens to the best of us.<br>
                  Let's get you back in.
                </p>
                <div style="margin:25px 0 20px;padding:22px 17px;
                            border:1px solid #f10b1d;border-radius:12px;
                            background:#250d11;text-align:center;">
                  <div style="margin-bottom:14px;font-size:12px;
                              font-weight:700;letter-spacing:1.5px;
                              color:#ff9ca5;">
                    🔐 RESET YOUR PASSWORD
                  </div>
                  <a href="{link}" style="display:inline-block;
                     background:#f10b1d;color:#ffffff;text-decoration:none;
                     font-size:14px;font-weight:800;letter-spacing:.6px;
                     padding:13px 34px;border-radius:10px;">
                    SET NEW PASSWORD
                  </a>
                </div>
                <p style="margin:0 0 20px;font-size:14px;color:#d0d0d0;">
                  ⏳ The link works <strong style="color:#ffffff;">once</strong>
                  and expires soon — use it right away.
                </p>
                <p style="margin:0;font-size:13px;line-height:1.55;
                          color:#9a9a9a;">
                  Didn't request this? Ignore this email —
                  your password stays unchanged.
                </p>
              </td></tr>
            </table>
          </td></tr>
        </table>
      </body>
    </html>
    """
    try:
        msg = EmailMultiAlternatives(
            subject="Reset your CUnnect password 🔐",
            body=plain,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email_addr],
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)
    except Exception as exc:
        print(f"[API-RESET-LINK] email failed: {exc}")
        return fail("Error sending email. Please try again.")
    at = email_addr.find("@")
    masked = (email_addr[:2] + "****" + email_addr[at - 1:]
              if at > 3 else email_addr)
    return ok({"sent": True, "email": masked})


_RESET_PAGE_STYLE = """
      margin:0;background:#000;color:#fff;
      font-family:Arial,Helvetica,sans-serif;min-height:100vh;
      display:flex;align-items:center;justify-content:center;
"""


def _reset_page_html(inner):
    """Black/red CUnnect-themed shell for the reset pages."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CUnnect — Reset Password</title></head>
<body style="{_RESET_PAGE_STYLE}">
  <div style="width:100%;max-width:400px;padding:20px;">
    <div style="text-align:center;margin-bottom:18px;">
      <span style="font-size:26px;font-weight:800;letter-spacing:1px;">
        CU<span style="color:#f10b1d;">nnect</span></span>
    </div>
    <div style="background:#111;border:1px solid #f10b1d;
                border-radius:16px;padding:26px 22px;">
      {inner}
    </div>
  </div>
</body></html>"""


def _reset_bounce_script(app_link):
    """⭐ v66: open the CUnnect app from ANY mail browser.

    1) plain custom scheme (works in Chrome/Safari/Firefox),
    2) intent:// — Gmail/Chrome Custom Tabs block plain scheme jumps
       from a page, this form resolves to the package reliably,
    3) the visible button stays as the always-works user gesture.
    """
    rest = app_link.split("://", 1)[-1]
    intent = (f"intent://{rest}#Intent;scheme=cunnect;"
              f"package=com.cunnect.cunnect_food;end")
    return (
        f"<script>"
        f"var _d=document,_went=false;"
        f"window.addEventListener('pagehide',function(){{_went=true}});"
        f"document.addEventListener('visibilitychange',function(){{"
        f"if(document.hidden)_went=true}});"
        f"setTimeout(function(){{"
        f"if(_went)return;window.location.replace('{app_link}');}},300);"
        f"setTimeout(function(){{"
        f"if(_went)return;window.location.replace('{intent}');}},1400);"
        f"</script>")


@csrf_exempt
def api_reset_page(request, uidb64, token):
    """⭐ v63/v66: the reset link target — NO web form anymore. The page
    bounces straight into the CUnnect APP via the cunnect:// deep link;
    the new password is set on the in-app screen.

    ⭐ v66: an EXPIRED link also bounces into the app (as
    cunnect://reset/expired/1) — the user is never left sitting on a
    website page, they get the in-app "request a new link" screen."""
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.http import urlsafe_base64_decode
    from django.http import HttpResponse

    try:
        uid_pk = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=uid_pk)
    except Exception:
        user = None
    if user is None or not default_token_generator.check_token(user, token):
        # ⭐ v66: expired/used link -> open the APP and ask for a new one.
        app_link = "cunnect://reset/expired/1"
        return HttpResponse(_reset_page_html(
            "<h2 style='margin:0 0 10px;font-size:19px;text-align:center;'>"
            "Link expired</h2>"
            "<p style='margin:0 0 22px;color:#9a9a9a;font-size:12.5px;"
            "line-height:1.55;text-align:center;'>This reset link is invalid "
            "or was already used. Tap the button and the CUnnect app will "
            "open — you can send yourself a fresh link in one tap.</p>"
            f"<a href='{app_link}' style='display:block;text-align:center;"
            f"background:#f10b1d;color:#fff;text-decoration:none;"
            f"border-radius:10px;padding:15px;font-size:14px;font-weight:800;"
            f"letter-spacing:.5px;'>OPEN CUNNECT APP</a>"
            f"<p style='margin:18px 0 0;color:#7a7a7a;font-size:11px;"
            f"line-height:1.55;text-align:center;'>Nothing happens? Make sure "
            f"the CUnnect app is installed on this phone, then tap the button "
            f"again.</p>"
            + _reset_bounce_script(app_link)))

    app_link = f"cunnect://reset/{uidb64}/{token}"
    return HttpResponse(_reset_page_html(
        f"<h2 style='margin:0 0 6px;font-size:19px;text-align:center;'>"
        f"Continue in the app</h2>"
        f"<p style='margin:0 0 22px;color:#9a9a9a;font-size:12.5px;"
        f"line-height:1.55;text-align:center;'>Hi <b style='color:#fff;'>"
        f"{_esc(user.username)}</b> — tap the button below and the CUnnect app "
        f"will open so you can set your new password securely.</p>"
        f"<a href='{app_link}' style='display:block;text-align:center;"
        f"background:#f10b1d;color:#fff;text-decoration:none;"
        f"border-radius:10px;padding:15px;font-size:14px;font-weight:800;"
        f"letter-spacing:.5px;'>OPEN CUNNECT APP</a>"
        f"<p style='margin:18px 0 0;color:#7a7a7a;font-size:11px;"
        f"line-height:1.55;text-align:center;'>Nothing happens? Make sure "
        f"the CUnnect app is installed on this phone, then tap the button "
        f"again.</p>"
        + _reset_bounce_script(app_link)))


@csrf_exempt
@require_http_methods(["POST"])
@throttle("resetlink", 120, 600)
def api_reset_link_password(request):
    """⭐ v63: called from INSIDE the app — the deep-linked reset screen
    posts uidb64+token+new password here."""
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.http import urlsafe_base64_decode

    body = json_body(request)
    uidb64 = str(body.get("uidb64", "")).strip()
    token = str(body.get("token", "")).strip()
    password = str(body.get("password", ""))
    try:
        uid_pk = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=uid_pk)
    except Exception:
        user = None
    if user is None or not default_token_generator.check_token(user, token):
        return fail("This reset link is invalid or was already used. "
                    "Tap 'Forgot password?' again for a fresh one.")
    if len(password) < 6:
        return fail("Keep the password at least 6 characters long.")
    user.set_password(password)
    user.save()
    return ok({"reset": True, "user_id": user.username})


@csrf_exempt
@require_http_methods(["POST"])
@throttle("register", 60, 3600, body_field="email", target_limit=4)
def api_register(request):
    from myapp.views import send_cunnect_otp_email

    body = json_body(request)
    full_name = str(body.get("full_name", "")).strip()
    user_id = str(body.get("user_id", "")).strip()
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))
    if not (full_name and user_id and email and password):
        return fail("Fill in all the details.")
    # ⭐ v65 security: strict UID format (blocks scripts/emoji/path tricks)
    if not valid_user_id(user_id):
        return fail("User ID can only contain letters and numbers "
                    "(3-30 characters).")
    full_name = clean_name(full_name)
    if not full_name:
        return fail("Enter your real name.")
    if len(password) < 6:
        return fail("Keep the password at least 6 characters long.")
    if len(email) > 100 or email.count("@") != 1:
        return fail("Enter a valid email address.")
    if not email.lower().endswith("@culkomail.in"):
        return fail("Please use your official CULKO email ID ending with "
                    "@culkomail.in. Example: 25lbcs3056@culkomail.in")
    if User.objects.filter(username=user_id).exists():
        return fail("User ID already exists!")
    if User.objects.filter(email=email).exists():
        return fail("Email already registered!")
    otp = UserProfile.generate_otp()
    _REG_OTP[user_id] = {
        "user_id": user_id, "full_name": full_name, "email": email,
        "password": password, "otp": otp, "created_at": time.time(),
    }
    try:
        send_cunnect_otp_email(recipient=email, otp=otp,
                               full_name=full_name, user_id=user_id)
    except Exception as exc:
        print(f"[API-REG] otp email failed: {exc}")
        return fail("Error sending email. Please try again.")
    return ok({"otp_sent": True, "user_id": user_id})


@csrf_exempt
@require_http_methods(["POST"])
@throttle("otpverify", 300, 600, body_field="user_id", target_limit=8)
def api_otp_verify(request):
    body = json_body(request)
    user_id = str(body.get("user_id", "")).strip()
    entered = str(body.get("otp", "")).strip()
    temp = _REG_OTP.get(user_id)
    if not temp:
        return fail("Session expired. Please register again.")
    if time.time() - float(temp.get("created_at", 0)) > 300:
        _REG_OTP.pop(user_id, None)
        return fail("OTP expired after 5 minutes. Please register again.")
    if temp["otp"] != entered:
        # ⭐ v65 security: 6 wrong tries burn the OTP (no infinite guessing)
        temp["tries"] = int(temp.get("tries", 0)) + 1
        if temp["tries"] >= 6:
            _REG_OTP.pop(user_id, None)
            return fail("Too many wrong attempts. Please register again.")
        return fail("OTP is wrong")
    try:
        user = User.objects.create_user(
            username=temp.get("user_id") or user_id, email=temp["email"],
            password=temp["password"], first_name=temp["full_name"])
        UserProfile.objects.create(user=user, is_verified=True)
    except Exception as error:
        return fail(f"Database error: {error}")
    _REG_OTP.pop(user_id, None)
    return ok({"registered": True})


@csrf_exempt
@require_http_methods(["POST"])
@throttle("otpresend", 60, 600, body_field="user_id", target_limit=3,
          target_window=300)
def api_resend_otp(request):
    from myapp.views import send_cunnect_otp_email

    body = json_body(request)
    user_id = str(body.get("user_id", "")).strip()
    temp = _REG_OTP.get(user_id)
    if not temp:
        return fail("Session expired. Please register again.")
    new_otp = UserProfile.generate_otp()
    temp["otp"] = new_otp
    temp["created_at"] = time.time()
    try:
        send_cunnect_otp_email(recipient=temp["email"], otp=new_otp,
                               full_name=temp.get("full_name", ""),
                               user_id=user_id, is_resend=True)
    except Exception:
        return fail("Failed to resend OTP.")
    return ok({"resent": True})


@csrf_exempt
@student_required
def api_complete_profile(request, user):
    if request.method != "POST":
        return fail("POST required.", status=405)
    body = json_body(request)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    full_name = str(body.get("full_name", "")).strip()
    if hasattr(profile, "full_name"):
        profile.full_name = full_name
    if full_name:
        user.first_name = full_name
        user.save(update_fields=["first_name"])
    profile.phone = str(body.get("phone", "")).strip()
    profile.dob = str(body.get("dob", "")).strip() or None
    profile.gender = str(body.get("gender", "")).strip()
    profile.branch = str(body.get("branch", "")).strip()
    profile.year = str(body.get("year", "")).strip()
    profile.stay_type = str(body.get("stay_type", "")).strip()
    profile.consent = bool(body.get("consent"))
    # ⭐ profile photo (base64 data-uri ya raw base64) -> media file
    photo_b64 = str(body.get("photo", "") or "").strip()
    if photo_b64:
        if photo_b64.startswith("data:") and "," in photo_b64:
            photo_b64 = photo_b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(photo_b64)
            # ⭐ v65 security: must actually BE an image (magic bytes)
            is_img = (raw.startswith(b"\xff\xd8\xff")      # jpeg
                      or raw.startswith(b"\x89PNG")           # png
                      or raw.startswith(b"RIFF"))              # webp
            if 0 < len(raw) < 3_000_000 and is_img:
                from django.core.files.base import ContentFile
                name = f"{user.username}_{int(time.time())}.jpg"
                profile.profile_photo.save(name, ContentFile(raw), save=False)
        except Exception as exc:  # invalid base64 -> ignore, save the rest of the profile
            print(f"[AUTH] photo save failed: {exc}")
    # ⭐ photo mandatory — step 3 cannot complete without a photo
    if not profile.profile_photo:
        return fail("Profile photo upload is required.")
    profile.save()
    return ok({
        "completed": True,
        "email": user.email,
        "photo_url": media_url(profile.profile_photo),
        "phone": profile.phone or "",
        "branch": profile.branch or "",
        "year": profile.year or "",
    })


# ---------------------------------------------------------------------
# Student auth + dashboard
# ---------------------------------------------------------------------


@csrf_exempt
@require_http_methods(["POST"])
@throttle("slogin", 600, 600, body_field="uid", target_limit=12)
def student_login(request):
    body = json_body(request)
    uid = str(body.get("uid", "")).strip()
    password = str(body.get("password", ""))
    if not uid or not password:
        return fail("Both UID and password are required.")
    # ⭐ Disabled accounts get a clear, dedicated message.
    from myapp.models import DisabledAccount
    blocked = User.objects.filter(username=uid, is_active=False).first()
    if blocked is not None and DisabledAccount.objects.filter(
            user=blocked).exists():
        return fail("Your account has been disabled by the CUnnect team.",
                    status=403)
    user = authenticate(request, username=uid, password=password)
    if user is None:
        if blocked is not None:
            return fail("Your account has been disabled by the CUnnect team.",
                        status=403)
        return fail("Invalid UID or password.", status=401)
    # ⭐ v70: one phone at a time (same rule as the main login)
    device_id = _device_id(request, body)
    busy = _session_guard(user, device_id)
    if busy:
        return fail(busy)
    token, _ = Token.objects.get_or_create(user=user)
    _session_start(user, device_id, token.key)
    profile = getattr(user, "userprofile", None)
    return ok({
        "token": token.key,
        "uid": user.username,
        "name": (profile.full_name if profile and profile.full_name
                 else user.get_full_name() or user.username),
        "phone": (profile.phone if profile else "") or "",
        "branch": (profile.branch if profile else "") or "",
        "year": (profile.year if profile else "") or "",
        "email": user.email or "",
        "photo_url": media_url(profile.profile_photo) if profile else "",
    })


@csrf_exempt
@student_required
@throttle("logout", 60, 600)
def auth_logout(request, user):
    """⭐ v70: Logout — frees the account so another phone can sign in.

    The auth token is deleted too, so the session cannot be replayed.
    """
    if request.method != "POST":
        return fail("POST required.", status=405)
    try:
        Token.objects.filter(user_id=user.id).delete()
    except Exception:
        pass
    _session_end(user)
    return ok({"logged_out": True})


@student_required
def student_dashboard(request, user):
    banners = Banner.objects.filter(is_active=True).order_by("order")
    return ok({
        "banners": [
            {
                "id": banner.id,
                "title": banner.title,
                "image_url": media_url(banner.image),
                # ⭐ v61: video banners on the student home carousel
                "video_url": media_url(banner.video),
                "is_video": bool(banner.video),
            }
            for banner in banners
        ],
    })


# ---------------------------------------------------------------------
# ⭐ CUnnect Feed: Notices + Polls + Reactions + Comments
# ---------------------------------------------------------------------


def _feed_social(kind, ids, user):
    """Reactions (emoji -> count) + my reaction + comment count, per object."""
    from myapp.models import FeedReaction, FeedComment
    from django.db.models import Count

    out = {i: {"reactions": {}, "my_reaction": "", "comment_count": 0}
           for i in ids}
    if not ids:
        return out
    rows = (FeedReaction.objects
            .filter(kind=kind, object_id__in=ids)
            .values("object_id", "emoji").annotate(c=Count("id")))
    for r in rows:
        out[r["object_id"]]["reactions"][r["emoji"]] = r["c"]
    mine = FeedReaction.objects.filter(
        kind=kind, object_id__in=ids, user=user)
    for m in mine:
        out[m.object_id]["my_reaction"] = m.emoji
    rows = (FeedComment.objects
            .filter(kind=kind, object_id__in=ids)
            .values("object_id").annotate(c=Count("id")))
    for r in rows:
        out[r["object_id"]]["comment_count"] = r["c"]
    return out


MAX_PINNED_POSTS = 15


@student_required
def notices_list(request, user):
    from myapp.models import Notice

    notices = list(Notice.objects.filter(is_active=True)[:50])
    social = _feed_social("notice", [n.id for n in notices], user)
    return ok({
        "notices": [
            {
                "id": n.id,
                "title": n.title,
                "message": n.message,
                "image_url": media_url(n.image),
                "video_url": media_url(n.video),
                "pinned": n.pinned_at is not None,
                "pinned_at_iso": iso(n.pinned_at) if n.pinned_at else "",
                "created_at_iso": iso(n.created_at),
                **social[n.id],
            }
            for n in notices
        ],
    })


@csrf_exempt
@student_required
def feed_react(request, user, kind, object_id):
    """⭐ WhatsApp-style emoji reaction — any emoji. Sending the same emoji
    again REMOVES the reaction (toggle)."""
    if request.method != "POST":
        return fail("POST required.", status=405)
    if kind not in ("notice", "poll"):
        return fail("Unknown kind.", status=404)
    from myapp.models import FeedReaction

    emoji = str(json_body(request).get("emoji", "")).strip()[:16]
    if not emoji:
        return fail("Emoji required.")
    existing = FeedReaction.objects.filter(
        kind=kind, object_id=object_id, user=user).first()
    if existing and existing.emoji == emoji:
        existing.delete()          # toggle off
    else:
        FeedReaction.objects.update_or_create(
            kind=kind, object_id=object_id, user=user,
            defaults={"emoji": emoji})
    social = _feed_social(kind, [object_id], user)[object_id]
    return ok(social)


def _display_name(u):
    try:
        p = getattr(u, "userprofile", None)
        if p is not None and getattr(p, "full_name", ""):
            return p.full_name
    except Exception:
        pass
    return u.get_full_name() or u.username


def _comments_payload(kind, object_id, user):
    """Threaded comments (top level + one reply level) with emoji
    reactions per comment."""
    from myapp.models import FeedComment, FeedCommentReaction
    from django.db.models import Count

    comments = list(FeedComment.objects.filter(
        kind=kind, object_id=object_id).select_related("user")[:400])
    ids = [c.id for c in comments]
    reactions = {i: {} for i in ids}
    mine = {i: "" for i in ids}
    if ids:
        for r in (FeedCommentReaction.objects
                  .filter(comment_id__in=ids)
                  .values("comment_id", "emoji").annotate(c=Count("id"))):
            reactions[r["comment_id"]][r["emoji"]] = r["c"]
        for r in FeedCommentReaction.objects.filter(
                comment_id__in=ids, user=user):
            mine[r.comment_id] = r.emoji

    def row(c):
        return {
            "id": c.id,
            "user": _display_name(c.user),
            "mine": c.user_id == user.id,
            "text": c.text,
            "reactions": reactions.get(c.id, {}),
            "my_reaction": mine.get(c.id, ""),
            "created_at_iso": iso(c.created_at),
        }

    top = [c for c in comments if c.parent_id is None]
    replies = {}
    for c in comments:
        if c.parent_id is not None:
            replies.setdefault(c.parent_id, []).append(row(c))
    return {
        "comments": [
            {**row(c), "replies": replies.get(c.id, [])} for c in top
        ],
        "total": len(comments),
    }


@student_required
def feed_comments(request, user, kind, object_id):
    if kind not in ("notice", "poll"):
        return fail("Unknown kind.", status=404)
    return ok(_comments_payload(kind, object_id, user))


@csrf_exempt
@student_required
def feed_comment_add(request, user, kind, object_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    if kind not in ("notice", "poll"):
        return fail("Unknown kind.", status=404)
    from myapp.models import FeedComment

    body = json_body(request)
    text = str(body.get("text", "")).strip()[:600]
    if text and not _rl_allow(f"comment:{user.id}", 15, 60):
        return fail("You are commenting too fast.", status=429)
    if not text:
        return fail("Comment cannot be empty.")
    parent = None
    parent_id = body.get("parent_id")
    if parent_id:
        parent = FeedComment.objects.filter(
            id=parent_id, kind=kind, object_id=object_id).first()
        if parent is None:
            return fail("The comment you replied to no longer exists.",
                        status=404)
        # Keep the thread one level deep (replies to a reply attach to the
        # top-level comment, WhatsApp/Instagram style).
        if parent.parent_id is not None:
            parent = parent.parent
    FeedComment.objects.create(
        kind=kind, object_id=object_id, user=user, text=text, parent=parent)
    return ok(_comments_payload(kind, object_id, user))


@csrf_exempt
@student_required
def feed_comment_delete(request, user, kind, object_id, comment_id):
    """Users can delete their OWN comments (replies are removed with them).
    Staff can delete any comment."""
    if request.method != "POST":
        return fail("POST required.", status=405)
    if kind not in ("notice", "poll"):
        return fail("Unknown kind.", status=404)
    from myapp.models import FeedComment

    comment = FeedComment.objects.filter(
        id=comment_id, kind=kind, object_id=object_id).first()
    if comment is None:
        return fail("Comment not found.", status=404)
    if comment.user_id != user.id and not (user.is_staff or user.is_superuser):
        return fail("You can only delete your own comments.", status=403)
    comment.delete()
    return ok(_comments_payload(kind, object_id, user))


@csrf_exempt
@student_required
def feed_comment_react(request, user, kind, object_id, comment_id):
    """Emoji reaction on a comment — same emoji again toggles it off."""
    if request.method != "POST":
        return fail("POST required.", status=405)
    if kind not in ("notice", "poll"):
        return fail("Unknown kind.", status=404)
    from myapp.models import FeedComment, FeedCommentReaction

    comment = FeedComment.objects.filter(
        id=comment_id, kind=kind, object_id=object_id).first()
    if comment is None:
        return fail("Comment not found.", status=404)
    emoji = str(json_body(request).get("emoji", "")).strip()[:16]
    if not emoji:
        return fail("Emoji required.")
    existing = FeedCommentReaction.objects.filter(
        comment=comment, user=user).first()
    if existing and existing.emoji == emoji:
        existing.delete()
    else:
        FeedCommentReaction.objects.update_or_create(
            comment=comment, user=user, defaults={"emoji": emoji})
    return ok(_comments_payload(kind, object_id, user))


def _serialize_poll(poll, user, social=None):
    from myapp.models import AppPollVote

    votes_by_option = {}
    total = 0
    for v in poll.votes.all():
        votes_by_option[v.option_id] = votes_by_option.get(v.option_id, 0) + 1
        total += 1
    my_vote = AppPollVote.objects.filter(poll=poll, user=user).first()
    if social is None:
        social = _feed_social("poll", [poll.id], user)[poll.id]
    return {
        **social,
        "id": poll.id,
        "question": poll.question,
        "image_url": media_url(poll.image),
        "video_url": media_url(poll.video),
        "pinned": poll.pinned_at is not None,
        "pinned_at_iso": iso(poll.pinned_at) if poll.pinned_at else "",
        "is_active": poll.is_active,
        "created_at_iso": iso(poll.created_at),
        "total_votes": total,
        "my_option_id": my_vote.option_id if my_vote else None,
        "options": [
            {
                "id": o.id,
                "text": o.text,
                "image_url": media_url(o.image),
                "votes": votes_by_option.get(o.id, 0),
            }
            for o in poll.options.all()
        ],
    }


@student_required
def polls_list(request, user):
    from myapp.models import AppPoll

    polls = list(AppPoll.objects.filter(is_active=True).prefetch_related(
        "options", "votes")[:20])
    social = _feed_social("poll", [p.id for p in polls], user)
    return ok({"polls": [
        _serialize_poll(p, user, social=social[p.id]) for p in polls]})


@csrf_exempt
@student_required
def poll_vote(request, user, poll_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    from myapp.models import AppPoll, AppPollOption, AppPollVote

    poll = AppPoll.objects.filter(id=poll_id, is_active=True).first()
    if poll is None:
        return fail("Poll not found or closed.", status=404)
    body = json_body(request)
    option_id = body.get("option_id")
    option = AppPollOption.objects.filter(id=option_id, poll=poll).first()
    if option is None:
        return fail("Invalid option.")
    # ⭐ one vote per user — voting again switches the chosen option
    AppPollVote.objects.update_or_create(
        poll=poll, user=user, defaults={"option": option})
    poll = AppPoll.objects.prefetch_related("options", "votes").get(id=poll.id)
    return ok({"poll": _serialize_poll(poll, user)})


@csrf_exempt
@student_required
def student_support(request, user):
    if request.method != "POST":
        return fail("POST required.", status=405)
    body = json_body(request)
    subject = str(body.get("subject", "")).strip()[:200]
    message = str(body.get("message", "")).strip()[:4000]
    if not subject or not message:
        return fail("Fill in both subject and message.")
    # ⭐ v65 security: support spam guard (per user)
    if not _rl_allow(f"support:{user.id}", 5, 3600):
        return fail("You have sent several requests already — "
                    "please wait a while.", status=429)
    email = (str(body.get("email", "")).strip()
             or user.email or f"{user.username}@cunnect.app")
    SupportRequest.objects.create(
        user=user, email=email, subject=subject, message=message
    )
    return ok({"sent": True})


@student_required
def student_profile(request, user):
    profile = getattr(user, "userprofile", None)
    return ok({
        "uid": user.username,
        "name": profile.full_name if profile else user.username,
        "phone": (profile.phone if profile else "") or "",
        "branch": (profile.branch if profile else "") or "",
        "year": (profile.year if profile else "") or "",
    })


# ---------------------------------------------------------------------
# Food
# ---------------------------------------------------------------------


@student_required
def food_home(request, user):
    now = timezone.now()
    items = FoodItem.objects.select_related("vendor").order_by("id")
    slides = HeroSlide.objects.filter(is_active=True).order_by("order")
    offers = (
        FoodOffer.objects.filter(is_active=True)
        .select_related("coupon")
        .order_by("order")
    )
    coupons = Coupon.objects.filter(
        is_active=True, valid_until__gte=now
    )
    item_dicts = []
    for item in items:
        d = serialize_food_item(item)
        # ⭐ when the kitchen is closed, items show as unavailable to students
        if not getattr(item.vendor, "kitchen_open", True):
            d["is_available"] = False
        item_dicts.append(d)
    return ok({
        "items": item_dicts,
        "hero_slides": [
            {
                "id": slide.id,
                "title": slide.title,
                "subtitle": slide.subtitle,
                "image_url": media_url(slide.image),
            }
            for slide in slides
        ],
        "offers": [
            {
                "id": offer.id,
                "title": offer.title,
                "description": offer.description,
                "coupon_code": offer.coupon.code if offer.coupon else "",
                "image_url": media_url(offer.image),
            }
            for offer in offers
        ],
        "coupons": [
            {
                "code": coupon.code,
                "discount_type": coupon.discount_type,
                "discount_value": float(coupon.discount_value),
                "minimum_order_value": float(coupon.minimum_order_value),
            }
            for coupon in coupons
        ],
    })


def _find_valid_coupon(code, order_total, user):
    now = timezone.now()
    try:
        coupon = Coupon.objects.get(code__iexact=code, is_active=True)
    except Coupon.DoesNotExist:
        return None, "Invalid coupon code."
    if coupon.valid_until and coupon.valid_until < now:
        return None, "This coupon has expired."
    if coupon.valid_from and coupon.valid_from > now:
        return None, "This coupon is not active yet."
    if float(order_total) < float(coupon.minimum_order_value):
        return None, (
            f"Minimum order ₹{float(coupon.minimum_order_value):.0f} "
            f"is required for this coupon."
        )
    if coupon.one_time_per_user and user is not None:
        if CouponUsage.objects.filter(coupon=coupon, user=user).exists():
            return None, "You have already used this coupon."
    return coupon, None


@csrf_exempt
@student_required
def food_coupon_validate(request, user):
    if request.method != "POST":
        return fail("POST required.", status=405)
    body = json_body(request)
    code = str(body.get("code", "")).strip()
    order_total = float(body.get("order_total", 0) or 0)
    if not code:
        return fail("Enter a coupon code.")
    coupon, error = _find_valid_coupon(code, order_total, user)
    if error:
        return fail(error)
    return ok({
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": float(coupon.discount_value),
        "minimum_order_value": float(coupon.minimum_order_value),
    })


def _new_order_number():
    """⭐ Sequential: CU-01, CU-02, ... auto-increment."""
    try:
        from django.db import transaction
        from myapp.models import OrderCounter

        with transaction.atomic():
            c, _ = OrderCounter.objects.select_for_update().get_or_create(
                id=1, defaults={"value": 0})
            c.value += 1
            c.save(update_fields=["value"])
            return f"CU-{c.value:02d}"
    except Exception:
        return f"CU-{uuid.uuid4().hex[:8].upper()}"

    return f"CU-{uuid.uuid4().hex[:8].upper()}"


@csrf_exempt
@student_required
def food_place_order(request, user):
    # ⭐ v65 security: order-bot guard (10 orders / 10 min per account)
    if request.method == "POST" and not _rl_allow(
            f"foodorder:{user.id}", 10, 600):
        return fail("Too many orders in a short time — please wait a "
                    "few minutes.", status=429)
    if request.method != "POST":
        return fail("POST required.", status=405)
    body = json_body(request)
    raw_items = body.get("items") or []
    if not raw_items:
        return fail("Cart is empty.")

    payment = str(body.get("payment", "cash"))
    note = str(body.get("note", ""))
    address = str(body.get("address", "")).strip() or "Chandigarh University"
    landmark = str(body.get("landmark", ""))
    customer_upi = str(body.get("customer_upi", "")).strip()
    txn_id = str(body.get("txn_id", "")).strip()[:64]
    txn_last4 = str(body.get("txn_last4", "")).strip()[:4]
    if not txn_last4 and txn_id:
        txn_last4 = txn_id[-4:]
    coupon_code = str(body.get("coupon_code", "")).strip()

    profile = getattr(user, "userprofile", None)
    customer_name = (
        (profile.full_name if profile and profile.full_name else user.get_full_name())
        or user.username
    )
    customer_phone = (profile.phone if profile else "") or ""

    # group items by vendor (same split as the Django cart).
    grouped = {}
    for raw in raw_items:
        try:
            item_id = int(raw.get("item_id"))
            quantity = int(raw.get("quantity", 1))
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        try:
            food_item = FoodItem.objects.get(id=item_id, is_available=True)
        except FoodItem.DoesNotExist:
            continue
        grouped.setdefault(food_item.vendor_id, []).append((food_item, quantity))

    if not grouped:
        return fail("No valid item in the cart.")

    cart_total = sum(
        float(food_item.price) * quantity
        for entries in grouped.values()
        for food_item, quantity in entries
    )

    coupon = None
    if coupon_code:
        coupon, error = _find_valid_coupon(coupon_code, cart_total, user)
        if error:
            return fail(error)

    discount_total = 0.0
    if coupon:
        if coupon.discount_type == "percent":
            discount_total = cart_total * float(coupon.discount_value) / 100
        else:
            discount_total = float(coupon.discount_value)
        discount_total = min(discount_total, cart_total)

    created = []
    remaining_discount = discount_total
    for vendor_id, entries in grouped.items():
        subtotal = sum(float(f.price) * q for f, q in entries)
        discount = min(remaining_discount, subtotal)
        remaining_discount -= discount
        order = Order.objects.create(
            order_number=_new_order_number(),
            vendor_id=vendor_id,
            customer=user,
            customer_name=customer_name,
            customer_phone=customer_phone,
            delivery_address=address,
            landmark=landmark,
            customer_upi=customer_upi,
            txn_last4=txn_last4,
            txn_id=txn_id,
            payment_method=payment,
            status="pending",
            subtotal=subtotal,
            discount=discount,
            total_amount=subtotal - discount,
            order_note=note,
            delivery_otp=str(random.randint(1000, 9999)),
        )
        for food_item, quantity in entries:
            OrderItem.objects.create(
                order=order,
                food_item=food_item,
                item_name=food_item.name,
                price=food_item.price,
                quantity=quantity,
            )
            # ⭐ live stock: order aate hi deduct, khatam -> auto-unavailable
            if food_item.stock > 0:
                food_item.stock = max(0, food_item.stock - quantity)
                if food_item.stock == 0:
                    food_item.is_available = False
                food_item.save()
        created.append(order)

    _notify(
        user=user,
        order=created[0],
        title="Order placed",
        message=f"{created[0].order_number} placed successfully.",
    )
    _seen_vendors = set()
    for _o in created:
        _vp = _o.vendor
        if _vp is None or _vp.id in _seen_vendors:
            continue
        _seen_vendors.add(_vp.id)
        _notify_vendor(
            _vp, "New order received",
            f"{_o.order_number} - accept or reject now", order=_o,
            push_data={"event": "new_order", "portal": "vendor",
                       "order_id": _o.id,
                       "order_number": _o.order_number})
        try:
            from api_app.tasks import order_alert_task, redis_ok
            if redis_ok():
                order_alert_task.apply_async(args=[_o.id, 1], countdown=45)
            else:
                raise RuntimeError("no redis")
        except Exception:
            threading.Thread(
                target=_order_alert_loop, args=(_o.id,), daemon=True).start()
    if coupon:
        CouponUsage.objects.create(coupon=coupon, user=user)
        vendor_name = created[0].vendor.business_name if created[0].vendor else ""
        _notify(
            user=user,
            order=created[0],
            title="Coupon applied",
            message=f"You saved ₹{discount_total:.0f} with {coupon.code} ({vendor_name}).",
        )

    return ok({
        "order_numbers": [order.order_number for order in created],
        "orders": [serialize_order(order, include_otp=True) for order in created],
    })


@student_required
def food_my_orders(request, user):
    orders = (
        Order.objects.filter(customer=user)
        .select_related("vendor", "customer", "customer__userprofile")
        .prefetch_related("items")
        .order_by("-created_at")[:40]
    )
    return ok({
        "orders": [
            serialize_order(order, include_otp=True) for order in orders
        ]
    })


@student_required
def food_orders_status(request, user):
    orders = (
        Order.objects.filter(customer=user)
        .select_related("vendor", "customer", "customer__userprofile")
        .order_by("-created_at")[:40]
    )
    return ok({
        "orders": [
            serialize_order(order, include_items=False, include_otp=True)
            for order in orders
        ]
    })


# ---------- ⭐ FCM PUSH (screen-off / lock-screen notifications) ----------
_fcm_app = None


_FCM_LOCK = threading.Lock()


def _fcm_init():
    global _fcm_app
    if _fcm_app is not None:
        return _fcm_app
    with _FCM_LOCK:
        if _fcm_app is not None:
            return _fcm_app
        return _fcm_init_locked()


def _fcm_init_locked():
    global _fcm_app
    try:
        import firebase_admin
        from firebase_admin import credentials

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "firebase-service-account.json",
        )
        b64 = os.environ.get("CUNNECT_FIREBASE_B64", "").strip()
        if not b64:
            try:
                b64 = open(os.path.join(
                    os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__))),
                    "deploy", "fcm_key.b64"), encoding="utf8").read().strip()
            except Exception:
                b64 = ""
        if b64:
            import base64 as _b64

            _fcm_app = firebase_admin.initialize_app(
                credentials.Certificate(
                    json.loads(_b64.b64decode(b64).decode())))
            return _fcm_app
        if os.path.exists(path):
            _fcm_app = firebase_admin.initialize_app(
                credentials.Certificate(path))
            return _fcm_app
        raw = os.environ.get("CUNNECT_FIREBASE_JSON", "").strip()
        if raw:
            _fcm_app = firebase_admin.initialize_app(
                credentials.Certificate(json.loads(raw)))
            return _fcm_app
        print("[FCM] firebase-service-account.json missing -> push OFF")
        return None
    except Exception as exc:
        # ⭐ if another thread already initialised it, reuse that app
        try:
            import firebase_admin

            _fcm_app = firebase_admin.get_app()
            return _fcm_app
        except Exception:
            pass
        _FCM_DEBUG["init_error"] = str(exc)
        print("[FCM] init failed:", exc)
        return None


def _notify(*args, **kwargs):
    """Notification row + instant push. (student audience default)"""
    kwargs.setdefault("audience", "student")
    # ⭐ v53: deep-link route for the push (default: student order flow)
    route = kwargs.pop("route", "orders")
    # ⭐ v74: extra push payload — lets the app raise the right POPUP
    # (ride accepted / rejected / paid / arrived / started / completed).
    extra = kwargs.pop("push_data", None)
    # ⭐ v75: which portal this push belongs to (student / rider / vendor).
    # The app uses it so a RIDER push never pops up inside the student
    # Ride screen and vice-versa.
    portal = kwargs.pop("portal", "student")
    n = Notification.objects.create(*args, **kwargs)
    try:
        data = dict(extra or {})
        data.setdefault("portal", portal)
        _push_user(n.user_id, n.title, n.message, route=route, data=data)
    except Exception:
        pass
    return n


def _notify_vendor(vendor_profile, title, message, order=None, route=None,
                   push_data=None, portal="vendor"):
    """⭐ Separate notification row for the vendor (audience=vendor) + push.

    ⭐ v75: `portal` = "rider" for ride partners, "vendor" for food/print.
    """
    n = None
    try:
        n = Notification.objects.create(
            user_id=vendor_profile.user_id,
            order=order,
            title=title,
            message=message,
            audience="vendor",
        )
    except Exception:
        pass
    try:
        data = dict(push_data or {})
        data.setdefault("portal", portal)
        _vendor_push(vendor_profile, title, message, route=route, data=data)
    except Exception:
        pass
    return n


_ORDER_ALERTS = set()


_FCM_DEBUG = {"last_push": "", "init_error": ""}


@csrf_exempt
def debug_fcm(request):
    """⭐ Notification debugging: FCM ready? tokens? last push?"""
    from myapp.models import DeviceToken

    app = _fcm_init()
    total = DeviceToken.objects.count()
    out = {
        "fcm_ready": app is not None,
        "env_set": bool(os.environ.get("CUNNECT_FIREBASE_JSON", "").strip()
                       or os.environ.get("CUNNECT_FIREBASE_B64", "").strip()),
        "file_exists": os.path.exists(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "firebase-service-account.json")),
        "tokens_total": total,
        "last_push": _FCM_DEBUG.get("last_push", ""),
        "init_error": _FCM_DEBUG.get("init_error", ""),
    }
    if request.GET.get("test") == "1":
        tokens = list(DeviceToken.objects.order_by("-id")
                      .values_list("token", flat=True))[:5]
        if not tokens:
            out["test"] = "NO TOKENS registered"
        else:
            _push_tokens(tokens, "CUnnect Test 🔔",
                         "Notification system working!")
            out["test"] = _FCM_DEBUG.get("last_push", "") or "push ran"
    return ok(out)


def _push_tokens(tokens, title, message, high=False, _direct=False,
                 route=None, data=None):
    # ⭐ Celery: push in the background, return the request instantly
    if not _direct:
        try:
            from api_app.tasks import push_tokens_task, redis_ok
            if redis_ok():
                push_tokens_task.delay(list(tokens or []), title, message,
                                       high, route, data)
                return
        except Exception:
            pass
    try:
        from firebase_admin import messaging

        app = _fcm_init()
        if app is None or not tokens:
            _FCM_DEBUG["last_push"] = (
                f"SKIP app={'yes' if app else 'NO'} tokens={len(tokens)}")
            return
        print(f"[FCM-PUSH] -> {len(tokens)} tokens | {title}")
        # ⭐ push to ALL tokens (in batches of 500) — previously only 5 were sent!
        okc = 0
        total = 0
        errtxt = ""
        for _i in range(0, len(tokens), 500):
            batch = tokens[_i:_i + 500]
            resp = messaging.send_each_for_multicast(
                messaging.MulticastMessage(
                    notification=messaging.Notification(
                        title=title, body=message),
                    # ⭐ v53: route -> tapping the notification opens the
                    # matching section of the app directly.
                    # ⭐ v74: "event" + "ride_code" let the app raise the
                    # matching POPUP the moment the push lands.
                    data=dict(
                        {"kind": "vendor" if high else "user",
                         "route": str(route or "")},
                        **{str(k): str(v) for k, v in (data or {}).items()
                           if v is not None}),
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(
                            channel_id=("cunnect_alert_v5" if high
                                        else "cunnect_ping_v5"),
                            sound="cunnect_alert" if high else "cunnect_ping",
                            icon="cu_notif",
                            priority="high",
                            visibility="public",
                            default_sound=False,
                        ),
                    ),
                    tokens=batch,
                ),
                app=app,
            )
            total += len(resp.responses)
            okc += sum(1 for r in resp.responses if r.success)
            for tok, r in zip(batch, resp.responses):
                if not r.success:
                    errtxt = f" | ERR {r.exception}"
                    print("[FCM-PUSH] err:", r.exception)
                    err = f"{getattr(r.exception, 'code', '')} {r.exception}"
                    if "not-registered" in err or "NotRegistered" in err \
                            or "SenderIdMismatch" in err or "sender-id-mismatch" in err \
                            or "InvalidArgument" in err or "invalid-argument" in err:
                        from myapp.models import DeviceToken as _DT
                        _DT.objects.filter(token=tok).delete()
                        print("[FCM-PUSH] stale token pruned")
        _FCM_DEBUG["last_push"] = f"{title} -> ok={okc}/{total}{errtxt}"
    except Exception as exc:
        _FCM_DEBUG["last_push"] = f"EXC {exc}"
        print("[FCM] push failed:", exc)


@csrf_exempt
def vendor_upi(request):
    """⭐ Vendor sets/updates their UPI ID (the QR is generated from it)."""
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor login required.", status=401)
    if request.method == "POST":
        profile.upi_id = str(json_body(request).get("upi_id", "")).strip()[:120]
        profile.save()
    return ok({
        "upi_id": profile.upi_id,
        "qr_url": profile.upi_qr_image.url if profile.upi_qr_image else "",
    })


@csrf_exempt
def vendor_profile_update(request):
    """Vendor updates their business name and mobile number."""
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor login required.", status=401)
    if request.method != "POST":
        return fail("POST only.", status=405)
    body = json_body(request)
    name = str(body.get("business_name", "")).strip()[:150]
    phone = str(body.get("phone", "")).strip()[:15]
    if not name:
        return fail("Business name cannot be empty.")
    if phone and VendorProfile.objects.filter(phone=phone).exclude(
            id=profile.id).exists():
        return fail("This mobile number is already used by another vendor.")
    profile.business_name = name
    if phone:
        profile.phone = phone
    profile.save(update_fields=["business_name", "phone"])
    return ok({
        "business_name": profile.business_name,
        "phone": profile.phone or "",
    })


@csrf_exempt
def vendor_logo_upload(request):
    """⭐ Vendor uploads their shop icon/logo (shown on food cards)."""
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor login required.", status=401)
    if request.method != "POST":
        return fail("POST only.", status=405)
    if str(request.POST.get("remove", "")).strip() == "1" or \
            str(json_body(request).get("remove", "")).strip() == "1":
        if profile.logo:
            profile.logo.delete(save=False)
        profile.logo = None
        profile.save()
        return ok({"logo_url": ""})
    f = request.FILES.get("file")
    if f is None:
        return fail("No image file sent.")
    upload_error = check_upload(f, kind="image", max_mb=8)
    if upload_error:
        return fail(upload_error)
    profile.logo = f
    profile.save()
    return ok({"logo_url": media_url(profile.logo)})


@csrf_exempt
def vendor_upi_qr_upload(request):
    """⭐ Vendor uploads their own QR image (backend QR option)."""
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor login required.", status=401)
    if request.method != "POST":
        return fail("POST only.", status=405)
    remove_flag = str(request.POST.get("remove", "")).strip()
    if not remove_flag:
        remove_flag = str(json_body(request).get("remove", "")).strip()
    if remove_flag == "1":
        if profile.upi_qr_image:
            profile.upi_qr_image.delete(save=False)
        profile.upi_qr_image = None
        profile.save()
        return ok({"qr_url": ""})
    f = request.FILES.get("file")
    if f is None:
        return fail("No image file sent.")
    upload_error = check_upload(f, kind="image", max_mb=8)
    if upload_error:
        return fail(upload_error)
    profile.upi_qr_image = f
    profile.save()
    return ok({"qr_url": profile.upi_qr_image.url})


@csrf_exempt
def upi_qr(request):
    """⭐ Vendor UPI payment QR (uploaded image or generated base64 PNG)."""
    vendor_id = str(request.GET.get("vendor_id", "")).strip()
    amount = str(request.GET.get("amount", "")).strip()
    from myapp.models import VendorProfile

    vp = VendorProfile.objects.filter(id=vendor_id).first()
    if vp is None:
        return fail("Vendor UPI not set.")
    # ⭐ v56: when the checkout sends an amount AND the vendor's UPI ID is
    # known, a QR is GENERATED with the amount embedded (upi://...&am=X)
    # so any UPI app auto-fills the exact total on scan. The uploaded
    # QR image (static — cannot carry a dynamic amount) is used only
    # when the UPI ID is missing.
    if vp.upi_qr_image and not (amount and vp.upi_id):
        return ok({
            "qr_url": vp.upi_qr_image.url,
            "upi_id": vp.upi_id or "",
            "name": vp.business_name,
        })
    if not vp.upi_id:
        return fail("Vendor UPI not set.")
    import base64
    import io

    import qrcode

    # ⭐ v57: build a SPEC-COMPLIANT upi:// URI. The previous string
    # embedded the business name raw — a space or & in it produced an
    # invalid URI and UPI apps rejected the QR ("This QR code is
    # invalid"). All params are now URL-encoded and the amount is
    # validated + normalised to 2 decimals.
    from urllib.parse import quote

    params = [("pa", vp.upi_id.strip()), ("pn", vp.business_name.strip())]
    if amount:
        try:
            amt = float(amount)
            if amt > 0:
                params.append(("am", f"{amt:.2f}"))
        except (TypeError, ValueError):
            pass
    params.append(("cu", "INR"))
    # NOTE: '@' stays raw in the VPA (pa) — the universal convention in
    # merchant QRs; some UPI apps mis-handle %40 there.
    upi = "upi://pay?" + "&".join(
        f"{k}={quote(str(v), safe='@' if k == 'pa' else '')}"
        for k, v in params)
    img = qrcode.make(upi)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return ok({
        "qr_b64": base64.b64encode(buf.getvalue()).decode(),
        "upi_id": vp.upi_id,
        "name": vp.business_name,
    })


_GH_RELEASE_CACHE = {"at": 0.0, "version": 0, "url": "", "err": ""}


def _gh_latest_release():
    """⭐ GitHub repo ki latest release (tag + APK asset) — 10 min cache."""
    import urllib.request

    now = time.time()
    ttl = 600 if _GH_RELEASE_CACHE["version"] else 60
    if now - _GH_RELEASE_CACHE["at"] < ttl:
        return _GH_RELEASE_CACHE
    _GH_RELEASE_CACHE["at"] = now
    _GH_RELEASE_CACHE["err"] = ""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/02Adarsh/cunnect-backend/"
            "releases/latest",
            headers={"User-Agent": "cunnect-app",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read().decode())
        tag = re.sub(r"[^0-9]", "", str(j.get("tag_name") or ""))
        ver = int(tag) if tag else 0
        url = ""
        for a in (j.get("assets") or []):
            name = str(a.get("name") or "").lower()
            if name.endswith(".apk"):
                url = str(a.get("browser_download_url") or "")
                break
        if ver and url:
            _GH_RELEASE_CACHE["version"] = ver
            _GH_RELEASE_CACHE["url"] = url
    except Exception as exc:
        # ⭐ on API rate-limit (403), parse the releases HTML page instead
        try:
            req2 = urllib.request.Request(
                "https://github.com/02Adarsh/cunnect-backend/"
                "releases/latest",
                headers={"User-Agent": "cunnect-app"})
            with urllib.request.urlopen(req2, timeout=8) as r2:
                html = r2.read().decode()
            mt = re.search(r"releases/tag/v(\d+)", html)
            m = None
            if mt:
                req3 = urllib.request.Request(
                    "https://github.com/02Adarsh/cunnect-backend/"
                    f"releases/expanded_assets/v{mt.group(1)}",
                    headers={"User-Agent": "cunnect-app"})
                with urllib.request.urlopen(req3, timeout=8) as r3:
                    m = re.search(
                        r"releases/download/(v\d+)/([A-Za-z0-9._-]+\.apk)",
                        r3.read().decode())
            if mt and m:
                _GH_RELEASE_CACHE["version"] = int(mt.group(1))
                _GH_RELEASE_CACHE["url"] = (
                    "https://github.com/02Adarsh/cunnect-backend/"
                    f"releases/download/{m.group(1)}/{m.group(2)}")
                _GH_RELEASE_CACHE["err"] = ""
            else:
                _GH_RELEASE_CACHE["err"] = str(exc)
        except Exception as exc2:
            _GH_RELEASE_CACHE["err"] = f"{exc} | {exc2}"
        print(f"[APP-VERSION] github check failed: {exc}")
    return _GH_RELEASE_CACHE


@csrf_exempt
def debug_gh(request):
    """⭐ GitHub release detection debug (?fresh=1 forces an instant recheck)."""
    if request.GET.get("fresh") == "1":
        _GH_RELEASE_CACHE["at"] = 0.0
    return ok(_gh_latest_release())


@csrf_exempt
def app_version(request):
    """⭐ In-app update check: app_version.json + GitHub latest release."""
    from django.conf import settings as _st

    data = {"version": 1, "url": "", "notes": ""}
    override_url = ""
    try:
        p = Path(_st.BASE_DIR) / "deploy" / "app_version.json"
        raw = json.loads(p.read_text(encoding="utf8"))
        data.update(raw)
        override_url = (raw.get("apk_url") or "").strip()
    except Exception:
        pass
    if override_url:
        # ⭐ custom hosting — GitHub bilkul ignore
        data["url"] = override_url
        return ok(data)
    gh = _gh_latest_release()
    if gh["version"] > int(data.get("version") or 0) and gh["url"]:
        data["version"] = gh["version"]
        data["url"] = gh["url"]
    return ok(data)


@csrf_exempt
def health(request):
    """⭐ Render keep-awake ping endpoint (cron-job.org se)."""
    return ok({"status": "ok"})


def _push_user(user_id, title, message, route=None, data=None):
    try:
        from myapp.models import DeviceToken

        tokens = list(
            DeviceToken.objects.filter(user_id=user_id)
            .order_by("-id").values_list("token", flat=True)
        )
        _push_tokens(tokens, title, message, high=False, route=route,
                     data=data)
    except Exception:
        pass


def _vendor_push(vendor_profile, title, message, route=None, data=None):
    from myapp.models import DeviceToken

    tokens = list(
        DeviceToken.objects.filter(user_id=vendor_profile.user_id)
        .order_by("-id").values_list("token", flat=True)
    )
    # ⭐ tapping a vendor alert opens the vendor portal directly
    # ⭐ v74: ride events ride along with route="ride" + the event name
    _push_tokens(tokens, title, message, high=True,
                 route=route or "vendor", data=data)


def _order_alert_loop(order_id):
    """Repeat ring on the vendor's phone until accept/reject/silence."""
    import time as _t

    for _ in range(12):
        _t.sleep(45)
        try:
            from food.models import Order

            if order_id in _ORDER_ALERTS:
                return
            o = Order.objects.filter(id=order_id).first()
            if o is None or o.status != "pending" or o.vendor is None:
                return
            _vendor_push(
                o.vendor,
                "Order pending - alert",
                f"{o.order_number} still waiting. Accept or reject now.",
            )
        except Exception as exc:
            print("[ALERT]", exc)



@csrf_exempt
def vendor_device_token(request):
    """vendor ka FCM token save."""
    if request.method != "POST":
        return fail("POST required.", status=405)
    user, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor login required.", status=401)
    from myapp.models import DeviceToken

    token = str(json_body(request).get("token", "")).strip()
    if len(token) < 20:
        return fail("Token required.")
    DeviceToken.objects.update_or_create(user=user, token=token)
    return ok({"saved": True})


@csrf_exempt
def vendor_silence(request):
    """repeat ring manual band."""
    if request.method != "POST":
        return fail("POST required.", status=405)
    user, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor login required.", status=401)
    oid = json_body(request).get("id")
    try:
        _ORDER_ALERTS.add(int(oid))
    except (TypeError, ValueError):
        return fail("Bad id.")
    return ok({"silenced": True})


@csrf_exempt
@student_required
def device_token(request, user):
    """⭐ Save the app's FCM token (for push)."""
    if request.method != "POST":
        return fail("POST required.", status=405)
    from myapp.models import DeviceToken

    token = str(json_body(request).get("token", "")).strip()
    if len(token) < 20:
        return fail("Token required.")
    DeviceToken.objects.update_or_create(user=user, token=token)
    return ok({"saved": True})


@student_required
def food_notifications(request, user):
    # ⭐ audience filter — student rows for the student app, vendor rows for the vendor app
    audience = str(request.GET.get("audience", "student")).strip().lower()
    if audience not in ("student", "vendor"):
        audience = "student"
    # ⭐ v75: the food screen shows food rows only — print and ride
    # notifications have their own screens/history.
    base_qs = Notification.objects.filter(
        user=user, audience=audience).exclude(category="print")
    notifications = base_qs.order_by("-created_at")[:30]
    return ok({
        "notifications": [
            {
                "id": notification.id,
                "title": notification.title,
                "message": notification.message,
                "is_read": notification.is_read,
                "created_at_iso": iso(notification.created_at),
            }
            for notification in notifications
        ],
        "unread_count": base_qs.filter(is_read=False).count(),
    })


@csrf_exempt
@student_required
def food_notifications_read(request, user):
    if request.method != "POST":
        return fail("POST required.", status=405)
    body = json_body(request)
    audience = str(
        body.get("audience") or request.GET.get("audience") or "student"
    ).strip().lower()
    if audience not in ("student", "vendor"):
        audience = "student"
    Notification.objects.filter(user=user, audience=audience).update(is_read=True)
    return ok({"read": True})


# ---------------------------------------------------------------------
# Vendor portal
# ---------------------------------------------------------------------


@csrf_exempt
@require_http_methods(["POST"])
@throttle("vlogin", 120, 600, body_field="phone", target_limit=10)
def vendor_login(request):
    body = json_body(request)
    phone = str(body.get("phone", "")).strip()
    password = str(body.get("password", ""))
    if not phone or not password:
        return fail("Both phone and password are required.")
    profile = VendorProfile.objects.filter(phone=phone).select_related("user").first()
    if profile is None:
        return fail("No vendor found for this phone number.", status=401)
    user = authenticate(request, username=profile.user.username, password=password)
    if user is None:
        return fail("Invalid phone or password.", status=401)
    token, _ = Token.objects.get_or_create(user=user)
    return ok({
        "token": token.key,
        "vendor_id": profile.id,
        "business_name": profile.business_name,
        "vendor_type": profile.vendor_type,
        "phone": profile.phone,
        "owner_username": user.username,
        "email": user.email or "",
    })


def _reveal_phone(status):
    """⭐ Customer mobile is shown only after the order is accepted."""
    return status in (
        "accepted", "preparing", "ready", "out_for_delivery", "delivered")


def _vendor_orders(vendor_profile):
    # ⭐ v62: customer + profile joined in ONE query — serialize_order reads
    # order.customer.profile, so without this every order row cost 2 extra
    # DB round-trips (the classic N+1 that made the dashboard feel slow).
    return Order.objects.filter(vendor_id=vendor_profile.id).select_related(
        "vendor", "customer", "customer__userprofile"
    ).prefetch_related("items")


@student_required
def vendor_dashboard(request, user):
    vendor_user_, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    orders = _vendor_orders(profile)
    today = timezone.now().date()
    today_sales = sum(
        float(order.total_amount)
        for order in orders.filter(created_at__date=today).exclude(
            status__in=["rejected", "cancelled"]
        )
    )
    incoming = orders.filter(status="pending").order_by("-created_at")[:8]
    active = orders.filter(
        status__in=["accepted", "preparing", "ready"]
    ).order_by("-created_at")[:10]
    history = orders.exclude(
        status__in=["pending", "accepted", "preparing", "ready"]
    ).order_by("-created_at")[:25]
    out_for_delivery = orders.filter(
        status="out_for_delivery", otp_verified=False
    ).order_by("-created_at")[:10]
    menu = FoodItem.objects.filter(vendor_id=profile.id)
    return ok({
        "incoming_count": incoming.count(),
        "active_count": active.count(),
        "today_sales": today_sales,
        "menu_count": menu.count(),
        "available_count": menu.filter(is_available=True).count(),
        "kitchen_open": getattr(profile, "kitchen_open", True),
        "incoming_orders": [
            serialize_order(
                order,
                reveal_mobile=_reveal_phone(order.status))
            for order in incoming],
        "active_orders": [
            serialize_order(
                order, reveal_mobile=_reveal_phone(order.status))
            for order in active],
        "history_orders": [
            serialize_order(
                order, include_items=False,
                reveal_mobile=_reveal_phone(order.status))
            for order in history],
        "out_for_delivery_orders": [
            serialize_order(
                order, reveal_mobile=_reveal_phone(order.status))
            for order in out_for_delivery
        ],
    })


VENDOR_ACTIONS = {
    "accept": ("pending", "accepted"),
    "reject": ("pending", "rejected"),
    "prepare": ("accepted", "preparing"),
    "ready": ("preparing", "ready"),
}


@csrf_exempt
@student_required
def vendor_order_action(request, user, order_id, action):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    if action not in VENDOR_ACTIONS:
        return fail("Unknown action.", status=404)
    expected_from, new_status = VENDOR_ACTIONS[action]
    try:
        order = Order.objects.get(id=order_id, vendor_id=profile.id)
    except Order.DoesNotExist:
        return fail("Order not found.", status=404)
    # ⭐ v55: idempotent — a double-tap that repeats the same action is a
    # silent success (no duplicate notification, no error toast).
    if order.status == new_status:
        return ok({"order": serialize_order(
            order, reveal_mobile=_reveal_phone(order.status))})
    if order.status != expected_from:
        return fail(f"Order is currently '{order.status}' — this action is not allowed.")
    order.status = new_status
    order.save(update_fields=["status", "updated_at"])
    if order.customer_id:
        _notify(
            user_id=order.customer_id,
            order=order,
            title=f"Order {new_status}",
            message=f"{order.order_number} is now {new_status}.",
        )
    # ⭐ confirmation of the vendor's own action (sound + heads-up)
    if action != "reject":
        _notify_vendor(profile, f"Order {new_status}",
                       f"{order.order_number} marked {new_status}.", order=order)
    return ok({"order": serialize_order(
        order, reveal_mobile=_reveal_phone(order.status))})


@csrf_exempt
@student_required
def vendor_start_delivery(request, user, order_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    try:
        order = Order.objects.get(id=order_id, vendor_id=profile.id)
    except Order.DoesNotExist:
        return fail("Order not found.", status=404)
    if order.status not in ("ready", "out_for_delivery"):
        return fail("Mark the order 'ready' first.")
    if not order.delivery_otp:
        order.delivery_otp = str(random.randint(1000, 9999))
    order.status = "out_for_delivery"
    order.save(update_fields=["status", "delivery_otp", "updated_at"])
    if order.customer_id:
        _notify(
            user_id=order.customer_id,
            order=order,
            title="Out for delivery",
            message=f"{order.order_number} is out for delivery. OTP: {order.delivery_otp}",
        )
    _notify_vendor(profile, "Delivery started",
                   f"{order.order_number} out for delivery. OTP shared with customer.",
                   order=order)
    return ok({"order": serialize_order(order, include_otp=True)})


@csrf_exempt
@student_required
def vendor_verify_otp(request, user, order_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    try:
        order = Order.objects.get(id=order_id, vendor_id=profile.id)
    except Order.DoesNotExist:
        return fail("Order not found.", status=404)
    otp = str(json_body(request).get("otp", "")).strip()
    if order.status != "out_for_delivery":
        return fail("Order is not out for delivery.")
    if otp != order.delivery_otp:
        return fail("OTP is wrong")
    order.status = "completed"
    order.otp_verified = True
    order.delivered_at = timezone.now()
    order.save(update_fields=["status", "otp_verified", "delivered_at", "updated_at"])
    if order.customer_id:
        _notify(
            user_id=order.customer_id,
            order=order,
            title="Delivered",
            message=f"{order.order_number} has been delivered. Enjoy!",
        )
    return ok({"order": serialize_order(
        order, reveal_mobile=_reveal_phone(order.status))})


@student_required
def vendor_menu(request, user):
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    items = FoodItem.objects.filter(vendor_id=profile.id).select_related(
        "vendor"
    ).order_by("id")
    return ok({"items": [serialize_food_item(item) for item in items]})


@csrf_exempt
def vendor_menu_photo(request, item_id):
    """⭐ Vendor uploads a photo for a food item."""
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    if request.method != "POST":
        return fail("POST required.", status=405)
    try:
        item = FoodItem.objects.get(id=item_id, vendor_id=profile.id)
    except FoodItem.DoesNotExist:
        return fail("Item not found.", status=404)
    f = request.FILES.get("file")
    if f is None:
        return fail("No image file sent.")
    upload_error = check_upload(f, kind="image", max_mb=8)
    if upload_error:
        return fail(upload_error)
    item.image = f
    item.save(update_fields=["image"])
    return ok({"image_url": media_url(item.image)})


@csrf_exempt
@student_required
def vendor_menu_toggle(request, user, item_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    try:
        item = FoodItem.objects.get(id=item_id, vendor_id=profile.id)
    except FoodItem.DoesNotExist:
        return fail("Item not found.", status=404)
    item.is_available = not item.is_available
    item.save(update_fields=["is_available"])
    return ok({"item": serialize_food_item(item)})


def _item_payload(body):
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()
    category = str(body.get("category", "")).strip() or "other"
    try:
        price = float(body.get("price", 0) or 0)
    except (TypeError, ValueError):
        price = 0
    is_available = bool(body.get("is_available", True))
    is_veg = bool(body.get("is_veg", True))
    try:
        stock = int(body.get("stock", 0) or 0)
    except (TypeError, ValueError):
        stock = 0
    if stock < 0:
        stock = 0
    return name, description, category, price, is_available, stock, is_veg


@csrf_exempt
@student_required
def vendor_menu_add(request, user):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    name, description, category, price, is_available, stock, is_veg = \
        _item_payload(json_body(request))
    if not name or price <= 0:
        return fail("Item name and a valid price are required.")
    item = FoodItem.objects.create(
        name=name,
        description=description[:250],
        category=category[:50],
        price=price,
        vendor_id=profile.id,
        is_available=is_available,
        is_veg=is_veg,
        stock=stock,
    )
    return ok({"item": serialize_food_item(item)})


@csrf_exempt
@student_required
def vendor_menu_edit(request, user, item_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    try:
        item = FoodItem.objects.get(id=item_id, vendor_id=profile.id)
    except FoodItem.DoesNotExist:
        return fail("Item not found.", status=404)
    name, description, category, price, is_available, stock, is_veg = \
        _item_payload(json_body(request))
    if not name or price <= 0:
        return fail("Item name and a valid price are required.")
    item.name = name
    item.description = description[:250]
    item.category = category[:50]
    item.price = price
    item.is_available = is_available
    item.is_veg = is_veg
    item.stock = stock
    if stock > 0:
        item.is_available = True  # ⭐ restock = available again
    item.save()
    return ok({"item": serialize_food_item(item)})


@csrf_exempt
@student_required
def vendor_menu_delete(request, user, item_id):
    """⭐ Vendor deletes their menu item."""
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    # ⭐ v57: FoodItem lives in food.models — the old import from
    # myapp.models raised ImportError, so EVERY delete failed with a 500.
    item = FoodItem.objects.filter(id=item_id, vendor=profile).first()
    if item is None:
        return fail("Item not found.")
    name = item.name
    item.delete()
    return ok({"deleted": name})


@csrf_exempt
@student_required
def vendor_kitchen(request, user, state):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    if state not in ("on", "off"):
        return fail("Unknown state.", status=404)
    try:
        profile.kitchen_open = (state == "on")
        profile.save(update_fields=["kitchen_open"])
    except Exception:
        VENDOR_SESSION["kitchen"][profile.id] = state == "on"
    return ok({"kitchen_open": state == "on"})


@student_required
def vendor_earnings(request, user):
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    # Print vendors earn through PrintOrder rows, food vendors through
    # Order rows. Both are normalised into the same response shape so the
    # app can reuse one earnings screen.
    if profile.vendor_type == "printout":
        completed = (
            PrintOrder.objects.filter(vendor_id=profile.id, status="completed")
            .select_related("student", "student__userprofile")
            .order_by("-created_at")[:400]
        )
        now = timezone.now()
        week_start = (now - timedelta(days=now.weekday())).date()
        weekly = []
        week_total = 0.0
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            total = sum(
                float(order.final_amount)
                for order in completed
                if order.created_at.date() == day
            )
            weekly.append({"label": day.strftime("%a"), "total": total})
            week_total += total

        def _print_row(order):
            student = order.student
            sprofile = getattr(student, "userprofile", None) if student else None
            name = ""
            if sprofile is not None:
                name = sprofile.full_name or ""
            if not name and student is not None:
                name = student.get_full_name() or student.username
            fname = order.document.name.split("/")[-1] if order.document else ""
            return {
                "id": order.id,
                "order_number": f"PRN-{order.id}",
                "vendor_id": order.vendor_id,
                "customer_name": name,
                "note": fname,
                "total": float(order.final_amount),
                "status": "completed",
                "created_at_iso": iso(order.created_at),
            }

        return ok({
            "week_total": week_total,
            "weekly": weekly,
            "completed_orders": [_print_row(order) for order in completed],
        })
    completed = (
        Order.objects.filter(vendor_id=profile.id, status="completed")
        .select_related("vendor", "customer", "customer__userprofile")
        .order_by("-created_at")[:400]
    )
    now = timezone.now()
    week_start = (now - timedelta(days=now.weekday())).date()
    weekly = []
    week_total = 0.0
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        total = sum(
            float(order.total_amount)
            for order in completed
            if order.created_at.date() == day
        )
        weekly.append({"label": day.strftime("%a"), "total": total})
        week_total += total
    return ok({
        "week_total": week_total,
        "weekly": weekly,
        "completed_orders": [
            serialize_order(order, include_items=False) for order in completed
        ],
    })


# ---------------------------------------------------------------------
# Delivery portal
# ---------------------------------------------------------------------


@csrf_exempt
@require_http_methods(["POST"])
def delivery_login(request):
    body = json_body(request)
    phone = str(body.get("phone", "")).strip()
    password = str(body.get("password", ""))
    if not phone or not password:
        return fail("Both phone and password are required.")
    profile = DeliveryProfile.objects.filter(phone=phone).select_related(
        "user"
    ).first()
    if profile is None:
        return fail("No delivery partner found for this phone number.", status=401)
    user = authenticate(request, username=profile.user.username, password=password)
    if user is None:
        return fail("Invalid phone or password.", status=401)
    token, _ = Token.objects.get_or_create(user=user)
    return ok({
        "token": token.key,
        "partner_id": str(profile.id),
        "phone": profile.phone,
    })


@student_required
def delivery_dashboard(request, user):
    _, profile = delivery_user(request)
    if profile is None:
        return fail("Delivery account not found.", status=401)
    ready = (
        Order.objects.filter(status="ready")
        .select_related("vendor", "customer", "customer__userprofile")
        .prefetch_related("items")
        .order_by("created_at")[:15]
    )
    active = (
        Order.objects.filter(status="out_for_delivery", otp_verified=False)
        .select_related("vendor", "customer", "customer__userprofile")
        .prefetch_related("items")
        .order_by("-created_at")[:15]
    )
    history = (
        Order.objects.filter(status="completed")
        .select_related("vendor", "customer", "customer__userprofile")
        .order_by("-created_at")[:25]
    )
    return ok({
        "ready_orders": [serialize_order(order) for order in ready],
        "active_orders": [serialize_order(order) for order in active],
        "history_orders": [
            serialize_order(order, include_items=False) for order in history
        ],
    })


@csrf_exempt
@student_required
def delivery_claim(request, user, order_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = delivery_user(request)
    if profile is None:
        return fail("Delivery account not found.", status=401)
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return fail("Order not found.", status=404)
    if order.status != "ready":
        return fail("This order cannot be claimed.")
    order.status = "out_for_delivery"
    order.delivery_partner_id = profile.id
    if not order.delivery_otp:
        order.delivery_otp = str(random.randint(1000, 9999))
    order.save(update_fields=[
        "status", "delivery_partner", "delivery_otp", "updated_at"
    ])
    if order.customer_id:
        _notify(
            user_id=order.customer_id,
            order=order,
            title="Out for delivery",
            message=f"{order.order_number} is out for delivery. OTP: {order.delivery_otp}",
        )
    return ok({"order": serialize_order(
        order, reveal_mobile=_reveal_phone(order.status))})


@csrf_exempt
@student_required
def delivery_verify_otp(request, user, order_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = delivery_user(request)
    if profile is None:
        return fail("Delivery account not found.", status=401)
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return fail("Order not found.", status=404)
    otp = str(json_body(request).get("otp", "")).strip()
    if order.status != "out_for_delivery":
        return fail("Order is not out for delivery.")
    if otp != order.delivery_otp:
        return fail("OTP is wrong")
    order.status = "completed"
    order.otp_verified = True
    order.delivered_at = timezone.now()
    order.save(update_fields=["status", "otp_verified", "delivered_at", "updated_at"])
    if order.customer_id:
        _notify(
            user_id=order.customer_id,
            order=order,
            title="Delivered",
            message=f"{order.order_number} has been delivered. Enjoy!",
        )
    return ok({"order": serialize_order(
        order, reveal_mobile=_reveal_phone(order.status))})


# ---------------------------------------------------------------------
# Printout
# ---------------------------------------------------------------------


def serialize_print_vendor(profile):
    return {
        "id": profile.id,
        "business_name": profile.business_name,
        "phone": profile.phone,
        "bw_price_per_page": float(profile.bw_price_per_page),
        "color_price_per_page": float(profile.color_price_per_page),
        # ⭐ UPI payment (QR + copyable ID) — for the printout checkout
        "upi_id": profile.upi_id,
        # ⭐ v55: an uploaded QR counts as payment-enabled even when the
        # UPI ID field is empty.
        "has_qr": bool(profile.upi_qr_image),
    }


@csrf_exempt
@require_http_methods(["POST"])
@student_required
def print_page_count(request, user):
    """⭐ Page count of the uploaded PDF (like the original pdf.js,
    server-side) — the dropdowns are filled from this."""
    doc = request.FILES.get("document")
    if doc is None:
        return fail("Please attach a document.")
    name = str(getattr(doc, "name", "") or "").lower()
    if name.endswith(".pdf"):
        try:
            pages = int(_count_pdf_pages(doc) or 1)
        except Exception as exc:
            print(f"[API-PRINT] page count failed: {exc}")
            return fail(
                "PDF pages could not be read. Please upload a valid PDF.")
    else:
        pages = 1
    return ok({"pages": max(pages, 1)})


@student_required
def print_vendors(request, user):
    profiles = VendorProfile.objects.filter(vendor_type="printout")
    return ok({
        "vendors": [serialize_print_vendor(profile) for profile in profiles]
    })


def _parse_page_ranges(raw, max_pages):
    pages = set()
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                start, end = int(start), int(end)
            except ValueError:
                continue
            for page in range(min(start, end), max(start, end) + 1):
                if 1 <= page <= max_pages:
                    pages.add(page)
        else:
            try:
                page = int(part)
            except ValueError:
                continue
            if 1 <= page <= max_pages:
                pages.add(page)
    return pages


def _count_pdf_pages(file_obj):
    try:
        file_obj.seek(0)
        from pypdf import PdfReader

        reader = PdfReader(file_obj)
        n = len(reader.pages)
        if n > 0:
            return n
    except Exception:
        pass
    # ⭐ if pypdf fails, count pages via a raw byte scan
    try:
        file_obj.seek(0)
        raw = file_obj.read()
        file_obj.seek(0)
        counts = [int(x) for x in re.findall(rb"/Count\s+(\d+)", raw)]
        pages = max(counts) if counts else 0
        if pages <= 0:
            pages = len(re.findall(rb"/Type\s*/Page[^s]", raw))
        return max(1, pages)
    except Exception:
        return 1


def serialize_print_order(order, for_vendor=False):
    """for_vendor=True -> include student details; mobile only after accept."""
    data = {
        "id": order.id,
        "vendor_id": order.vendor_id,
        "vendor_name": order.vendor.business_name if order.vendor else "",
        "file_name": order.document.name.split("/")[-1] if order.document else "",
        # ⭐ Served through the backend (Cloudinary blocks direct public
        # PDF/raw delivery on free plans — ERR_INVALID_RESPONSE / 401).
        "file_url": (f"/api/print/orders/{order.id}/file/"
                     if order.document else ""),
        "pages": order.pages,
        "copies": order.copies,
        "print_side": order.print_side,
        "bw_pages": order.bw_pages,
        "color_pages": order.color_pages,
        "bw_page_ranges": order.bw_page_ranges,
        "color_page_ranges": order.color_page_ranges,
        "notes": order.notes,
        "txn_id": order.txn_id,
        "txn_last4": order.txn_last4,
        "status": order.status,
        "total_price": float(order.final_amount),
        "created_at_iso": iso(order.created_at),
    }
    if for_vendor:
        student = order.student
        profile = getattr(student, "userprofile", None) if student else None
        name = ""
        phone = ""
        if profile is not None:
            name = profile.full_name or ""
            phone = profile.phone or ""
        if not name and student is not None:
            name = student.get_full_name() or student.username
        # ⭐ mobile number + document file: only after the order is ACCEPTED
        reveal = order.status not in ("pending", "rejected", "cancelled")
        data["student_name"] = name
        data["student_uid"] = student.username if student else ""
        data["student_phone"] = phone if reveal else ""
        if not reveal:
            data["file_url"] = ""
    return data


@csrf_exempt
@student_required
def print_place_order(request, user):
    if request.method != "POST":
        return fail("POST required.", status=405)
    if not _rl_allow(f"printorder:{user.id}", 8, 600):
        return fail("Too many orders in a short time — please wait a "
                    "few minutes.", status=429)
    document = request.FILES.get("document")
    if document is None:
        return fail("Upload a file (field: document).")
    upload_error = check_upload(document, kind="doc", max_mb=25)
    if upload_error:
        return fail(upload_error)
    try:
        vendor_id = int(request.POST.get("vendor_id", 0))
    except ValueError:
        return fail("Select a vendor.")
    vendor = VendorProfile.objects.filter(
        id=vendor_id, vendor_type="printout"
    ).first()
    if vendor is None:
        return fail("Print vendor not found.")
    try:
        copies = max(1, int(request.POST.get("copies", 1)))
    except ValueError:
        copies = 1
    print_side = str(request.POST.get("print_side", "single"))
    bw_ranges = str(request.POST.get("bw_page_ranges", ""))
    color_ranges = str(request.POST.get("color_page_ranges", ""))
    notes = str(request.POST.get("notes", ""))
    txn_id = str(request.POST.get("txn_id", "")).strip()[:64]
    txn_last4 = txn_id[-4:] if txn_id else ""

    pages = _count_pdf_pages(document.file)
    color_pages = _parse_page_ranges(color_ranges, pages)
    bw_pages_set = _parse_page_ranges(bw_ranges, pages)
    if bw_pages_set:
        bw_count = len(bw_pages_set - color_pages)
    else:
        bw_count = pages - len(color_pages)
    color_count = len(color_pages)

    total = (
        bw_count * float(vendor.bw_price_per_page)
        + color_count * float(vendor.color_price_per_page)
    ) * copies

    order = PrintOrder.objects.create(
        vendor=vendor,
        student=user,
        document=document,
        pages=pages,
        copies=copies,
        bw_pages=bw_count,
        color_pages=color_count,
        bw_page_ranges=bw_ranges[:500],
        color_page_ranges=color_ranges[:500],
        print_side=print_side,
        notes=notes,
        txn_id=txn_id,
        txn_last4=txn_last4,
        final_amount=total,
        status="pending",
    )
    # ⭐ v75: the printout portal rings the moment a job lands
    try:
        _notify_vendor(
            vendor, "New printout order 🖨",
            (f"{order.document.name.split('/')[-1]} · {pages} pages "
             f"x {copies} — accept or reject now"),
            push_data={"event": "new_print_order", "portal": "vendor",
                       "order_id": order.id},
        )
    except Exception:
        pass
    return ok({"order": serialize_print_order(order)})


@student_required
def print_my_orders(request, user):
    orders = PrintOrder.objects.filter(student=user).select_related(
        "vendor"
    ).order_by("-created_at")[:40]
    return ok({"orders": [serialize_print_order(order) for order in orders]})


@student_required
def print_vendor_dashboard(request, user):
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    orders = PrintOrder.objects.filter(vendor_id=profile.id).select_related(
        "vendor", "student", "student__userprofile"
    ).order_by("-created_at")[:40]
    return ok({"orders": [
        serialize_print_order(order, for_vendor=True) for order in orders]})


@csrf_exempt
def print_order_file(request, order_id):
    """Secure document download. Cloudinary free accounts block public
    PDF/raw delivery (401 deny/ACL), so this endpoint hands out a signed
    API download link instead. Token via header or ?token= (so the link
    can open in an external browser)."""
    user = user_from_token(request)
    if user is None:
        key = request.GET.get("token", "").strip()
        if key:
            try:
                user = Token.objects.select_related("user").get(key=key).user
            except Token.DoesNotExist:
                user = None
    if user is None:
        return fail("Login required.", status=401)
    try:
        order = PrintOrder.objects.select_related("vendor").get(id=order_id)
    except PrintOrder.DoesNotExist:
        return fail("Print order not found.", status=404)
    is_owner = order.student_id == user.id
    is_vendor = bool(order.vendor and order.vendor.user_id == user.id)
    is_admin = user.is_staff or user.is_superuser
    if not (is_owner or is_vendor or is_admin):
        return fail("Not allowed.", status=403)
    # Vendors can download only after accepting the order.
    if is_vendor and not (is_owner or is_admin) and \
            order.status in ("pending", "rejected", "cancelled"):
        return fail("Accept the order first to download the file.",
                    status=403)
    if not order.document:
        return fail("No file attached to this order.", status=404)
    cloud = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
    if cloud:
        import hashlib
        from urllib.parse import urlencode

        name = order.document.name
        public_id = name if name.startswith("cunnect/") else f"cunnect/{name}"
        params = {
            "public_id": public_id,
            "timestamp": str(int(time.time())),
            "attachment": "true",
        }
        to_sign = "&".join(f"{k}={params[k]}" for k in sorted(params))
        secret = os.environ.get("CLOUDINARY_API_SECRET", "")
        signature = hashlib.sha1((to_sign + secret).encode()).hexdigest()
        query = urlencode({
            **params,
            "api_key": os.environ.get("CLOUDINARY_API_KEY", ""),
            "signature": signature,
        })
        from django.shortcuts import redirect
        return redirect(
            f"https://api.cloudinary.com/v1_1/{cloud}/raw/download?{query}")
    # Local-disk fallback (development).
    from django.conf import settings
    from django.http import FileResponse
    try:
        path = os.path.join(str(settings.MEDIA_ROOT), order.document.name)
        return FileResponse(
            open(path, "rb"),
            as_attachment=True,
            filename=order.document.name.split("/")[-1],
        )
    except Exception:
        return fail("File missing on the server.", status=404)


PRINT_ACTIONS = {
    "accept": ("pending", "accepted"),
    "reject": ("pending", "rejected"),
    "printing": ("accepted", "printing"),
    "ready": ("printing", "ready"),
    "complete": ("ready", "completed"),
}


@csrf_exempt
@student_required
def print_order_action(request, user, order_id, action):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    if action not in PRINT_ACTIONS:
        return fail("Unknown action.", status=404)
    expected_from, new_status = PRINT_ACTIONS[action]
    try:
        order = PrintOrder.objects.get(id=order_id, vendor_id=profile.id)
    except PrintOrder.DoesNotExist:
        return fail("Print order not found.", status=404)
    # ⭐ v55: idempotent — repeating the same action is a silent success.
    if order.status == new_status:
        return ok({"order": serialize_print_order(order)})
    if order.status != expected_from:
        # also allow jumping straight from 'ready' to complete (vendor shortcut).
        if not (action == "complete" and order.status == "ready"):
            return fail(f"Order is currently '{order.status}'.")
    order.status = new_status
    order.save(update_fields=["status", "updated_at"])
    if order.student_id:
        _notify(
            user_id=order.student_id,
            title=f"Print order {new_status}",
            message=f"{order.document.name.split('/')[-1]} is now {new_status}.",
            category="print",
        )
    return ok({"order": serialize_print_order(order)})


@csrf_exempt
@student_required
def print_vendor_prices(request, user):
    if request.method != "POST":
        return fail("POST required.", status=405)
    _, profile = vendor_user(request)
    if profile is None or profile.vendor_type != "printout":
        return fail("Print vendor account not found.", status=401)
    body = json_body(request)
    try:
        bw = float(body.get("bw_price_per_page", profile.bw_price_per_page))
        color = float(
            body.get("color_price_per_page", profile.color_price_per_page)
        )
    except (TypeError, ValueError):
        return fail("Enter a valid price.")
    profile.bw_price_per_page = bw
    profile.color_price_per_page = color
    profile.save(update_fields=["bw_price_per_page", "color_price_per_page"])
    return ok({"vendor": serialize_print_vendor(profile)})


# ---------------------------------------------------------------------
# Chat (network app)
# ---------------------------------------------------------------------


def serialize_room(room, user):
    is_member = room.members.filter(id=user.id).exists()
    pending = RoomJoinRequest.objects.filter(
        room=room, user=user, status="pending"
    ).exists()
    return {
        "id": room.id,
        "name": room.name,
        "privacy": room.privacy,
        "members_count": room.members.count(),
        "online_count": 1 if is_member else 0,
        "official": bool(room.created_by and room.created_by.is_superuser),
        "is_member": is_member,
        "pending": pending,
    }


@student_required
def chat_rooms(request, user):
    public_rooms = ChatRoom.objects.filter(privacy="public")
    member_rooms = ChatRoom.objects.filter(members=user)
    rooms = (public_rooms | member_rooms).distinct().order_by("name")
    return ok({"rooms": [serialize_room(room, user) for room in rooms]})


@csrf_exempt
@student_required
def chat_rooms_create(request, user):
    if request.method != "POST":
        return fail("POST required.", status=405)
    body = json_body(request)
    name = str(body.get("name", "")).strip()
    privacy = str(body.get("privacy", "public"))
    if privacy not in ("public", "private"):
        privacy = "public"
    if not name:
        return fail("Enter a room name.")
    if ChatRoom.objects.filter(name__iexact=name).exists():
        return fail("A room with this name already exists.")
    room = ChatRoom.objects.create(
        name=name, created_by=user, privacy=privacy
    )
    room.members.add(user)
    return ok({"room": serialize_room(room, user)})


def _room_or_fail(name, user, join_public=True):
    try:
        room = ChatRoom.objects.get(name__iexact=name)
    except ChatRoom.DoesNotExist:
        return None, fail("Room not found.", status=404)
    if join_public and room.privacy == "public":
        room.members.add(user)
    return room, None


@student_required
def chat_room_detail(request, user, name):
    room, error = _room_or_fail(name, user)
    if error:
        return error
    if not room.members.filter(id=user.id).exists():
        return fail("You are not a member of this room.", status=403)
    messages = list(
        Message.objects.filter(room=room, deleted_for_everyone=False)
        .select_related("user")
        .prefetch_related("likes")
        .order_by("-created_at")[:100]
    )
    messages.reverse()
    poll = (
        Poll.objects.filter(room=room)
        .prefetch_related("options__votes")
        .order_by("-created_at")
        .first()
    )
    poll_data = None
    if poll is not None:
        poll_data = {
            "id": poll.id,
            "question": poll.question,
            "options": [
                {
                    "id": option.id,
                    "text": option.text,
                    "votes": option.votes.count(),
                    "voted_by_me": option.votes.filter(user=user).exists(),
                }
                for option in poll.options.all()
            ],
        }
    return ok({
        "room": serialize_room(room, user),
        "messages": [serialize_message(message, user) for message in messages],
        "poll": poll_data,
    })


@csrf_exempt
@student_required
def chat_room_join(request, user, name):
    if request.method != "POST":
        return fail("POST required.", status=405)
    room, error = _room_or_fail(name, user, join_public=False)
    if error:
        return error
    if room.members.filter(id=user.id).exists():
        return ok({"joined": True})
    if room.privacy == "public":
        room.members.add(user)
        return ok({"joined": True})
    RoomJoinRequest.objects.get_or_create(room=room, user=user)
    return ok({"joined": False, "requested": True})


@csrf_exempt
@student_required
def chat_send_message(request, user, name):
    if request.method != "POST":
        return fail("POST required.", status=405)
    if not _rl_allow(f"chat:{user.id}", 20, 30):
        return fail("You are sending messages too fast.", status=429)
    room, error = _room_or_fail(name, user)
    if error:
        return error
    if not room.members.filter(id=user.id).exists():
        return fail("Join the room first.", status=403)
    if request.FILES:
        content = str(request.POST.get("content", "")).strip()
    else:
        content = str(json_body(request).get("content", "")).strip()
    image = request.FILES.get("image")
    video = request.FILES.get("video")
    attachment = request.FILES.get("attachment")
    if not content and not image and not video and not attachment:
        return fail("Message is empty.")
    for f, k in ((image, "image"), (video, "video"), (attachment, "doc")):
        if f is not None:
            upload_error = check_upload(f, kind=k, max_mb=25)
            if upload_error:
                return fail(upload_error)
    message = Message.objects.create(
        room=room, user=user, content=content[:2000],
        image=image or None, video=video or None,
        attachment=attachment or None)
    return ok({"message": serialize_message(message, user)})


@csrf_exempt
@student_required
def chat_send_poll(request, user, name):
    if request.method != "POST":
        return fail("POST required.", status=405)
    room, error = _room_or_fail(name, user)
    if error:
        return error
    if not room.members.filter(id=user.id).exists():
        return fail("Join the room first.", status=403)
    body = json_body(request)
    question = str(body.get("question", "")).strip()
    options = [str(o).strip() for o in (body.get("options") or []) if str(o).strip()]
    if not question or len(options) < 2:
        return fail("Question + at least 2 options are required.")
    marker = Message.objects.create(
        room=room, user=user, content=f"📊 {question[:240]}"
    )
    poll = Poll.objects.create(
        message=marker, room=room, created_by=user, question=question[:240]
    )
    for text in options[:8]:
        PollOption.objects.create(poll=poll, text=text[:160])
    return ok({"poll_id": poll.id})


@csrf_exempt
@student_required
def chat_poll_vote(request, user, poll_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    try:
        poll = Poll.objects.get(id=poll_id)
    except Poll.DoesNotExist:
        return fail("Poll not found.", status=404)
    try:
        option_id = int(json_body(request).get("option_id", 0))
    except (TypeError, ValueError):
        return fail("Select an option.")
    option = PollOption.objects.filter(id=option_id, poll=poll).first()
    if option is None:
        return fail("Option not found.")
    PollVote.objects.filter(poll=poll, user=user).delete()
    PollVote.objects.create(poll=poll, option=option, user=user)
    return ok({"voted": True})


@csrf_exempt
@student_required
def chat_message_like(request, user, message_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    try:
        message = Message.objects.get(id=message_id)
    except Message.DoesNotExist:
        return fail("Message not found.", status=404)
    if message.likes.filter(id=user.id).exists():
        message.likes.remove(user)
        liked = False
    else:
        message.likes.add(user)
        liked = True
    return ok({"liked": liked, "like_count": message.likes.count()})


@csrf_exempt
@student_required
def chat_message_pin(request, user, message_id):
    if request.method != "POST":
        return fail("POST required.", status=405)
    try:
        message = Message.objects.get(id=message_id)
    except Message.DoesNotExist:
        return fail("Message not found.", status=404)
    if message.is_pinned:
        message.is_pinned = False
        message.pinned_by = None
        message.pinned_at = None
    else:
        Message.objects.filter(room=message.room, is_pinned=True).update(
            is_pinned=False, pinned_by=None, pinned_at=None
        )
        message.is_pinned = True
        message.pinned_by = user
        message.pinned_at = timezone.now()
    message.save()
    return ok({"pinned": message.is_pinned})


# ---------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------

STORE_CATEGORIES = [
    {
        "key": "food",
        "icon": "🍔",
        "title": "Food",
        "subtitle": "Campus kitchens",
    },
    {
        "key": "printout",
        "icon": "🖨",
        "title": "Printout",
        "subtitle": "PDF print karwao",
    },
    {
        "key": "books",
        "icon": "📚",
        "title": "Books",
        "subtitle": "Coming soon",
    },
    {
        "key": "essentials",
        "icon": "🧴",
        "title": "Essentials",
        "subtitle": "Coming soon",
    },
]


@student_required
def store_home(request, user):
    profiles = VendorProfile.objects.filter(vendor_type="printout")
    return ok({
        "categories": STORE_CATEGORIES,
        "print_vendors": [
            serialize_print_vendor(profile) for profile in profiles
        ],
    })


# ---------------------------------------------------------------------
# UMS (scraper_app bridge)
# ---------------------------------------------------------------------

_UMS_LOCK = threading.Lock()
_UMS_STATE = {}  # uid -> {"scraper": ..., "cookies": ..., "dashboard": {...}}
_UMS_KEEPALIVE_STARTED = False


def _ums_ensure_keepalive():
    """⭐ 10-min background keepalive: session zinda + data auto-fresh,
    so the captcha appears at most 1-2 times a day."""
    global _UMS_KEEPALIVE_STARTED
    with _UMS_LOCK:
        if _UMS_KEEPALIVE_STARTED:
            return
        _UMS_KEEPALIVE_STARTED = True
    threading.Thread(target=_ums_keepalive_loop, daemon=True).start()


def _ums_keepalive_loop():
    # ⭐ v62: 10-min cycle and ONLY for sessions the student actually used
    # in the last 30 minutes. Idle sessions are never scraped — at scale
    # (thousands of users) the portal is hit only for people actively in
    # the app, so the server load stays flat.
    while True:
        time.sleep(600)
        try:
            now = time.time()
            with _UMS_LOCK:
                uids = [u for u, st in _UMS_STATE.items()
                        if st.get("scraper") and st.get("cookies")
                        and now - float(st.get("last_seen") or 0) < 1800]
            for uid in uids:
                try:
                    with _UMS_LOCK:
                        st = _UMS_STATE.get(uid) or {}
                        scraper = st.get("scraper")
                        cookies = st.get("cookies") or {}
                    if not scraper:
                        continue
                    dash = _scrape_ums_dashboard(scraper, cookies, state=st)
                    with _UMS_LOCK:
                        if st.get("last_scrape_ok"):
                            st["dashboard"] = dash
                            st["dashboard_at"] = time.time()
                            st["fail_streak"] = 0
                        else:
                            st["fail_streak"] = int(
                                st.get("fail_streak") or 0) + 1
                    # ⭐ v53: attendance marked while the app is CLOSED ->
                    # the keepalive scrape detects it and pushes instantly.
                    if st.get("last_scrape_ok"):
                        _ums_attendance_notify(uid, dash)
                except Exception as exc:
                    print(f"[UMS-KEEPALIVE] {uid}: {exc}")
        except Exception as exc:
            print(f"[UMS-KEEPALIVE] loop: {exc}")

# ⭐ REAL-TIME + NO RE-LOGIN: persist credentials/cookies to disk
# so the user never has to log in again after a runserver restart or
# is needed - the backend silently reuses cookies / re-authenticates.
_UMS_SAVE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ums_saved_logins.json",
)


def _ums_saved_load():
    try:
        from myapp.models import UmsSaved

        rows = UmsSaved.objects.all()
        data = {r.uid: (r.payload or {}) for r in rows}
        if data:
            return data
    except Exception as exc:
        print(f"[API-UMS] db saved load failed: {exc}")
    try:
        with open(_UMS_SAVE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _ums_saved_update(uid, **kw):
    data = _ums_saved_load()
    entry = data.get(uid) or {}
    for k, v in kw.items():
        if v is not None:
            entry[k] = v
    data[uid] = entry
    try:
        from myapp.models import UmsSaved

        UmsSaved.objects.update_or_create(
            uid=uid, defaults={"payload": entry})
    except Exception as exc:
        print(f"[API-UMS] db persist failed: {exc}")
        try:
            tmp = _UMS_SAVE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, _UMS_SAVE_FILE)
        except Exception as exc2:
            print(f"[API-UMS] file persist failed: {exc2}")


def _ums_access(request, uid):
    """⭐ v65 security: UMS data endpoints require a logged-in app user
    AND that user must own the UMS session (or claim an unowned one).
    Token comes from the Authorization header or ?token= (images/PDFs
    that open in browser/Image.network can't always send headers)."""
    user = user_from_token(request)
    if user is None:
        key = str(request.GET.get("token", "")).strip()
        if key:
            try:
                user = Token.objects.select_related("user").get(key=key).user
            except Token.DoesNotExist:
                user = None
            if user is not None and not user.is_active:
                user = None
    if user is None:
        return None, fail("Login required.", status=401)
    if uid and uid != "__demo__":
        try:
            data = _ums_saved_load()
            entry = data.get(uid) or {}
            owner = str(entry.get("owner") or "")
            if owner and owner != user.username:
                return None, fail(
                    "This UMS account is not linked to your app login.",
                    status=403)
            if entry and not owner:
                # claim the orphan session for this user
                _ums_saved_update(uid, owner=user.username)
        except Exception:
            pass
    return user, None


def _ums_owner(request):
    """⭐ Which app user owns this UMS session (multi-student isolation)."""
    try:
        u = user_from_token(request)
        return u.username if u else ""
    except Exception:
        return ""


def _ums_reauth(uid, password):
    """Silent re-login (only when possible without a captcha). Cookies on success."""
    from scraper_app.scraper_backend import CUIMSScraperBackend

    if not password:
        return None
    try:
        scraper = CUIMSScraperBackend(uid=uid)
        r1 = scraper.execute_stage1() or {}
        if not r1.get("success") or r1.get("has_captcha"):
            return None
        r2 = scraper.execute_stage2(password, captcha_code="") or {}
        if not r2.get("success"):
            return None
        return {"scraper": scraper, "cookies": r2.get("cookies") or {}}
    except Exception as exc:
        print(f"[API-UMS] auto re-auth failed: {exc}")
        return None


def _ums_start_captcha(uid):
    """⭐ ANY-NETWORK: if the portal asks for a captcha, hand the image to the app;
    keep it pending so a live scrape can run as soon as it is verified."""
    import base64

    from scraper_app.scraper_backend import CUIMSScraperBackend

    if not uid or uid == "__demo__":
        return None
    try:
        scraper = CUIMSScraperBackend(uid=uid)
        r1 = scraper.execute_stage1() or {}
        if not r1.get("success") or not r1.get("has_captcha"):
            return None
        if not scraper.captcha_image_bytes:
            return None
        b64 = base64.b64encode(scraper.captcha_image_bytes).decode()
        with _UMS_LOCK:
            st = _UMS_STATE.setdefault(uid, {"uid": uid})
            st["pending_captcha"] = scraper
        return b64
    except Exception as exc:
        print(f"[API-UMS] captcha fetch failed: {exc}")
        return None


def _ums_auto_session(uid):
    """If the session is not in memory (restart), restore it from saved
    cookies/password - never show the user the login screen."""
    if not uid or uid == "__demo__":
        with _UMS_LOCK:
            return _UMS_STATE.get(uid)
    with _UMS_LOCK:
        state = _UMS_STATE.get(uid)
    if state and state.get("scraper") and state.get("cookies"):
        return state
    saved = _ums_saved_load().get(uid) or {}
    if not saved:
        return state
    _ums_ensure_keepalive()
    restored = None
    if saved.get("cookies"):
        from scraper_app.scraper_backend import CUIMSScraperBackend

        restored = {"scraper": CUIMSScraperBackend(uid=uid),
                    "cookies": saved.get("cookies") or {}}
    if saved.get("password"):
        fresh = _ums_reauth(uid, saved["password"])
        if fresh:
            restored = fresh
            _ums_saved_update(uid, cookies=fresh["cookies"])
    if not restored:
        return state
    with _UMS_LOCK:
        old_state = _UMS_STATE.get(uid) or {}
        old_state.update(restored)
        old_state.setdefault("uid", uid)
        _UMS_STATE[uid] = old_state
        return old_state


def _ums_error(result):
    message = result.get("error") or "Could not connect to the UMS portal."
    return fail(message)


@csrf_exempt
@require_http_methods(["POST"])
def ums_stage1(request):
    from scraper_app.scraper_backend import CUIMSScraperBackend

    # ⭐ v65 security: UMS bridge only for logged-in app users + throttled
    if user_from_token(request) is None:
        return fail("Login required.", status=401)
    if not _rl_allow(f"ums1:{client_ip(request)}", 120, 600):
        return fail("Too many attempts. Please wait a moment.", status=429)
    body = json_body(request)
    uid = str(body.get("uid", "")).strip()
    if not uid:
        return fail("Enter your CUIMS UID.")
    if not valid_user_id(uid):
        return fail("Enter a valid CUIMS UID.")
    scraper = CUIMSScraperBackend(uid=uid)
    result = scraper.execute_stage1()
    if not result.get("success"):
        return _ums_error(result)
    import base64

    captcha_b64 = None
    if result.get("has_captcha") and scraper.captcha_image_bytes:
        captcha_b64 = base64.b64encode(scraper.captcha_image_bytes).decode()
    with _UMS_LOCK:
        _UMS_STATE[uid] = {"scraper": scraper, "dashboard": None, "uid": uid}
    return ok({
        "uid": uid,
        "has_captcha": bool(result.get("has_captcha")),
        "captcha_b64": captcha_b64,
    })


@csrf_exempt
@require_http_methods(["POST"])
def ums_stage2(request):
    if user_from_token(request) is None:
        return fail("Login required.", status=401)
    if not _rl_allow(f"ums2:{client_ip(request)}", 120, 600):
        return fail("Too many attempts. Please wait a moment.", status=429)
    body = json_body(request)
    uid = str(body.get("uid", "")).strip()
    password = str(body.get("password", ""))
    captcha = str(body.get("captcha", "")).strip()
    if not uid or not password:
        return fail("UID and password are required.")
    with _UMS_LOCK:
        state = _UMS_STATE.get(uid)
    if state is None:
        return fail("Enter your UID and tap NEXT first (stage 1).", status=400)
    scraper = state["scraper"]
    auth_result = scraper.execute_stage2(password, captcha_code=captcha)
    if not auth_result.get("success"):
        return _ums_error(auth_result)
    cookies = auth_result.get("cookies", {})
    # ⭐ Bind the UMS to this app user (multi-student isolation)
    _ums_saved_update(uid, password=password, cookies=cookies,
                      owner=_ums_owner(request))
    _ums_ensure_keepalive()
    dashboard = _scrape_ums_dashboard(scraper, cookies, state=state)
    with _UMS_LOCK:
        state["cookies"] = cookies
        state["dashboard"] = dashboard
        state["dashboard_at"] = time.time()
    return ok({"uid": uid, **dashboard})


def _scrape_ums_dashboard(scraper, cookies, state=None):
    """Original scraper_app authenticate-flow ka EXACT mirror:
    wahi functions, wahi order (phase-1 parallel + phase-2 results +
    phase-3 risky) + ALL template keys of the original dashboard render —
    results dropdown (available_sessions), SGPA/CGPA, fee normalize
    (has_money/cleared/all_paid), course-modal dlog pack."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from scraper_app.views import (
        academic_semester_number,
        attach_plan_urls,
        clean_attendance_records,
        declared_result_semesters,
        merge_session_pool,
        normalize_fee_data,
        normalize_subject_grades,
        normalize_timetable_map,
        numbered_sessions,
        sort_timetable_slots,
    )

    uid = scraper.uid
    if state is None:
        state = {}
    dashboard = {
        "student_name": "",
        "uid": uid,
        "overall_attendance": None,
        "total_attended": 0,
        "total_held": 0,
        "total_missed": 0,
        "attendance": [],
        "timetable": {},
        "marks": [],
        "subject_grades": [],
        "exam_results": [],
        "available_sessions": [],
        "active_session": "",
        "active_sgpa": "0.00",
        "student_cgpa": "0.00",
        "total_credits": 0,
        "result_pending": False,
        "notices": [],
        "fee_summary": {},
        "fee_records": [],
        "hostel_details": {"found": False},
        "student_profile": {"found": False},
        "course_plan": {"found": False},
    }

    def _parallel(tasks, workers=6):
        out = {}
        if not tasks:
            return out
        with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    out[name] = fut.result()
                except Exception as exc:
                    print(f"[API-UMS] parallel scrape '{name}' failed: {exc}")
                    out[name] = None
        return out

    # ── Attendance (serial, first — same as the original) ──
    try:
        attendance_result = scraper.scrape_attendance_records(cookies) or {}
    except Exception as exc:
        print(f"[API-UMS] attendance failed: {exc}")
        attendance_result = {}
    state["encrypt_codes"] = [
        str(r.get("EncryptCode")).strip()
        for r in (attendance_result.get("records") or [])
        if r.get("EncryptCode")
    ][:3]
    if attendance_result.get("success"):
        records = clean_attendance_records(attendance_result.get("records", []))
        attended_sum = sum(int(r.get("attended", 0) or 0) for r in records)
        held_sum = sum(int(r.get("total", 0) or 0) for r in records)
        dashboard["attendance"] = records
        # ⭐ Portal "Eligible" math (user proof): DL (IDL/ADL/VDL) ya
        # When Medical Leave applies, the portal adjusts delivered/attended/percentage
        # adjusts to eligible numbers. The app must show the SAME values.
        _el_att_sum = 0
        _el_held_sum = 0
        try:
            def _num(x):
                try:
                    return int(float(str(x).strip() or 0))
                except (TypeError, ValueError):
                    return 0

            def _fnum(x):
                try:
                    return float(str(x).replace("%", "").strip() or 0)
                except (TypeError, ValueError):
                    return 0.0

            def _nkey(k):
                return "".join(ch for ch in str(k).lower() if ch.isalnum())

            def _get(_nk, *names):
                for _nm in names:
                    if _nk.get(_nm) not in (None, ""):
                        return _nk[_nm]
                return None

            _el_att_sum = 0
            _el_held_sum = 0
            for _rr in (attendance_result.get("records") or []):
                if not isinstance(_rr, dict):
                    continue
                _nk = {_nkey(k): v for k, v in _rr.items()}
                _ck = _nkey(str(_rr.get("Code") or _rr.get("CourseCode") or ""))
                if not _ck:
                    _t = str(_rr.get("Title") or "")
                    _ck = _nkey(_t.split(":", 1)[0]) if ":" in _t else ""
                if not _ck:
                    continue
                _rec = None
                for _r in records:
                    if _nkey(_r.get("code")) == _ck:
                        _rec = _r
                        break
                if _rec is None:
                    continue
                _dl = _num(_nk.get("idl")) + _num(_nk.get("adl"))                     + _num(_nk.get("vdl"))
                _rec["medical_leave"] = _num(
                    _nk.get("medicalleave") or _nk.get("medical"))
                _ed = _get(_nk, "eligibledelivered",
                           "eligibilitydelivered", "edelivered")
                _ea = _get(_nk, "eligibleattended",
                           "eligibilityattended", "eattended")
                _ep = _get(_nk, "eligiblepercentage",
                           "eligibilitypercentage", "eligibleperc",
                           "epercentage")
                _td = _num(_nk.get("totaldelv") or _nk.get("totaldelivered"))
                if _ed is None and _td and (_dl or _rec["medical_leave"]):
                    _ed = max(_td - _dl - _rec["medical_leave"], 0)
                if _ed is not None and _num(_ed) > 0:
                    _excl = max(_dl, max(_td - _num(_ed), 0))
                    _rec["duty_leave"] = _excl if _excl > 0 else _dl
                    _rec["total"] = _num(_ed)
                    if _ea is not None:
                        _rec["attended"] = _num(_ea)
                    if _ep is not None and _fnum(_ep) > 0:
                        _rec["percentage"] = _fnum(_ep)
                    elif _rec["total"]:
                        _rec["percentage"] = round(
                            _rec["attended"] / _rec["total"] * 100, 2)
                    _pct = float(_rec["percentage"] or 0)
                    _att = int(_rec["attended"] or 0)
                    _tot = int(_rec["total"] or 0)
                    if _pct >= 75:
                        _rec["miss"] = max(
                            0, int((100 * _att - 75 * _tot) // 75))
                        _rec["need"] = 0
                    else:
                        _rec["miss"] = 0
                        _rec["need"] = max(
                            0, int(((75 * _tot - 100 * _att) + 24) // 25))
                else:
                    _rec["duty_leave"] = _dl
                _el_att_sum += int(_rec.get("attended") or 0)
                _el_held_sum += int(_rec.get("total") or 0)
            if records:
                dashboard["total_attended"] = _el_att_sum
                dashboard["total_held"] = _el_held_sum
                dashboard["total_missed"] = max(_el_held_sum - _el_att_sum, 0)
        except Exception:
            dashboard["total_attended"] = attended_sum
            dashboard["total_held"] = held_sum
            dashboard["total_missed"] = max(held_sum - attended_sum, 0)
        # Mirror original dashboard.html: portal overall authoritative,
        # else AVG of per-course percentages (only delivered courses).
        _pcts = [
            float(r.get("percentage", 0) or 0)
            for r in records
            if int(r.get("total", 0) or 0) > 0
        ]
        avg_percentage = round(sum(_pcts) / len(_pcts), 1) if _pcts else 0.0
        overall = attendance_result.get("overall")
        if not (isinstance(overall, (int, float)) and 0 <= float(overall) <= 100):
            # ⭐ portal overall na mile to weighted (attended/held) —
            # a simple average differs from the portal % (the 62.62 vs 60 bug)
            _wa_att = _el_att_sum if _el_held_sum > 0 else attended_sum
            _wa_held = _el_held_sum if _el_held_sum > 0 else held_sum
            overall = (_wa_att / _wa_held * 100) if _wa_held > 0 else avg_percentage
        dashboard["overall_attendance"] = round(float(overall), 2)

    fee_fn = getattr(scraper, "scrape_fee_records", None)

    # ── Phase-1: safe pages parallel (original set + daily attendance) ──
    _p1 = _parallel([
        ("timetable", lambda: scraper.scrape_timetable(cookies)),
        ("marks", lambda: scraper.scrape_marks_records(cookies)),
        ("fees", lambda: fee_fn(cookies) if callable(fee_fn) else None),
        ("notices", lambda: scraper.scrape_home_announcements(cookies)),
        ("profile", lambda: scraper.scrape_student_profile(cookies)),
        ("daily", lambda: scraper.scrape_daily_attendance(
            cookies, encrypt_codes=state.get("encrypt_codes"))),
    ])

    timetable_result = _p1.get("timetable") or {}
    if timetable_result.get("success"):
        dashboard["timetable"] = sort_timetable_slots(
            normalize_timetable_map(timetable_result.get("timetable", {})))

    marks_result = _p1.get("marks") or {}
    if marks_result.get("success"):
        dashboard["marks"] = marks_result.get("marks", []) or []
    session_pool = merge_session_pool(
        marks_result.get("available_sessions") or [], [])
    active_session = str(marks_result.get("active_session") or "")
    state["marks_codes"] = [
        item.get("code", "") for item in marks_result.get("marks", [])
        if item.get("code")
    ]

    fees_result = _p1.get("fees") or None
    if fees_result and fees_result.get("success"):
        state["receipts_map"] = fees_result.get("receipts_map", {}) or {}
        fee_summary, fee_records = normalize_fee_data(
            fees_result.get("summary", {}) or {},
            fees_result.get("records", []) or [],
        )
        # ⭐ Receipt PDFs: original session-proxy -> API proxy (uid query)
        for record in fee_records:
            receipt_no = str(record.get("receipt") or "").strip()
            if receipt_no and receipt_no in state["receipts_map"]:
                record["receipt_url"] = (
                    f"/api/ums/receipt/{receipt_no}/?uid={uid}"
                )
        dashboard["fee_summary"] = fee_summary
        dashboard["fee_records"] = fee_records

    notices_result = _p1.get("notices") or None
    if notices_result and notices_result.get("success"):
        dashboard["notices"] = notices_result.get("announcements", []) or []

    profile_result = _p1.get("profile") or None
    if profile_result and profile_result.get("success"):
        profile_result["has_photo"] = bool(
            profile_result.get("photo_b64")
            or profile_result.get("photo_url")
        )
        dashboard["student_profile"] = profile_result
        dashboard["student_name"] = profile_result.get("name", "")

    daily_result = _p1.get("daily") or None
    daily_attendance = {}
    if daily_result and daily_result.get("success"):
        daily_attendance = daily_result
    # ⭐ Duty/medical leave: the portal does NOT count such lectures as conducted.
    # The scraper marks these tone=present - convert to "leave" here so the
    # app shows a DL chip and no lecture count (not merged into present/absent).
    try:
        _LEAVE_SET = ("dl", "duty", "on duty", "on-duty", "od", "ml",
                      "medical", "leave", "holiday")
        _dl_daily = {}
        for _sub in (daily_attendance.get("subjects") or []):
            if not isinstance(_sub, dict):
                continue
            for _e in (_sub.get("entries") or []):
                if not isinstance(_e, dict):
                    continue
                _st = str(_e.get("status") or "").strip().lower()
                if _st.startswith(_LEAVE_SET):
                    _e["tone"] = "leave"
                    _cc = str(_sub.get("code") or "").strip().upper()
                    _dl_daily[_cc] = _dl_daily.get(_cc, 0) + 1
        # merge daily leave counts into course records (max, to avoid double-counting)
        if _dl_daily:
            for _r in (dashboard.get("attendance") or []):
                _cc = str(_r.get("code") or "").strip().upper()
                if _cc in _dl_daily:
                    _r["duty_leave"] = max(
                        int(_r.get("duty_leave") or 0), _dl_daily[_cc])
    except Exception:
        pass
    state["daily_attendance"] = daily_attendance

    # ── Phase-2: exam results (same as original — marks codes + session) ──
    batch_match = re.match(r"^(\d{2})", str(uid))
    batch_year = int(batch_match.group(1)) if batch_match else None
    results_result = {}
    try:
        results_result = scraper.scrape_exam_results(
            cookies,
            sem_id=active_session or None,
            marks_codes=state.get("marks_codes"),
            semester_number=(
                academic_semester_number(active_session, batch_year)
                if active_session else None
            ),
        ) or {}
    except Exception as exc:
        print(f"[API-UMS] exam results failed: {exc}")

    exam_results = []
    student_cgpa = "0.00"
    result_pending = False
    result_active_sgpa = ""
    result_active_cgpa = ""
    subjects = []
    total_credits = 0
    if results_result.get("success"):
        exam_results = results_result.get("results", []) or []
        student_cgpa = results_result.get("global_cgpa", "0.00")
        result_pending = bool(results_result.get("semester_pending"))
        result_active_sgpa = str(results_result.get("active_sgpa") or "")
        result_active_cgpa = str(results_result.get("active_cgpa") or "")
        subjects, total_credits = normalize_subject_grades(
            results_result.get("subject_grades", []) or [])
        session_pool = merge_session_pool(
            session_pool, results_result.get("available_sems") or [])
        result_active_sem = str(results_result.get("active_sem") or "")
        if not active_session and result_active_sem:
            active_session = result_active_sem
    dashboard["exam_results"] = exam_results
    dashboard["subject_grades"] = subjects
    dashboard["student_cgpa"] = student_cgpa
    dashboard["total_credits"] = total_credits
    dashboard["result_pending"] = result_pending

    # ⭐ Results dropdown (same as the original render — all sessions + pending flag)
    numbered = numbered_sessions(session_pool, uid)
    declared_nums = declared_result_semesters(exam_results)
    dashboard["available_sessions"] = [
        {
            "id": sn["id"],
            "name": f"Semester {sn['sem_num']}",
            "selected": sn["id"] == active_session,
            "pending": sn["sem_num"] not in declared_nums,
        }
        for sn in numbered
    ]
    dashboard["active_session"] = active_session

    # ⭐ Active semester ka SGPA (original regex match + fallbacks)
    active_sem_num = next(
        (sn["sem_num"] for sn in numbered if sn["id"] == active_session),
        None,
    )
    active_sgpa = "0.00"
    if active_sem_num is not None:
        for result in exam_results:
            match = re.search(
                r"\b(?:semester|sem)\s*[-:]?\s*(\d+)\b",
                str(result.get("semester", "")), flags=re.I)
            if match and int(match.group(1)) == active_sem_num:
                active_sgpa = str(result.get("sgpa", "0.00"))
                break
    if active_sgpa == "0.00" and result_active_sgpa:
        active_sgpa = result_active_sgpa
    if active_sgpa == "0.00" and exam_results:
        active_sgpa = str(exam_results[-1].get("sgpa", "0.00"))
    if (not student_cgpa or student_cgpa == "0.00") and result_active_cgpa:
        student_cgpa = result_active_cgpa
        dashboard["student_cgpa"] = student_cgpa
    dashboard["active_sgpa"] = active_sgpa

    # ── Phase-3: risky pages (hostel + course plan) ──
    _p3 = _parallel([
        ("hostel", lambda: scraper.scrape_hostel_details(cookies)),
        ("course_plan", lambda: scraper.scrape_course_plan(cookies)),
    ])
    hostel_result = _p3.get("hostel") or None
    if hostel_result and hostel_result.get("success"):
        dashboard["hostel_details"] = hostel_result

    course_plan_result = _p3.get("course_plan") or None
    plan_credits = {}
    if course_plan_result and (
        course_plan_result.get("success") or course_plan_result.get("found")
    ):
        plan = attach_plan_urls(course_plan_result)
        courses = []
        for index, course in enumerate(plan.get("courses") or []):
            item = dict(course)
            if item.get("plan_view_url"):
                item["plan_view_url"] = f"/api/ums/pdf/{index}/?uid={uid}"
            credits_label = ""
            for meta in item.get("meta") or []:
                cm = re.search(r"(\d+(?:\.\d+)?)\s*Credits?", str(meta), re.I)
                if cm:
                    credits_label = f"{cm.group(1)} Credits"
                    break
            code_key = str(item.get("code") or "").strip().upper()
            if code_key:
                plan_credits[code_key] = (
                    item.get("plan_view_url") or "", credits_label)
            courses.append(item)
        page_pdfs = []
        for offset, entry in enumerate(plan.get("page_pdfs") or []):
            link = dict(entry)
            if link.get("view_url"):
                link["view_url"] = f"/api/ums/pdf/{1000 + offset}/?uid={uid}"
            page_pdfs.append(link)
        plan["courses"] = courses
        plan["page_pdfs"] = page_pdfs
        dashboard["course_plan"] = plan

    # ── ⭐ Course-modal pack (original dlog/dlog_days/ring_off enrich) ──
    try:
        dsubj = (daily_attendance or {}).get("subjects") or []
        smap = {}
        for s in dsubj:
            if isinstance(s, dict):
                key = str(s.get("code") or "").strip().upper()
                if key:
                    smap[key] = s

        def _days_of(entries):
            groups = []
            cur = None
            for e in entries:
                if cur is None or cur["date"] != e.get("date"):
                    cur = {"date": e.get("date", ""),
                           "wday": e.get("wday", ""),
                           "p": 0, "a": 0, "entries": []}
                    groups.append(cur)
                cur["entries"].append(e)
                if e.get("tone") == "present":
                    cur["p"] += 1
                elif e.get("tone") == "absent":
                    cur["a"] += 1
            return groups

        packed = []
        for r in dashboard["attendance"]:
            key = str(r.get("code") or "").strip().upper()
            subj = smap.get(key)
            att = int(r.get("attended") or 0)
            tot = int(r.get("total") or 0)
            try:
                pct = float(r.get("percentage") or 0)
            except (TypeError, ValueError):
                pct = 0.0
            plan_url, credits_label = plan_credits.get(key, ("", ""))
            item = dict(r)
            item["dlog"] = subj
            item["dlog_days"] = (
                _days_of(subj.get("entries") or []) if subj else [])
            item["ring_off"] = round(
                188.5 * (100.0 - min(pct, 100.0)) / 100.0, 1)
            item["dplan_url"] = plan_url
            item["dplan_credits"] = credits_label
            packed.append(item)
        dashboard["attendance"] = packed
    except Exception as exc:
        print(f"[API-UMS] course-modal pack skip: {exc}")

    # ⭐ State extras for semester-switch / receipt / photo proxies
    state["pool"] = session_pool
    state["active_session"] = active_session
    state["batch_year"] = batch_year
    state["cookies"] = cookies
    if state.get("uid"):
        _ums_saved_update(state["uid"], cookies=cookies)
    state["last_scrape_ok"] = bool(
        (attendance_result or {}).get("success")
        or (dashboard.get("timetable") or {}))
    return dashboard


@csrf_exempt
@require_http_methods(["POST"])
def ums_demo(request):
    dashboard = {
        "student_name": "Demo Student",
        "uid": "DEMO0001",
        "overall_attendance": 82.4,
        "total_attended": 91,
        "total_held": 113,
        "student_cgpa": "8.1",
        "active_sgpa": "7.9",
        "total_credits": 20,
        "result_pending": True,
        "active_session": "26271",
        "available_sessions": [
            {"id": "25262", "name": "Semester 1", "selected": False, "pending": False},
            {"id": "26271", "name": "Semester 2", "selected": True, "pending": False},
            {"id": "26272", "name": "Semester 3", "selected": False, "pending": True},
        ],
        "marks": [
            {"code": "CSE201", "title": "Data Structures", "marks": [
                {"element": "Sessional 1", "obtained": 18, "total": 20},
                {"element": "Sessional 2", "obtained": 17, "total": 20},
            ]},
            {"code": "CSE202", "title": "Operating Systems", "marks": [
                {"element": "Sessional 1", "obtained": 15, "total": 20},
            ]},
        ],
        "total_missed": 22,
        "course_plan": {
            "found": True,
            "page_pdfs": [
                {"label": "Academic Calendar 2026-27",
                 "view_url": "http://localhost:8000/api/ums/pdf/?u=cal"},
                {"label": "Syllabus Handbook CSE",
                 "view_url": "http://localhost:8000/api/ums/pdf/?u=syl"},
            ],
            "courses": [
                {"code": "CSE201", "title": "Data Structures",
                 "meta": ["4 CREDITS", "THEORY"],
                 "plan_view_url": "http://localhost:8000/api/ums/pdf/?u=cse201",
                 "plan": [{"rows": [
                     ["Unit", "Topic", "Lectures"],
                     ["1", "Arrays & Stacks", "L1-L6"],
                     ["2", "Trees & Heaps", "L7-L12"],
                 ]}]},
                {"code": "CSE202", "title": "Operating Systems",
                 "meta": ["3 CREDITS", "THEORY"],
                 "plan_view_url": "", "plan": []},
            ],
        },
        "attendance": [
            {"code": "CSE201", "title": "Data Structures", "attended": 34,
             "total": 40, "percentage": 85.0, "miss": 5, "need": 0},
            {"code": "CSE202", "title": "Operating Systems", "attended": 27,
             "total": 38, "percentage": 71.1, "miss": 0, "need": 5},
            {"code": "CSE203", "title": "DBMS", "attended": 30,
             "total": 35, "percentage": 85.7, "miss": 4, "need": 0},
        ],
        "timetable": {
            "MON": {"slots": [
                {"time": "09:00 - 10:00", "type": "THEORY",
                 "title": "Data Structures", "teacher": "Dr. Sharma",
                 "code": "CSE201", "room": "AB-204"},
                {"time": "10:00 - 11:00", "type": "THEORY",
                 "title": "DBMS", "teacher": "Prof. Mehta",
                 "code": "CSE203", "room": "AB-105"},
            ]},
            "TUE": {"slots": [
                {"time": "09:00 - 10:00", "type": "THEORY",
                 "title": "Operating Systems", "teacher": "Dr. Verma",
                 "code": "CSE202", "room": "AB-301"},
                {"time": "14:00 - 16:00", "type": "PRACTICAL",
                 "title": "DS Lab", "teacher": "Dr. Sharma",
                 "code": "CSE201", "room": "Lab-3"},
            ]},
        },
        "exam_results": [
            {"semester": "Sem 1", "sgpa": "8.3", "sessions": [
                {"name": "End Sem", "subjects": [
                    {"code": "CSE101", "title": "Programming Fundamentals",
                     "credits": 4, "internal": 28, "external": 45,
                     "score": 73.0, "grade": "A"},
                    {"code": "CSE102", "title": "Maths I", "credits": 4,
                     "internal": 26, "external": 41, "score": 67.0,
                     "grade": "B+"},
                ]},
            ]},
            {"semester": "Sem 2", "sgpa": "7.9", "sessions": [
                {"name": "End Sem", "subjects": [
                    {"code": "CSE111", "title": "OOP with C++", "credits": 4,
                     "internal": 27, "external": 43, "score": 70.0,
                     "grade": "A"},
                ]},
            ]},
        ],
        "notices": [
            {"title": "Mid-sem exam schedule released",
             "department": "Examination Cell", "date": "21 Aug 2026",
             "desc": "Mid-semester exams will run 1-7 September.",
             "files": [
                 {"name": "exam_schedule.pdf",
                  "url": "http://localhost:8000/api/ums/pdf/?u=exsch"},
             ]},
            {"title": "Tech fest registrations open",
             "department": "Cultural Committee", "date": "18 Aug 2026",
             "desc": "Team registrations open for the CUnnect tech fest."},
        ],
        "fee_summary": {"total": "95,000", "paid": "70,000", "due": "25,000",
                        "has_money": True, "cleared": False, "all_paid": False,
                        "meter": True, "paid_pct": 73.7, "due_pct": 26.3,
                        "latest": "15 Jul 2026", "receipt_count": 2},
        "fee_records": [
            {"receipt": "RCPT-1187", "title": "Exam Fee", "amount": 25000,
             "date": "15 Jul 2026", "status": "Paid", "semester": "2026-27",
             "receipt_url": ""},
            {"receipt": "RCPT-1023", "title": "Tuition Fee", "amount": 45000,
             "date": "10 Jan 2026", "status": "Paid", "semester": "2025-26",
             "receipt_url": ""},
        ],
        "hostel_details": {"found": True, "sections": [
            {"heading": "Hostel", "rows": [
                {"label": "Hostel Name", "value": "Boys Hostel 2"},
                {"label": "Room No", "value": "B2-114"},
                {"label": "Status", "value": "Active"},
            ]},
        ]},
        "student_profile": {"found": True, "name": "Demo Student",
                            "sections": [
            {"heading": "Academic", "rows": [
                {"label": "UID", "value": "DEMO0001"},
                {"label": "Program", "value": "BTech CSE"},
                {"label": "Year", "value": "2nd Year"},
            ]},
        ]},
    }
    with _UMS_LOCK:
        _UMS_STATE["__demo__"] = {"dashboard": dashboard}
        _UMS_STATE["DEMO0001"] = {"dashboard": dashboard}
    return ok({"uid": "DEMO0001", **dashboard})


@student_required
def ums_saved_uids(request, user):
    """⭐ Only THIS app user's own saved UMS accounts (multi-student
    isolation) - another student's UMS is never shown."""
    data = _ums_saved_load()
    uids = [
        str(u)
        for u, e in data.items()
        if isinstance(e, dict)
        and (e.get("password") or e.get("cookies"))
        and str(e.get("owner") or "") == user.username
    ]
    return ok({"uids": uids})


def _ums_attendance_notify(uid, dashboard):
    """Attendance present/absent change -> push to the student (both sync + bg)."""
    try:
        state = _UMS_STATE.get(uid)
        if state is None:
            return
        recs = dashboard.get("attendance") or []
        snap = {}
        for r in recs:
            snap[str(r.get("code") or "")] = (
                int(r.get("attended", 0) or 0),
                int(r.get("total", 0) or 0))
        prev = state.get("att_snap")
        if prev and snap and prev != snap:
            changed = []
            for code, (a, t) in snap.items():
                if not code:
                    continue
                pa, pt = prev.get(code, (a, t))
                if a != pa or t != pt:
                    status = ("Present ✔" if a > pa
                              else ("Absent ✘" if t > pt else "Updated"))
                    changed.append(f"{code}: {status} ({a}/{t})")
            if changed:
                from django.contrib.auth.models import User

                # ⭐ v53: find the app account — UID match first, then the
                # session owner (multi-student safe).
                u = User.objects.filter(username__iexact=uid).first()
                if u is None:
                    owner = str((_ums_saved_load().get(uid) or {})
                                .get("owner") or "")
                    if owner:
                        u = User.objects.filter(username=owner).first()
                if u:
                    _push_user(u.id, "Attendance Updated 📋",
                               " | ".join(changed[:3]), route="ums")
        if snap:
            state["att_snap"] = snap
    except Exception as exc:
        print(f"[UMS-ATT-PUSH] {exc}")


@student_required
def ums_dashboard(request, user):
    """⭐ REAL-TIME: fresh scrape on every app-open/SYNC (refresh=1), else
    4-min TTL. Session mare to saved password se silent re-auth + retry."""
    uid = request.GET.get("uid", "").strip() or "__demo__"
    # ⭐ multi-student isolation: doosre user ka UMS block
    if uid != "__demo__":
        entry = _ums_saved_load().get(uid) or {}
        owner = str(entry.get("owner") or "")
        if owner and owner != user.username:
            return fail("This UMS account is not linked to your app login.",
                        status=403)
    refresh = request.GET.get("refresh", "") in ("1", "true", "yes")
    state = _ums_auto_session(uid)
    if state is None:
        return fail(
            "UMS session not found — please log in to UMS in the app.", status=404)
    # ⭐ v62: mark this session ACTIVE — the keepalive only scrapes
    # sessions used recently, never the whole user base.
    state["last_seen"] = time.time()
    if not state.get("scraper"):
        if state.get("dashboard"):
            return ok(state["dashboard"])
        return fail("UMS session not found — please login again.",
                    status=404)
    now = time.time()
    stale = now - float(state.get("dashboard_at") or 0) > 240
    busy = now - float(state.get("scraping_at") or 0) < 25
    # ⭐ v62: ZERO-WAIT OPEN — whenever a cached dashboard exists, return
    # it IMMEDIATELY and run the fresh scrape in the background (Celery if
    # available, else a daemon thread). The app listens via /api/ums/ping/
    # and picks up the fresh numbers seconds later. Only `live=1`
    # (explicit SYNC / pull-to-refresh) waits for the portal.
    live = request.GET.get("live", "") in ("1", "true", "yes")
    if not live and state.get("dashboard") and not busy and (
            refresh or stale):
        started = False
        try:
            from api_app.tasks import redis_ok, ums_scrape_task
            if redis_ok():
                ums_scrape_task.delay(uid)
                started = True
        except Exception:
            pass
        if not started:
            def _bg_scrape():
                try:
                    dash = _scrape_ums_dashboard(
                        state["scraper"], state.get("cookies") or {},
                        state=state)
                    if state.get("last_scrape_ok") and isinstance(dash, dict):
                        with _UMS_LOCK:
                            state["dashboard"] = dash
                            state["dashboard_at"] = time.time()
                        _ums_attendance_notify(uid, dash)
                except Exception as exc:
                    print(f"[UMS-BG] {uid}: {exc}")
            state["scraping_at"] = now
            threading.Thread(target=_bg_scrape, daemon=True).start()
        return ok(state["dashboard"])
    if not live and state.get("dashboard") and (busy or not stale):
        # Fresh-enough cache (or a scrape already running) — instant reply.
        return ok(state["dashboard"])
    if (refresh or live or stale or not state.get("dashboard")) and not (
            busy and state.get("dashboard")):
        state["scraping_at"] = now
        dashboard = _scrape_ums_dashboard(
            state["scraper"], state.get("cookies") or {}, state=state)
        if state.get("last_scrape_ok"):
            state["fail_streak"] = 0
        else:
            state["fail_streak"] = int(state.get("fail_streak") or 0) + 1
            # ⭐ session-killer re-auth ONLY after 2+ consecutive failures
            if state["fail_streak"] >= 2:
                saved = _ums_saved_load().get(uid) or {}
                fresh = _ums_reauth(uid, saved.get("password") or "")
                if fresh:
                    state["scraper"] = fresh["scraper"]
                    state["cookies"] = fresh["cookies"]
                    _ums_saved_update(uid, cookies=fresh["cookies"])
                    dashboard = _scrape_ums_dashboard(
                        state["scraper"], state["cookies"], state=state)
                    if state.get("last_scrape_ok"):
                        state["fail_streak"] = 0
        if not state.get("last_scrape_ok"):
            # ⭐ On outside networks the portal asks for a captcha - send the image
            # to the app, the student verifies it, then a live scrape runs.
            # If cached data exists, ask for a captcha at most every 10 min (no spam).
            cached = state.get("dashboard") or {}
            has_data = bool(cached.get("attendance") or cached.get("name")
                            or cached.get("courses") or cached.get("result")
                            or cached.get("fees"))
            live = request.GET.get("live", "") in ("1", "true", "yes")
            due = now - float(state.get("last_captcha_at") or 0) > 600
            if (not has_data) or (live and due):
                b64 = _ums_start_captcha(uid)
                if b64:
                    state["last_captcha_at"] = now
                    dashboard = dict(cached)
                    dashboard["needs_captcha"] = True
                    dashboard["captcha_b64"] = b64
                else:
                    dashboard = cached
            else:
                dashboard = {k: v for k, v in cached.items()
                             if k not in ("needs_captcha", "captcha_b64")}
                dashboard["stale"] = True
        with _UMS_LOCK:
            state["dashboard"] = dashboard
            state["dashboard_at"] = time.time()
        # ⭐ attendance marked present/absent -> push to the student
        if state.get("last_scrape_ok"):
            _ums_attendance_notify(uid, dashboard)
    return ok(state["dashboard"])


@csrf_exempt
@require_http_methods(["GET"])
def ums_captcha(request):
    """⭐ Fresh captcha image (app ka 'Verification Required' dialog)."""
    uid = request.GET.get("uid", "").strip()
    _, err = _ums_access(request, uid)
    if err is not None:
        return err
    b64 = _ums_start_captcha(uid)
    if not b64:
        return fail("The portal is not asking for a captcha / portal unreachable.")
    return ok({"needs_captcha": True, "captcha_b64": b64})


@csrf_exempt
@require_http_methods(["POST"])
def ums_verify_captcha(request):
    """⭐ The student's captcha code -> portal login -> instant live scrape."""
    body = json_body(request)
    uid = str(body.get("uid", "")).strip()
    _, err = _ums_access(request, uid)
    if err is not None:
        return err
    code = str(body.get("code", "")).strip()
    if not uid or not code:
        return fail("Enter the captcha code.")
    with _UMS_LOCK:
        state = _UMS_STATE.get(uid) or {}
        scraper = state.get("pending_captcha")
    if scraper is None:
        return fail("Captcha expired — refresh and try again.",
                    status=400)
    saved = _ums_saved_load().get(uid) or {}
    password = saved.get("password") or ""
    if not password:
        return fail("Saved password not found — please log in to UMS again.",
                    status=400)
    auth = scraper.execute_stage2(password, captcha_code=code) or {}
    if not auth.get("success"):
        return fail(str(auth.get("error") or
                        "Incorrect captcha — try again."))
    cookies = auth.get("cookies") or {}
    _ums_saved_update(uid, cookies=cookies, owner=_ums_owner(request))
    with _UMS_LOCK:
        state["scraper"] = scraper
        state["cookies"] = cookies
        state.pop("pending_captcha", None)
        _UMS_STATE[uid] = state
    _ums_ensure_keepalive()
    dashboard = _scrape_ums_dashboard(scraper, cookies, state=state)
    with _UMS_LOCK:
        state["dashboard"] = dashboard
        state["dashboard_at"] = time.time()
    return ok({"uid": uid, **dashboard})


def ums_course_pdf(request, index):
    """Lecture-plan PDF proxy — delegates to the original course_plan_pdf_view
    delegates by injecting the session (Flutter has no web
    session, so state is restored from the uid query)."""
    from scraper_app.views import course_plan_pdf_view

    uid = request.GET.get("uid", "").strip()
    _, err = _ums_access(request, uid)
    if err is not None:
        return err
    state = _ums_auto_session(uid)
    if not state or not state.get("scraper"):
        return HttpResponseNotFound(
            "UMS session not found — please log in to UMS again in the app."
        )
    scraper = state["scraper"]
    request.session["scraper_state"] = {
        "base_url": getattr(scraper, "base_url", None),
        "uid": uid,
        "cookies": state.get("cookies") or {},
    }
    request.session["course_plan"] = (state.get("dashboard") or {}).get(
        "course_plan"
    ) or {}
    request.session.modified = True
    return course_plan_pdf_view(request, index)


@student_required
def ums_semester(request, user):
    """⭐ Results dropdown: session tap -> us semester ke marks + results
    (original ?tab=marks&session_id=X flow ka mirror)."""
    from scraper_app.views import (
        declared_result_semesters,
        merge_session_pool,
        normalize_subject_grades,
        numbered_sessions,
    )

    uid = request.GET.get("uid", "").strip() or str(user.username)
    _, err = _ums_access(request, uid)
    if err is not None:
        return err
    state = _ums_auto_session(uid)
    if not state or not state.get("scraper"):
        return fail("UMS session not found — please login again.", status=404)
    requested = str(request.GET.get("session_id") or "").strip()
    dashboard = dict(state.get("dashboard") or {})
    current = str(state.get("active_session") or "")
    pool = state.get("pool") or []
    if not requested:
        requested = current

    if requested and requested != current:
        scraper = state["scraper"]
        cookies = state.get("cookies") or {}
        try:
            marks_result = scraper.scrape_marks_records(
                cookies_dict=cookies, session_id=requested) or {}
        except Exception as exc:
            print(f"[API-UMS] semester marks failed: {exc}")
            marks_result = {}
        numbered = numbered_sessions(pool, uid)
        sem_num = next(
            (sn["sem_num"] for sn in numbered if sn["id"] == requested),
            None,
        )
        codes = [
            item.get("code", "")
            for item in marks_result.get("marks", [])
            if item.get("code")
        ]
        try:
            exam_result = scraper.scrape_exam_results(
                cookies_dict=cookies,
                sem_id=requested,
                marks_codes=codes,
                semester_number=sem_num,
            ) or {}
        except Exception as exc:
            print(f"[API-UMS] semester results failed: {exc}")
            exam_result = {}
        if not marks_result.get("success") and not exam_result.get("success"):
            return fail(
                "This semester was not found on the portal — try again later.",
                status=502,
            )
        if marks_result.get("success"):
            dashboard["marks"] = marks_result.get("marks", []) or []
        if exam_result.get("success"):
            subjects, total_credits = normalize_subject_grades(
                exam_result.get("subject_grades", []) or [])
            dashboard["exam_results"] = exam_result.get("results", []) or []
            dashboard["subject_grades"] = subjects
            dashboard["student_cgpa"] = exam_result.get("global_cgpa", "0.00")
            dashboard["result_pending"] = bool(
                exam_result.get("semester_pending"))
            dashboard["total_credits"] = total_credits
            pool = merge_session_pool(
                pool, exam_result.get("available_sems") or [])
            state["pool"] = pool
            state["active_session"] = requested
            dashboard["active_session"] = requested
            active_sgpa = "0.00"
            for result in dashboard["exam_results"]:
                match = re.search(
                    r"\b(?:semester|sem)\s*[-:]?\s*(\d+)\b",
                    str(result.get("semester", "")), flags=re.I)
                if (match and sem_num is not None
                        and int(match.group(1)) == sem_num):
                    active_sgpa = str(result.get("sgpa", "0.00"))
                    break
            if active_sgpa == "0.00" and exam_result.get("active_sgpa"):
                active_sgpa = str(exam_result.get("active_sgpa"))
            if active_sgpa == "0.00" and dashboard["exam_results"]:
                active_sgpa = str(
                    dashboard["exam_results"][-1].get("sgpa", "0.00"))
            dashboard["active_sgpa"] = active_sgpa
            if ((not dashboard.get("student_cgpa")
                    or dashboard["student_cgpa"] == "0.00")
                    and exam_result.get("active_cgpa")):
                dashboard["student_cgpa"] = str(
                    exam_result.get("active_cgpa"))
        # ⭐ Refresh the dropdown (the pool can change)
        declared_nums = declared_result_semesters(
            dashboard.get("exam_results") or [])
        active_now = str(dashboard.get("active_session") or "")
        dashboard["available_sessions"] = [
            {
                "id": sn["id"],
                "name": f"Semester {sn['sem_num']}",
                "selected": sn["id"] == active_now,
                "pending": sn["sem_num"] not in declared_nums,
            }
            for sn in numbered_sessions(pool, uid)
        ]
        state["dashboard"] = dashboard
    return ok(dashboard)


def ums_fee_receipt(request, receipt_id):
    """⭐ Fee receipt PDF proxy — original fee_receipt_view ko state se
    delegates by injecting the session (uid query, opens directly in the browser)."""
    from scraper_app.views import fee_receipt_view

    uid = request.GET.get("uid", "").strip()
    _, err = _ums_access(request, uid)
    if err is not None:
        return err
    state = _ums_auto_session(uid)
    if not state or not state.get("scraper"):
        return HttpResponseNotFound(
            "UMS session not found — please log in to UMS again in the app."
        )
    scraper = state["scraper"]
    request.session["scraper_state"] = {
        "base_url": getattr(scraper, "base_url", None),
        "uid": uid,
        "cookies": state.get("cookies") or {},
    }
    request.session["fee_receipts_map"] = state.get("receipts_map") or {}
    request.session.modified = True
    return fee_receipt_view(request, receipt_id)


def ums_profile_photo(request):
    """⭐ Profile photo proxy — original profile_photo_view mirror."""
    from scraper_app.views import profile_photo_view

    uid = request.GET.get("uid", "").strip()
    _, err = _ums_access(request, uid)
    if err is not None:
        return err
    state = _ums_auto_session(uid)
    if not state or not state.get("scraper"):
        return HttpResponseNotFound("UMS session not found.")
    scraper = state["scraper"]
    request.session["scraper_state"] = {
        "base_url": getattr(scraper, "base_url", None),
        "uid": uid,
        "cookies": state.get("cookies") or {},
    }
    request.session["student_profile"] = (
        (state.get("dashboard") or {}).get("student_profile") or {}
    )
    request.session.modified = True
    return profile_photo_view(request)


ID_CARD_MAX_BYTES = 6 * 1024 * 1024


@csrf_exempt
def ums_id_card(request):
    """⭐ College ID card — GET: image serve, POST {image: dataURL}: save
    (original id_card_upload_view/image_view ka API mirror, state-based)."""
    uid = request.GET.get("uid", "").strip()
    _, err = _ums_access(request, uid)
    if err is not None:
        return err
    state = _ums_auto_session(uid)
    if not state:
        if request.method == "GET":
            return HttpResponse(status=404)
        return JsonResponse(
            {"ok": False, "error": "UMS login required"}, status=401)

    if request.method == "GET":
        b64 = state.get("id_card")
        if not b64:
            return HttpResponse(status=404)
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return HttpResponse(status=404)
        resp = HttpResponse(
            raw, content_type=state.get("id_card_type") or "image/jpeg")
        resp["Cache-Control"] = "private, max-age=60"
        return resp

    body = json_body(request)
    data_url = str(body.get("image") or "")
    if not data_url.startswith("data:image/") or "," not in data_url:
        return JsonResponse({"ok": False, "error": "Only image files are allowed."})
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except Exception:
        return JsonResponse({"ok": False, "error": "The image data is corrupt."})
    if not raw:
        return JsonResponse({"ok": False, "error": "empty image"})
    if len(raw) > ID_CARD_MAX_BYTES:
        return JsonResponse({"ok": False, "error": "The image is too large."})
    if raw.startswith(b"\xff\xd8\xff"):
        ctype = "image/jpeg"
    elif raw.startswith(b"\x89PNG"):
        ctype = "image/png"
    elif raw.startswith(b"RIFF"):
        ctype = "image/webp"
    else:
        return JsonResponse(
            {"ok": False, "error": "Please send a jpeg/png/webp image."})
    with _UMS_LOCK:
        state["id_card"] = base64.b64encode(raw).decode("ascii")
        state["id_card_type"] = ctype
        state["id_card_v"] = int(time.time())
    print(f"[API-IDCard] upload ok: uid={uid} size={len(raw) // 1024}KB")
    return JsonResponse({"ok": True, "v": state["id_card_v"]})


@csrf_exempt
@require_http_methods(["POST"])
def ums_id_card_remove(request):
    uid = request.GET.get("uid", "").strip()
    _, err = _ums_access(request, uid)
    if err is not None:
        return err
    state = _ums_auto_session(uid)
    if state:
        for key in ("id_card", "id_card_type", "id_card_v"):
            state.pop(key, None)
    return JsonResponse({"ok": True})


def ums_ping(request):
    """⭐ Realtime sync (original dashboard_data ka lite mirror) —
    cached attendance numbers + alive flag, without hitting the portal."""
    _, err = _ums_access(request, request.GET.get("uid", "").strip())
    if err is not None:
        return err
    uid = request.GET.get("uid", "").strip()
    state = _ums_auto_session(uid)
    if not state or not state.get("dashboard"):
        return JsonResponse({"ok": False, "alive": False}, status=401)
    # ⭐ v62: ping = the student is actively on the UMS screen
    state["last_seen"] = time.time()
    dash = state["dashboard"]
    return JsonResponse({
        "ok": True,
        "alive": True,
        "attendance": {
            "global": dash.get("overall_attendance") or 0,
            "attended": dash.get("total_attended") or 0,
            "held": dash.get("total_held") or 0,
            "records": [
                {
                    "code": r.get("code", ""),
                    "percentage": r.get("percentage") or 0,
                    "attended": r.get("attended") or 0,
                    "total": r.get("total") or 0,
                    "miss": r.get("miss") or 0,
                    "need": r.get("need") or 0,
                }
                for r in (dash.get("attendance") or [])
                if isinstance(r, dict)
            ],
        },
    })



@csrf_exempt
@require_http_methods(["POST"])
def ums_logout(request):
    body = json_body(request)
    uid = str(body.get("uid", "")).strip()
    # ⭐ v65 security: login + ownership required — a stranger cannot
    # wipe another student's saved UMS session by guessing the UID.
    _, err = _ums_access(request, uid)
    if err is not None:
        return err
    with _UMS_LOCK:
        if uid and uid in _UMS_STATE:
            del _UMS_STATE[uid]
            try:
                _data = _ums_saved_load()
                if uid in _data:
                    del _data[uid]
                    with open(_UMS_SAVE_FILE, "w", encoding="utf-8") as _f:
                        json.dump(_data, _f)
            except Exception:
                pass
        _UMS_STATE.pop("__demo__", None)
    return ok({"logged_out": True})


# ---------------------------------------------------------------------
# ⭐ ADMIN PANEL — full app management from inside the CUnnect app.
# Login with a Django superuser/staff account (username + password).
# ---------------------------------------------------------------------


def admin_user(request):
    """User resolved from the token, only if staff/superuser."""
    user = user_from_token(request)
    if user is None or not (user.is_staff or user.is_superuser):
        return None
    return user


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = admin_user(request)
        if user is None:
            return fail("Admin login required.", status=401)
        return view(request, user, *args, **kwargs)

    return wrapper


@csrf_exempt
@require_http_methods(["POST"])
@throttle("alogin", 60, 900, body_field="username", target_limit=8,
          target_window=900)
def admin_login(request):
    body = json_body(request)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username or not password:
        return fail("Both username and password are required.")
    user = authenticate(request, username=username, password=password)
    if user is None or not (user.is_staff or user.is_superuser):
        return fail("Invalid admin credentials.", status=401)
    token, _ = Token.objects.get_or_create(user=user)
    return ok({
        "token": token.key,
        "username": user.username,
        "is_superuser": user.is_superuser,
    })


@admin_required
def admin_overview(request, user):
    """Dashboard numbers + live user count (app currently open)."""
    now_ts = time.time()
    live_ids = [uid for uid, ts in list(_LAST_SEEN.items())
                if now_ts - ts <= LIVE_WINDOW_SECONDS]
    today = timezone.now().date()
    food_today = Order.objects.filter(created_at__date=today)
    print_today = PrintOrder.objects.filter(created_at__date=today)
    from myapp.models import HostelOrder
    hostel_today = HostelOrder.objects.filter(created_at__date=today)
    return ok({
        "live_users": len(live_ids),
        "live_window_seconds": LIVE_WINDOW_SECONDS,
        "total_users": User.objects.filter(is_active=True).count(),
        "total_students": UserProfile.objects.count(),
        "total_vendors": VendorProfile.objects.count(),
        "total_delivery": DeliveryProfile.objects.count(),
        "food_orders_today": food_today.count(),
        "food_sales_today": float(sum(
            o.total_amount for o in food_today.filter(status="completed"))),
        "print_orders_today": print_today.count(),
        "hostel_orders_today": hostel_today.count(),
        "orders_pending": Order.objects.filter(status="pending").count(),
        "print_pending": PrintOrder.objects.filter(status="pending").count(),
        "support_pending": SupportRequest.objects.filter(
            status="pending").count(),
    })


@admin_required
def admin_live_users(request, user):
    """Who has the app open right now (last API call <= window)."""
    now_ts = time.time()
    rows = []
    for uid, ts in sorted(_LAST_SEEN.items(), key=lambda x: -x[1]):
        age = now_ts - ts
        if age > LIVE_WINDOW_SECONDS:
            continue
        u = User.objects.filter(id=uid).first()
        if u is None:
            continue
        profile = UserProfile.objects.filter(user=u).first()
        vendor = VendorProfile.objects.filter(user=u).first()
        kind = "student"
        name = (profile.full_name if profile and profile.full_name
                else (u.get_full_name() or u.username))
        if vendor:
            kind = f"vendor ({vendor.vendor_type})"
            name = vendor.business_name
        elif u.is_staff or u.is_superuser:
            kind = "admin"
        rows.append({
            "user_id": uid,
            "username": u.username,
            "name": name,
            "kind": kind,
            "seconds_ago": int(age),
        })
    return ok({"count": len(rows), "users": rows[:200]})


def _serialize_admin_vendor(v):
    return {
        "id": v.id,
        "business_name": v.business_name,
        "vendor_type": v.vendor_type,
        "phone": v.phone or "",
        "owner_username": v.user.username,
        "kitchen_open": v.kitchen_open,
        "upi_id": v.upi_id or "",
        "qr_url": v.upi_qr_image.url if v.upi_qr_image else "",
        "bw_price_per_page": float(v.bw_price_per_page),
        "color_price_per_page": float(v.color_price_per_page),
    }


@csrf_exempt
@admin_required
def admin_vendors(request, user):
    """GET list; POST create a vendor (with a login user)."""
    if request.method == "POST":
        body = json_body(request)
        name = str(body.get("business_name", "")).strip()
        phone = str(body.get("phone", "")).strip()
        password = str(body.get("password", ""))
        vtype = str(body.get("vendor_type", "food")).strip().lower()[:30]
        # Built-in types (⭐ v66 adds "ride") OR a custom store section key.
        if vtype not in ("food", "printout", "hostel", "ride"):
            from myapp.models import StoreSection
            if not StoreSection.objects.filter(key=vtype).exists():
                return fail(
                    "vendor_type must be food, printout, hostel, ride or "
                    "the key of a store section you created.")
        if not name or not phone or not password:
            return fail("business_name, phone and password are required.")
        if VendorProfile.objects.filter(phone=phone).exists():
            return fail("A vendor with this phone already exists.")
        username = f"vendor_{phone}"
        if User.objects.filter(username=username).exists():
            username = f"vendor_{phone}_{int(time.time())}"
        vuser = User.objects.create_user(username=username, password=password)
        v = VendorProfile.objects.create(
            user=vuser, business_name=name, phone=phone, vendor_type=vtype)
        return ok({"vendor": _serialize_admin_vendor(v)})
    vendors = VendorProfile.objects.select_related("user").order_by("id")
    return ok({"vendors": [_serialize_admin_vendor(v) for v in vendors]})


@csrf_exempt
@admin_required
def admin_vendor_detail(request, user, vendor_id):
    """POST update fields / reset password; DELETE remove vendor+user."""
    try:
        v = VendorProfile.objects.select_related("user").get(id=vendor_id)
    except VendorProfile.DoesNotExist:
        return fail("Vendor not found.", status=404)
    if request.method == "DELETE":
        vuser = v.user
        v.delete()
        vuser.delete()
        return ok({"deleted": True})
    if request.method != "POST":
        return fail("POST or DELETE only.", status=405)
    body = json_body(request)
    name = str(body.get("business_name", "")).strip()
    phone = str(body.get("phone", "")).strip()
    vtype = str(body.get("vendor_type", "")).strip()
    password = str(body.get("password", ""))
    if name:
        v.business_name = name
    if phone:
        if VendorProfile.objects.filter(phone=phone).exclude(
                id=v.id).exists():
            return fail("This phone is already used by another vendor.")
        v.phone = phone
    if vtype:
        vtype = vtype.lower()[:30]
        if vtype in ("food", "printout", "hostel"):
            v.vendor_type = vtype
        else:
            from myapp.models import StoreSection
            if StoreSection.objects.filter(key=vtype).exists():
                v.vendor_type = vtype
    if "kitchen_open" in body:
        v.kitchen_open = bool(body.get("kitchen_open"))
    if "upi_id" in body:
        v.upi_id = str(body.get("upi_id", "")).strip()[:120]
    v.save()
    if password:
        v.user.set_password(password)
        v.user.save(update_fields=["password"])
    return ok({"vendor": _serialize_admin_vendor(v)})


@admin_required
def admin_students(request, user):
    """Student list with basic profile info (search via ?q=)."""
    q = request.GET.get("q", "").strip()
    profiles = UserProfile.objects.select_related("user").order_by("-id")
    if q:
        from django.db.models import Q
        profiles = profiles.filter(
            Q(full_name__icontains=q) | Q(user__username__icontains=q) |
            Q(phone__icontains=q))
    rows = []
    for p in profiles[:300]:
        rows.append({
            "id": p.user_id,
            "uid": p.user.username,
            "name": p.full_name or p.user.get_full_name() or "",
            "phone": p.phone or "",
            "branch": p.branch or "",
            "year": p.year or "",
            "verified": p.is_verified,
            "active": p.user.is_active,
        })
    return ok({"students": rows, "total": profiles.count()})


@csrf_exempt
@admin_required
def admin_student_action(request, user, user_id, action):
    """POST enable/disable/delete a student account."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    target = User.objects.filter(id=user_id).first()
    if target is None:
        return fail("User not found.", status=404)
    if target.is_superuser:
        return fail("Cannot modify a superuser.", status=403)
    from myapp.models import DisabledAccount
    if action == "disable":
        target.is_active = False
        target.save(update_fields=["is_active"])
        DisabledAccount.objects.get_or_create(user=target)
        # ⭐ Kill the session token -> the app logs the user out on its
        # very next API call (automatic logout).
        Token.objects.filter(user=target).delete()
        _LAST_SEEN.pop(target.id, None)
    elif action == "enable":
        target.is_active = True
        target.save(update_fields=["is_active"])
        DisabledAccount.objects.filter(user=target).delete()
    elif action == "delete":
        target.delete()
        return ok({"deleted": True})
    else:
        return fail("Unknown action.", status=404)
    return ok({"active": target.is_active})


@admin_required
def admin_orders(request, user):
    """⭐ v61: latest orders for EVERY store section (?kind=<section key>).
    Built-ins map to their own order tables; custom sections list their
    food-style orders filtered by the section's vendors. Every row
    carries vendor_name so multi-vendor stores stay unambiguous."""
    kind = request.GET.get("kind", "food")
    if kind == "print" or kind == "printout":
        orders = PrintOrder.objects.select_related(
            "vendor", "student").order_by("-created_at")[:100]
        return ok({"orders": [
            serialize_print_order(o, for_vendor=True) for o in orders]})
    if kind == "hostel":
        from myapp.models import HostelOrder
        hostel_vendor = _hostel_vendor()
        vname = hostel_vendor.business_name if hostel_vendor else ""
        rows = []
        for o in HostelOrder.objects.order_by("-created_at")[:100]:
            rows.append({
                "id": o.id,
                "order_no": o.order_no,
                "orderer_name": o.orderer_name,
                "recipient_name": o.recipient_name,
                "recipient_mobile": o.recipient_mobile,
                "vendor_name": vname,
                "status": o.status,
                "total": float(o.total),
                "items": o.items or [],
                "txn_id": o.txn_id,
                "created_at_iso": iso(o.created_at),
            })
        return ok({"orders": rows})
    # ⭐ v62: join customer + profile too — serialize_order reads them,
    # so this cuts ~200 extra queries per page load.
    qs = Order.objects.select_related(
        "vendor", "customer", "customer__userprofile").order_by("-created_at")
    if kind != "food":
        # custom section: only orders of vendors that belong to it
        qs = qs.filter(vendor__vendor_type=kind)
    return ok({"orders": [
        serialize_order(o, include_items=False) for o in qs[:100]]})


@admin_required
def admin_order_kinds(request, user):
    """⭐ v61: every store section as an order tab — key + title."""
    from myapp.models import StoreSection
    _ensure_builtin_sections()
    kinds = []
    for s in StoreSection.objects.all():
        key = "print" if s.key == "printout" else s.key
        kinds.append({"key": key, "title": s.title})
    return ok({"kinds": kinds})


@csrf_exempt
@admin_required
def admin_order_status(request, user, kind, order_id):
    """POST {status: ...} — force-set any order's status."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    status_new = str(json_body(request).get("status", "")).strip()
    if kind == "print":
        valid = [s for s, _ in PrintOrder.STATUS_CHOICES]
        obj = PrintOrder.objects.filter(id=order_id).first()
    elif kind == "hostel":
        from myapp.models import HostelOrder
        valid = [s for s, _ in HostelOrder.STATUS_CHOICES]
        obj = HostelOrder.objects.filter(id=order_id).first()
    else:
        valid = [s for s, _ in Order.STATUS_CHOICES]
        obj = Order.objects.filter(id=order_id).first()
    if obj is None:
        return fail("Order not found.", status=404)
    if status_new not in valid:
        return fail(f"Status must be one of: {', '.join(valid)}")
    obj.status = status_new
    obj.save()
    return ok({"status": obj.status})


def _serialize_admin_item(i):
    return {
        "id": i.id,
        "name": i.name,
        "price": float(i.price),
        "category": i.category,
        "vendor_id": i.vendor_id,
        "vendor_name": i.vendor.business_name if i.vendor else "",
        "is_available": i.is_available,
        "is_veg": i.is_veg,
        "stock": i.stock,
        "image_url": media_url(i.image),
    }


@csrf_exempt
@admin_required
def admin_food_items(request, user):
    """GET all items; POST create (vendor_id, name, price, ...)."""
    if request.method == "POST":
        body = json_body(request)
        try:
            vendor = VendorProfile.objects.get(id=int(body.get("vendor_id")))
            price = float(body.get("price"))
        except (TypeError, ValueError, VendorProfile.DoesNotExist):
            return fail("Valid vendor_id and price are required.")
        name = str(body.get("name", "")).strip()
        if not name:
            return fail("Item name is required.")
        item = FoodItem.objects.create(
            vendor=vendor,
            name=name,
            price=price,
            description=str(body.get("description", "")).strip()[:250],
            category=str(body.get("category", "wrap")).strip() or "wrap",
            is_veg=bool(body.get("is_veg", True)),
            is_available=bool(body.get("is_available", True)),
        )
        return ok({"item": _serialize_admin_item(item)})
    items = FoodItem.objects.select_related("vendor").order_by("vendor_id", "id")
    return ok({"items": [_serialize_admin_item(i) for i in items]})


@csrf_exempt
@admin_required
def admin_food_item_detail(request, user, item_id):
    """POST update; DELETE remove."""
    item = FoodItem.objects.filter(id=item_id).first()
    if item is None:
        return fail("Item not found.", status=404)
    if request.method == "DELETE":
        item.delete()
        return ok({"deleted": True})
    if request.method != "POST":
        return fail("POST or DELETE only.", status=405)
    body = json_body(request)
    if "name" in body:
        item.name = str(body.get("name", "")).strip()[:100] or item.name
    if "price" in body:
        try:
            item.price = float(body.get("price"))
        except (TypeError, ValueError):
            return fail("Enter a valid price.")
    if "description" in body:
        item.description = str(body.get("description", "")).strip()[:250]
    if "category" in body:
        item.category = str(body.get("category", "")).strip() or item.category
    if "is_veg" in body:
        item.is_veg = bool(body.get("is_veg"))
    if "is_available" in body:
        item.is_available = bool(body.get("is_available"))
    if "stock" in body:
        try:
            item.stock = int(body.get("stock"))
        except (TypeError, ValueError):
            pass
    item.save()
    return ok({"item": _serialize_admin_item(item)})


def _serialize_admin_coupon(c):
    return {
        "id": c.id,
        "code": c.code,
        "discount_type": c.discount_type,
        "discount_value": float(c.discount_value),
        "minimum_order_value": float(c.minimum_order_value),
        "offer_text": c.offer_text,
        "label": c.discount_label,
        "one_time_per_user": c.one_time_per_user,
        "is_active": c.is_active,
    }


@csrf_exempt
@admin_required
def admin_coupons(request, user):
    """GET list; POST create a coupon."""
    if request.method == "POST":
        body = json_body(request)
        code = str(body.get("code", "")).strip().upper()
        if not code:
            return fail("Coupon code is required.")
        if Coupon.objects.filter(code=code).exists():
            return fail("This coupon code already exists.")
        dtype = str(body.get("discount_type", "percentage"))
        if dtype not in ("percentage", "fixed", "bogo", "addon"):
            dtype = "percentage"
        offer_text = str(body.get("offer_text", "")).strip()[:200]
        if dtype in ("bogo", "addon"):
            value = 0.0
            if not offer_text:
                return fail("Describe the offer (e.g. Buy 1 Burger, "
                            "Get 1 Free).")
        else:
            try:
                value = float(body.get("discount_value"))
            except (TypeError, ValueError):
                return fail("Enter a valid discount value.")
        c = Coupon.objects.create(
            code=code,
            discount_type=dtype,
            discount_value=value,
            offer_text=offer_text,
            minimum_order_value=float(body.get("minimum_order_value", 0) or 0),
            one_time_per_user=bool(body.get("one_time_per_user", False)),
            is_active=bool(body.get("is_active", True)),
        )
        return ok({"coupon": _serialize_admin_coupon(c)})
    return ok({"coupons": [
        _serialize_admin_coupon(c) for c in Coupon.objects.order_by("code")]})


@csrf_exempt
@admin_required
def admin_coupon_detail(request, user, coupon_id):
    c = Coupon.objects.filter(id=coupon_id).first()
    if c is None:
        return fail("Coupon not found.", status=404)
    if request.method == "DELETE":
        c.delete()
        return ok({"deleted": True})
    if request.method != "POST":
        return fail("POST or DELETE only.", status=405)
    body = json_body(request)
    if "is_active" in body:
        c.is_active = bool(body.get("is_active"))
    if "discount_value" in body:
        try:
            c.discount_value = float(body.get("discount_value"))
        except (TypeError, ValueError):
            return fail("Enter a valid discount value.")
    if "minimum_order_value" in body:
        try:
            c.minimum_order_value = float(body.get("minimum_order_value"))
        except (TypeError, ValueError):
            pass
    if "one_time_per_user" in body:
        c.one_time_per_user = bool(body.get("one_time_per_user"))
    if "offer_text" in body:
        c.offer_text = str(body.get("offer_text", "")).strip()[:200]
    c.save()
    return ok({"coupon": _serialize_admin_coupon(c)})


@csrf_exempt
@admin_required
def admin_notices(request, user):
    """GET list; POST create a feed notice (broadcast push included)."""
    from myapp.models import Notice
    if request.method == "POST":
        body = json_body(request)
        title = str(body.get("title", "")).strip()
        if not title:
            return fail("Title is required.")
        n = Notice.objects.create(
            title=title[:200],
            message=str(body.get("message", "")).strip(),
            is_active=bool(body.get("is_active", True)),
        )
        try:
            from myapp.models import DeviceToken
            tokens = list(DeviceToken.objects.values_list("token", flat=True))
            if tokens:
                _push_tokens(tokens, f"📢 {n.title}",
                             (n.message or "New update on the CUnnect Feed")[:180],
                             route="feed")
        except Exception:
            pass
        return ok({"id": n.id})
    from myapp.models import Notice
    rows = [{
        "id": n.id,
        "title": n.title,
        "message": n.message,
        "is_active": n.is_active,
        "created_at_iso": iso(n.created_at),
    } for n in Notice.objects.order_by("-created_at")[:60]]
    return ok({"notices": rows})


@csrf_exempt
@admin_required
def admin_notice_detail(request, user, notice_id):
    from myapp.models import Notice
    n = Notice.objects.filter(id=notice_id).first()
    if n is None:
        return fail("Notice not found.", status=404)
    if request.method == "DELETE":
        n.delete()
        return ok({"deleted": True})
    if request.method != "POST":
        return fail("POST or DELETE only.", status=405)
    body = json_body(request)
    if "title" in body:
        n.title = str(body.get("title", "")).strip()[:200] or n.title
    if "message" in body:
        n.message = str(body.get("message", "")).strip()
    if "is_active" in body:
        n.is_active = bool(body.get("is_active"))
    n.save()
    return ok({"id": n.id})


@csrf_exempt
def admin_create_superuser(request):
    """⭐ One-time superuser bootstrap — callable from PowerShell so no
    server shell is needed. Protected by the ADMIN_SETUP_KEY environment
    variable: the endpoint is completely disabled unless that env var is
    set on the server, and the caller must send the same key."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    setup_key = os.environ.get("ADMIN_SETUP_KEY", "")
    if not setup_key:
        return fail("Superuser setup is disabled on this server.", status=403)
    # ⭐ v65 security: brutal throttle + constant-time compare (no timing leak)
    if not _rl_allow(f"setupkey:{client_ip(request)}", 5, 3600):
        return fail("Too many attempts.", status=429)
    import hmac
    body = json_body(request)
    if not hmac.compare_digest(str(body.get("setup_key", "")), setup_key):
        return fail("Invalid setup key.", status=403)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username or len(password) < 8:
        return fail("Provide a username and a password of 8+ characters.")
    existing = User.objects.filter(username=username).first()
    if existing:
        existing.set_password(password)
        existing.is_staff = True
        existing.is_superuser = True
        existing.is_active = True
        existing.save()
        return ok({"created": False, "updated": True, "username": username})
    User.objects.create_superuser(username=username, password=password)
    return ok({"created": True, "username": username})


VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm", ".3gp", ".mkv", ".avi")


@csrf_exempt
@admin_required
def admin_feed_post(request, user):
    """⭐ Flexible feed composer (multipart) — post ANY combination of:
    text (title/message), media (image or video, kept in its original
    aspect ratio) and a poll (question + options). If a poll is included
    the post is stored as an AppPoll (media shows above the options);
    otherwise it is a Notice."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    from myapp.models import Notice, AppPoll, AppPollOption, DeviceToken

    title = str(request.POST.get("title", "")).strip()[:200]
    message = str(request.POST.get("message", "")).strip()
    question = str(request.POST.get("question", "")).strip()[:240]
    options_raw = str(request.POST.get("options", "")).strip()
    media = request.FILES.get("media")
    if media is not None:
        upload_error = check_upload(media, kind="media", max_mb=60)
        if upload_error:
            return fail(upload_error)

    options = []
    if options_raw:
        try:
            options = [str(o).strip()[:160]
                       for o in json.loads(options_raw) if str(o).strip()]
        except Exception:
            options = [o.strip()[:160]
                       for o in options_raw.split("\n") if o.strip()]

    if not title and not message and not question and media is None:
        return fail("Add some text, media or a poll before posting.")
    if question and len(options) < 2:
        return fail("A poll needs at least 2 options.")

    is_video = False
    if media is not None:
        name = (media.name or "").lower()
        ctype = (getattr(media, "content_type", "") or "").lower()
        is_video = (name.endswith(VIDEO_EXTENSIONS)
                    or ctype.startswith("video/"))

    if question:
        poll = AppPoll.objects.create(
            question=question,
            image=None if (media is None or is_video) else media,
            video=media if (media is not None and is_video) else None,
        )
        for text in options[:10]:
            AppPollOption.objects.create(poll=poll, text=text)
        push_title = "🗳️ New poll"
        push_body = question[:180]
        created = {"kind": "poll", "id": poll.id}
    else:
        notice = Notice.objects.create(
            title=title,
            message=message,
            image=None if (media is None or is_video) else media,
            video=media if (media is not None and is_video) else None,
        )
        push_title = f"📢 {title}" if title else "📢 CUnnect Feed"
        push_body = (message or "New update on the CUnnect Feed")[:180]
        created = {"kind": "notice", "id": notice.id}

    try:
        tokens = list(DeviceToken.objects.values_list("token", flat=True))
        if tokens:
            _push_tokens(tokens, push_title, push_body, route="feed")
    except Exception:
        pass
    return ok(created)


@admin_required
def admin_feed_list(request, user):
    """Combined feed (notices + polls, pinned first) for the admin panel."""
    from myapp.models import Notice, AppPoll

    rows = []
    for n in Notice.objects.all()[:100]:
        rows.append({
            "kind": "notice",
            "id": n.id,
            "title": n.title or (n.message[:60] if n.message else "Media post"),
            "pinned": n.pinned_at is not None,
            "has_media": bool(n.image or n.video),
            "created_at_iso": iso(n.created_at),
        })
    for p in AppPoll.objects.all()[:100]:
        rows.append({
            "kind": "poll",
            "id": p.id,
            "title": p.question,
            "pinned": p.pinned_at is not None,
            "has_media": bool(p.image or p.video),
            "created_at_iso": iso(p.created_at),
        })
    rows.sort(key=lambda r: (not r["pinned"], r["created_at_iso"]),
              reverse=False)
    rows.sort(key=lambda r: r["created_at_iso"], reverse=True)
    rows.sort(key=lambda r: not r["pinned"])
    pinned_count = sum(1 for r in rows if r["pinned"])
    return ok({"posts": rows[:150], "pinned_count": pinned_count,
               "max_pinned": MAX_PINNED_POSTS})


@csrf_exempt
@admin_required
def admin_feed_pin(request, user, kind, object_id):
    """POST {pinned: true|false} — pin/unpin a feed post (max 15 pinned
    across notices + polls, WhatsApp-group style)."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    if kind not in ("notice", "poll"):
        return fail("Unknown kind.", status=404)
    from myapp.models import Notice, AppPoll

    model = Notice if kind == "notice" else AppPoll
    obj = model.objects.filter(id=object_id).first()
    if obj is None:
        return fail("Post not found.", status=404)
    want_pin = bool(json_body(request).get("pinned", True))
    if want_pin and obj.pinned_at is None:
        pinned_total = (
            Notice.objects.filter(pinned_at__isnull=False).count()
            + AppPoll.objects.filter(pinned_at__isnull=False).count())
        if pinned_total >= MAX_PINNED_POSTS:
            return fail(
                f"You can pin up to {MAX_PINNED_POSTS} posts. "
                "Unpin something first.")
        obj.pinned_at = timezone.now()
    elif not want_pin:
        obj.pinned_at = None
    obj.save(update_fields=["pinned_at"])
    return ok({"pinned": obj.pinned_at is not None})


@csrf_exempt
@admin_required
def admin_feed_delete(request, user, kind, object_id):
    """POST — delete any feed post (notice or poll)."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    if kind not in ("notice", "poll"):
        return fail("Unknown kind.", status=404)
    from myapp.models import Notice, AppPoll

    model = Notice if kind == "notice" else AppPoll
    deleted, _ = model.objects.filter(id=object_id).delete()
    if not deleted:
        return fail("Post not found.", status=404)
    return ok({"deleted": True})


@csrf_exempt
@admin_required
def admin_support(request, user):
    """GET support requests; POST {id, status} to update."""
    if request.method == "POST":
        body = json_body(request)
        req = SupportRequest.objects.filter(id=body.get("id")).first()
        if req is None:
            return fail("Request not found.", status=404)
        status_new = str(body.get("status", "")).strip()
        if status_new in ("pending", "in_progress", "resolved"):
            req.status = status_new
            req.save(update_fields=["status", "updated_at"])
        return ok({"status": req.status})
    rows = [{
        "id": r.id,
        "email": r.email,
        "subject": r.subject,
        "message": r.message,
        "status": r.status,
        "user": r.user.username,
        "created_at_iso": iso(r.created_at),
    } for r in SupportRequest.objects.select_related(
        "user").order_by("-created_at")[:100]]
    return ok({"requests": rows})


_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi", ".3gp")


def _is_video_file(f):
    """True when the uploaded file looks like a video (name or MIME)."""
    if f is None:
        return False
    name = (getattr(f, "name", "") or "").lower()
    ctype = (getattr(f, "content_type", "") or "").lower()
    return ctype.startswith("video/") or name.endswith(_VIDEO_EXTS)


@csrf_exempt
@admin_required
def admin_banners(request, user):
    """⭐ v61: GET list / POST create — student home banners.
    Creating needs ONLY a media file (photo OR video) + a position:
    the file arrives as 'media' (or legacy 'image'/'video') and is
    routed to the right field automatically."""
    from myapp.models import Banner
    if request.method == "POST":
        f = (request.FILES.get("media") or request.FILES.get("image")
             or request.FILES.get("video"))
        if f is None:
            return fail("Pick a photo or a video for the banner.")
        upload_error = check_upload(f, kind="media", max_mb=60)
        if upload_error:
            return fail(upload_error)
        try:
            order = int(str(request.POST.get("order", "")).strip() or 0)
        except ValueError:
            order = 0
        if order < 1:
            return fail("Enter the banner position (1 onwards).")
        is_video = _is_video_file(f)
        b = Banner.objects.create(
            title=str(request.POST.get("title", "")).strip()[:200]
            or f"Banner {order}",
            image=None if is_video else f,
            video=f if is_video else None,
            is_active=True,
            order=order,
        )
        return ok({"id": b.id})
    rows = [{
        "id": b.id,
        "title": b.title,
        "subtitle": b.subtitle or "",
        "image_url": media_url(b.image),
        "video_url": media_url(b.video),
        "is_video": bool(b.video),
        "is_active": b.is_active,
        "order": b.order,
    } for b in Banner.objects.order_by("order", "-id")]
    return ok({"banners": rows})


@csrf_exempt
@admin_required
def admin_banner_detail(request, user, banner_id):
    """⭐ v58: POST update (multipart, partial) / DELETE remove a banner."""
    from myapp.models import Banner
    b = Banner.objects.filter(id=banner_id).first()
    if b is None:
        return fail("Banner not found.", status=404)
    if request.method == "DELETE":
        if b.image:
            b.image.delete(save=False)
        if b.video:
            b.video.delete(save=False)
        b.delete()
        return ok({"deleted": True})
    if request.method != "POST":
        return fail("POST or DELETE only.", status=405)
    if "title" in request.POST:
        b.title = str(request.POST.get("title", "")).strip()[:200] or b.title
    if "subtitle" in request.POST:
        b.subtitle = str(request.POST.get("subtitle", "")).strip() or None
    if "is_active" in request.POST:
        b.is_active = str(request.POST.get(
            "is_active", "1")).strip() not in ("0", "false", "False")
    if "order" in request.POST:
        try:
            b.order = int(str(request.POST.get("order", "0")).strip() or 0)
        except ValueError:
            pass
    # ⭐ v61: replacing media accepts photo OR video and clears the other.
    f = (request.FILES.get("media") or request.FILES.get("image")
         or request.FILES.get("video"))
    if f is not None:
        if b.image:
            b.image.delete(save=False)
        if b.video:
            b.video.delete(save=False)
        if _is_video_file(f):
            b.image = None
            b.video = f
        else:
            b.video = None
            b.image = f
    b.save()
    return ok({"id": b.id})


@csrf_exempt
@admin_required
def admin_support_delete(request, user, request_id):
    """⭐ v58: permanently delete a support request."""
    if request.method not in ("POST", "DELETE"):
        return fail("POST or DELETE only.", status=405)
    req = SupportRequest.objects.filter(id=request_id).first()
    if req is None:
        return fail("Request not found.", status=404)
    req.delete()
    return ok({"deleted": True})


@csrf_exempt
@admin_required
def admin_broadcast(request, user):
    """POST {title, message} — push notification to every device."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    body = json_body(request)
    title = str(body.get("title", "")).strip()
    message = str(body.get("message", "")).strip()
    if not title or not message:
        return fail("Both title and message are required.")
    from myapp.models import DeviceToken
    tokens = list(DeviceToken.objects.values_list("token", flat=True))
    if not tokens:
        return fail("No registered devices found.")
    # ⭐ v73: broadcasts carry their own route so the app can open the
    # message as a full-screen card on the home page.
    _push_tokens(tokens, title, message, route="broadcast")
    return ok({"sent_to": len(tokens)})


# ---------------------------------------------------------------------
# ⭐ Admin Portal 2.0 (v50): stats, store sections, vendor portal access,
# support acknowledge.
# ---------------------------------------------------------------------


def _range_buckets(period):
    """Return (start_date, bucket) for daily/weekly/monthly up to 1 year."""
    today = timezone.localdate()
    if period == "weekly":
        return today - timedelta(days=7 * 12), "weekly"    # 12 weeks
    if period == "monthly":
        return today - timedelta(days=365), "monthly"      # 12 months
    return today - timedelta(days=30), "daily"             # 30 days


@admin_required
def admin_stats(request, user):
    """⭐ Home-page stats: active users, impressions, unique traffic,
    transactions and revenue (total + by source) for daily / weekly /
    monthly windows up to 1 year. ?period=daily|weekly|monthly"""
    from myapp.models import TrafficStat, DailyVisit, HostelOrder, PrintOrder
    from django.db.models import Sum, Count

    period = request.GET.get("period", "daily")
    start, bucket = _range_buckets(period)
    today = timezone.localdate()

    # ---- live + today's headline numbers ----
    now = time.time()
    live = sum(1 for t in _LAST_SEEN.values()
               if now - t <= LIVE_WINDOW_SECONDS)
    imp_today = (TrafficStat.objects.filter(date=today)
                 .aggregate(s=Sum("impressions"))["s"] or 0)
    imp_today += _IMPRESSION_BUFFER["count"]
    visits_today = DailyVisit.objects.filter(date=today).count()

    # ---- revenue TODAY by source ----
    def _sum(qs, field):
        return float(qs.aggregate(s=Sum(field))["s"] or 0)

    food_done = Order.objects.filter(
        status__in=["completed", "out_for_delivery", "ready", "preparing",
                    "accepted"])
    print_done = PrintOrder.objects.filter(
        status__in=["accepted", "printing", "ready", "completed"])
    hostel_done = HostelOrder.objects.filter(
        status__in=["accepted", "delivered"])

    def day_filter(qs):
        return qs.filter(created_at__date=today)

    rev_today = {
        "food": _sum(day_filter(food_done), "total_amount"),
        "print": _sum(day_filter(print_done), "final_amount"),
        "hostel": _sum(day_filter(hostel_done), "total"),
    }
    txn_today = (day_filter(food_done).count()
                 + day_filter(print_done).count()
                 + day_filter(hostel_done).count())

    # ---- time series (impressions, traffic, revenue by source) ----
    imps = {r["date"]: r["s"] for r in
            TrafficStat.objects.filter(date__gte=start)
            .values("date").annotate(s=Sum("impressions"))}
    visits = {r["date"]: r["c"] for r in
              DailyVisit.objects.filter(date__gte=start)
              .values("date").annotate(c=Count("id"))}

    def series_by_day(qs, field):
        return {r["d"]: float(r["s"] or 0) for r in
                qs.filter(created_at__date__gte=start)
                .extra(select={"d": "date(created_at)"})
                .values("d").annotate(s=Sum(field))}

    rev_food = series_by_day(food_done, "total_amount")
    rev_print = series_by_day(print_done, "final_amount")
    rev_hostel = series_by_day(hostel_done, "total")

    def norm_key(k):
        return k.isoformat() if hasattr(k, "isoformat") else str(k)

    rev_food = {norm_key(k): v for k, v in rev_food.items()}
    rev_print = {norm_key(k): v for k, v in rev_print.items()}
    rev_hostel = {norm_key(k): v for k, v in rev_hostel.items()}
    imps = {norm_key(k): v for k, v in imps.items()}
    visits = {norm_key(k): v for k, v in visits.items()}

    # bucket the days into daily / weekly / monthly points
    points = []
    day = start
    acc = None
    while day <= today:
        key = day.isoformat()
        if bucket == "daily":
            label = day.strftime("%d %b")
            points.append({
                "label": label,
                "impressions": int(imps.get(key, 0)),
                "traffic": int(visits.get(key, 0)),
                "food": rev_food.get(key, 0.0),
                "print": rev_print.get(key, 0.0),
                "hostel": rev_hostel.get(key, 0.0),
            })
        else:
            if bucket == "weekly":
                blabel = f"W{day.isocalendar()[1]}"
            else:
                blabel = day.strftime("%b %y")
            if acc is None or acc["label"] != blabel:
                acc = {"label": blabel, "impressions": 0, "traffic": 0,
                       "food": 0.0, "print": 0.0, "hostel": 0.0}
                points.append(acc)
            acc["impressions"] += int(imps.get(key, 0))
            acc["traffic"] += int(visits.get(key, 0))
            acc["food"] += rev_food.get(key, 0.0)
            acc["print"] += rev_print.get(key, 0.0)
            acc["hostel"] += rev_hostel.get(key, 0.0)
        day += timedelta(days=1)

    for p in points:
        p["revenue"] = round(p["food"] + p["print"] + p["hostel"], 2)
        p["food"] = round(p["food"], 2)
        p["print"] = round(p["print"], 2)
        p["hostel"] = round(p["hostel"], 2)

    return ok({
        "period": bucket,
        "live_users": live,
        "impressions_today": int(imp_today),
        "traffic_today": visits_today,
        "transactions_today": txn_today,
        "revenue_today": round(sum(rev_today.values()), 2),
        "revenue_today_by_source": {
            k: round(v, 2) for k, v in rev_today.items()},
        "points": points[-60:],
        "totals": {
            "impressions": sum(p["impressions"] for p in points),
            "traffic": sum(p["traffic"] for p in points),
            "revenue": round(sum(p["revenue"] for p in points), 2),
            "food": round(sum(p["food"] for p in points), 2),
            "print": round(sum(p["print"] for p in points), 2),
            "hostel": round(sum(p["hostel"] for p in points), 2),
        },
    })


# ⭐ v60: built-in store sections are now DB rows too, so the admin
# portal has full control (title/subtitle/icon/hide/coming-soon) over
# CUnnect Food, Printout Services and Hostel Essentials as well.
BUILTIN_SECTIONS = [
    ("food", "🍔", "CUnnect Food",
     "Order from campus food partners."),
    ("printout", "🖨", "Printout Services",
     "PDF print, color print, photocopy, binding and lamination."),
    ("hostel", "🛏", "Hostel Essentials",
     "Everything your hostel room needs, delivered on campus."),
]
BUILTIN_KEYS = {k for k, _, _, _ in BUILTIN_SECTIONS}


def _ensure_builtin_sections():
    """Creates the built-in StoreSection rows once (idempotent)."""
    from myapp.models import StoreSection
    for i, (key, icon, title, subtitle) in enumerate(BUILTIN_SECTIONS):
        StoreSection.objects.get_or_create(
            key=key,
            defaults={"title": title, "subtitle": subtitle,
                      "icon": icon, "order": i})


def _serialize_section(sec):
    return {
        "id": sec.id,
        "key": sec.key,
        "title": sec.title,
        "subtitle": sec.subtitle,
        "icon": sec.icon,
        "is_active": sec.is_active,
        "coming_soon": sec.coming_soon,
        "order": sec.order,
        "builtin": sec.key in BUILTIN_KEYS,
    }


@student_required
def store_sections(request, user):
    """Public: store sections shown on the CUnnect Store page.
    'sections' keeps the old shape (custom + active only) so older app
    versions render unchanged; 'builtin' carries the admin-controlled
    flags for the built-in categories (v60+ clients use it)."""
    from myapp.models import StoreSection
    _ensure_builtin_sections()
    all_secs = list(StoreSection.objects.all())
    return ok({
        "sections": [
            _serialize_section(s) for s in all_secs
            if s.is_active and s.key not in BUILTIN_KEYS],
        "builtin": {
            s.key: _serialize_section(s)
            for s in all_secs if s.key in BUILTIN_KEYS},
    })


@csrf_exempt
@admin_required
def admin_store_sections(request, user):
    """GET list (built-in + custom); POST create a custom store section."""
    from myapp.models import StoreSection
    _ensure_builtin_sections()
    if request.method == "POST":
        body = json_body(request)
        title = str(body.get("title", "")).strip()[:80]
        if not title:
            return fail("Section title is required.")
        key = str(body.get("key", "")).strip().lower()[:30]
        if not key:
            import re as _re
            key = _re.sub(r"-+", "-", "".join(
                c if c.isalnum() else "-"
                for c in title.lower())).strip("-")[:30]
        if StoreSection.objects.filter(key=key).exists():
            return fail("A section with this key already exists.")
        sec = StoreSection.objects.create(
            key=key,
            title=title,
            subtitle=str(body.get("subtitle", "")).strip()[:200],
            icon=str(body.get("icon", "🛍")).strip()[:8] or "🛍",
            coming_soon=bool(body.get("coming_soon", False)),
        )
        return ok({"section": _serialize_section(sec)})
    return ok({"sections": [
        _serialize_section(s) for s in StoreSection.objects.all()]})


@csrf_exempt
@admin_required
def admin_store_section_detail(request, user, section_id):
    """POST update; DELETE remove a custom store section."""
    from myapp.models import StoreSection
    sec = StoreSection.objects.filter(id=section_id).first()
    if sec is None:
        return fail("Section not found.", status=404)
    if request.method == "DELETE":
        # ⭐ v60: built-ins cannot be deleted (the whole app links to them)
        # — hide them with the is_active toggle instead.
        if sec.key in BUILTIN_KEYS:
            return fail("Built-in sections cannot be deleted — "
                        "switch them off to hide them from the store.")
        sec.delete()
        return ok({"deleted": True})
    if request.method != "POST":
        return fail("POST or DELETE only.", status=405)
    body = json_body(request)
    if "title" in body:
        sec.title = str(body.get("title", "")).strip()[:80] or sec.title
    if "subtitle" in body:
        sec.subtitle = str(body.get("subtitle", "")).strip()[:200]
    if "icon" in body:
        sec.icon = str(body.get("icon", "")).strip()[:8] or sec.icon
    if "is_active" in body:
        sec.is_active = bool(body.get("is_active"))
    if "coming_soon" in body:
        sec.coming_soon = bool(body.get("coming_soon"))
    sec.save()
    return ok({"section": _serialize_section(sec)})


# ---- ⭐ v61: hostel product CRUD core, shared by the vendor portal and
# the admin (admin opens the vendor portal to manage the store) --------

def _hostel_products_handler(request):
    """GET list / POST create a hostel product."""
    from myapp.models import HostelProduct
    _ensure_hostel_products()
    if request.method == "POST":
        body = json_body(request)
        name = str(body.get("name", "")).strip()[:120]
        if not name:
            return fail("Product name is required.")
        try:
            mrp = round(float(body.get("mrp", 0)), 2)
        except (TypeError, ValueError):
            return fail("Enter a valid MRP.")
        if mrp <= 0:
            return fail("MRP must be greater than zero.")
        try:
            stock = max(0, int(body.get("stock", 0) or 0))
        except (TypeError, ValueError):
            stock = 0
        p = HostelProduct.objects.create(
            name=name,
            mrp=mrp,
            description=str(body.get("description", "")).strip()[:2000],
            emoji=str(body.get("emoji", "🛒")).strip()[:8] or "🛒",
            stock=stock,
            order=int(body.get("order", 100) or 100),
        )
        return ok({"product": _serialize_hostel_product(p)})
    return ok({"products": [
        _serialize_hostel_product(p)
        for p in HostelProduct.objects.all().prefetch_related("photos")]})


def _hostel_product_detail_handler(request, product_id):
    """POST update / DELETE remove a hostel product."""
    from myapp.models import HostelProduct
    p = HostelProduct.objects.filter(id=product_id).first()
    if p is None:
        return fail("Product not found.", status=404)
    if request.method == "DELETE":
        p.delete()
        return ok({"deleted": True})
    if request.method != "POST":
        return fail("POST or DELETE only.", status=405)
    body = json_body(request)
    if "name" in body:
        p.name = str(body.get("name", "")).strip()[:120] or p.name
    if "mrp" in body:
        try:
            mrp = round(float(body.get("mrp")), 2)
            if mrp > 0:
                p.mrp = mrp
        except (TypeError, ValueError):
            pass
    if "description" in body:
        p.description = str(body.get("description", "")).strip()[:2000]
    if "emoji" in body:
        p.emoji = str(body.get("emoji", "")).strip()[:8] or p.emoji
    if "is_active" in body:
        p.is_active = bool(body.get("is_active"))
    if "stock" in body:
        try:
            p.stock = max(0, int(body.get("stock") or 0))
            # restocking flips the item back on if it auto-closed at 0
            if p.stock > 0 and not p.is_active and "is_active" not in body:
                p.is_active = True
        except (TypeError, ValueError):
            pass
    if "order" in body:
        try:
            p.order = int(body.get("order"))
        except (TypeError, ValueError):
            pass
    p.save()
    return ok({"product": _serialize_hostel_product(p)})


def _hostel_product_photo_handler(request, product_id):
    """POST multipart 'image' adds a photo; DELETE ?photo_id=N."""
    from myapp.models import HostelProduct, HostelProductPhoto
    p = HostelProduct.objects.filter(id=product_id).first()
    if p is None:
        return fail("Product not found.", status=404)
    if request.method == "DELETE":
        photo_id = request.GET.get("photo_id", "")
        ph = HostelProductPhoto.objects.filter(
            id=photo_id or 0, product=p).first()
        if ph is None:
            return fail("Photo not found.", status=404)
        ph.delete()
        return ok({"deleted": True})
    if request.method != "POST":
        return fail("POST or DELETE only.", status=405)
    f = request.FILES.get("image") or request.FILES.get("file")
    if f is None:
        return fail("No image file sent.")
    upload_error = check_upload(f, kind="image", max_mb=8)
    if upload_error:
        return fail(upload_error)
    ph = HostelProductPhoto.objects.create(product=p, image=f)
    return ok({"photo": {"id": ph.id, "url": media_url(ph.image)},
               "product": _serialize_hostel_product(p)})


def _require_hostel_vendor(request):
    """Vendor-token auth; only the hostel vendor may manage the catalogue."""
    _, profile = vendor_user(request)
    if profile is None:
        return None, fail("Vendor login required.", status=401)
    if profile.vendor_type != "hostel":
        return None, fail(
            "Only the Hostel Essentials partner can manage these products.",
            status=403)
    return profile, None


@csrf_exempt
def vendor_hostel_products(request):
    """⭐ v61: hostel VENDOR manages their own catalogue from the portal."""
    _, err = _require_hostel_vendor(request)
    if err is not None:
        return err
    return _hostel_products_handler(request)


@csrf_exempt
def vendor_hostel_product_detail(request, product_id):
    _, err = _require_hostel_vendor(request)
    if err is not None:
        return err
    return _hostel_product_detail_handler(request, product_id)


@csrf_exempt
def vendor_hostel_product_photo(request, product_id):
    _, err = _require_hostel_vendor(request)
    if err is not None:
        return err
    return _hostel_product_photo_handler(request, product_id)


@csrf_exempt
def vendor_store_settings(request):
    """⭐ v61: any vendor edits their storefront content (name shown on
    the store card + description shown at the top of their store page)."""
    _, profile = vendor_user(request)
    if profile is None:
        return fail("Vendor login required.", status=401)
    if request.method == "POST":
        body = json_body(request)
        if "store_description" in body:
            profile.store_description = str(
                body.get("store_description", "")).strip()[:2000]
        if "business_name" in body:
            name = str(body.get("business_name", "")).strip()[:150]
            if name:
                profile.business_name = name
        profile.save()
    return ok({
        "business_name": profile.business_name,
        "store_description": profile.store_description,
        "open": profile.kitchen_open,
    })


# ---- admin equivalents (kept so the admin API also works directly) ----

@csrf_exempt
@admin_required
def admin_hostel_products(request, user):
    """⭐ v60: GET list / POST create a hostel product (admin portal)."""
    return _hostel_products_handler(request)


@csrf_exempt
@admin_required
def admin_hostel_product_detail(request, user, product_id):
    """⭐ v60: POST update / DELETE remove a hostel product."""
    return _hostel_product_detail_handler(request, product_id)


@csrf_exempt
@admin_required
def admin_hostel_product_photo(request, user, product_id):
    """⭐ v60: POST multipart 'image' adds a photo; DELETE ?photo_id=N."""
    return _hostel_product_photo_handler(request, product_id)


@csrf_exempt
@admin_required
def admin_vendor_portal(request, user, vendor_id):
    """⭐ Full vendor-portal access for the admin: returns the vendor's own
    session token + profile, so the admin's app can open the EXACT vendor
    dashboard (every feature identical) inside the admin portal."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    v = VendorProfile.objects.select_related("user").filter(
        id=vendor_id).first()
    if v is None:
        return fail("Vendor not found.", status=404)
    token, _ = Token.objects.get_or_create(user=v.user)
    return ok({
        "token": token.key,
        "vendor_id": v.id,
        "business_name": v.business_name,
        "vendor_type": v.vendor_type,
        "phone": v.phone or "",
        "owner_username": v.user.username,
        "email": v.user.email or "",
    })


@csrf_exempt
@admin_required
def admin_support_ack(request, user, request_id):
    """⭐ Acknowledge a support request — pushes 'your support request has
    been acknowledged by the CUnnect team' to the requester's devices."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    req = SupportRequest.objects.select_related("user").filter(
        id=request_id).first()
    if req is None:
        return fail("Request not found.", status=404)
    if req.status == "pending":
        req.status = "in_progress"
        req.save(update_fields=["status", "updated_at"])
    try:
        from myapp.models import DeviceToken
        tokens = list(DeviceToken.objects.filter(
            user=req.user).values_list("token", flat=True))
        if tokens:
            _push_tokens(
                tokens, "CUnnect Support",
                "Your support request has been acknowledged by the "
                "CUnnect team.")
    except Exception:
        pass
    try:
        _notify(
            user=req.user,
            title="Support request acknowledged",
            message="Your support request has been acknowledged by the "
                    "CUnnect team.",
        )
    except Exception:
        pass
    return ok({"acknowledged": True, "status": req.status})


@csrf_exempt
@admin_required
def admin_vendor_qr(request, user, vendor_id):
    """⭐ v50: Admin uploads/removes a vendor's UPI payment QR directly
    from the admin portal (same image the students see at checkout)."""
    if request.method != "POST":
        return fail("POST only.", status=405)
    try:
        v = VendorProfile.objects.get(id=vendor_id)
    except VendorProfile.DoesNotExist:
        return fail("Vendor not found.", status=404)
    remove_flag = str(request.POST.get("remove", "")).strip()
    if not remove_flag and not request.FILES:
        remove_flag = str(json_body(request).get("remove", "")).strip()
    if remove_flag == "1":
        if v.upi_qr_image:
            v.upi_qr_image.delete(save=False)
        v.upi_qr_image = None
        v.save()
        return ok({"qr_url": ""})
    f = request.FILES.get("file")
    if f is None:
        return fail("No image file sent.")
    v.upi_qr_image = f
    v.save()
    return ok({"qr_url": v.upi_qr_image.url})


# ---------------------------------------------------------------------
# ⭐ v66: CUnnect RIDE — student booking + rider partner portal.
#
# Flow: estimate -> book -> rider alert -> accept (fare locks to that
# rider's own rate) -> student pays (full | 50-50 with +5%) -> rider
# arrives ("I'm on location") -> OTP to student -> ride starts ->
# rider completes -> student notified.
# ---------------------------------------------------------------------

def _ride_haversine_km(lat1, lng1, lat2, lng2):
    """Straight-line km, scaled to approximate the real road distance."""
    import math

    try:
        lat1, lng1, lat2, lng2 = (float(lat1), float(lng1),
                                  float(lat2), float(lng2))
    except (TypeError, ValueError):
        return 0.0
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lng / 2) ** 2)
    km = radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(km * 1.25, 2)          # ⭐ road factor


def _ride_fare(base, per_km, distance_km):
    """base + per_km x distance (never below the base fare)."""
    try:
        base = float(base)
        per_km = float(per_km)
        distance_km = float(distance_km or 0)
    except (TypeError, ValueError):
        return 0.0
    fare = base + per_km * distance_km
    if fare < base:
        fare = base
    return round(fare, 2)


def _ride_rate_pool(vehicle_type):
    """Ride partners offering this vehicle — online ones first."""
    from ride.models import RideVendor

    field = f"{vehicle_type}_active"
    if vehicle_type not in ("auto", "mini", "sedan", "suv", "xl"):
        return []
    qs = RideVendor.objects.filter(**{field: True}).select_related("vendor")
    pool = list(qs)
    online = [v for v in pool if v.is_online]
    return online or pool


def _ride_estimate_for(vehicle_type, distance_km):
    """Cheapest live rate for a vehicle type (platform default fallback)."""
    from ride.models import (VEHICLE_DEFAULT_BASE, VEHICLE_DEFAULT_PER_KM,
                             VEHICLE_ICON, VEHICLE_LABEL, VEHICLE_SEATS)

    pool = _ride_rate_pool(vehicle_type)
    best = None
    for rv in pool:
        _active, base, per_km = rv.rate_for(vehicle_type)
        fare = _ride_fare(base, per_km, distance_km)
        if best is None or fare < best:
            best = fare
    if best is None:
        best = _ride_fare(VEHICLE_DEFAULT_BASE.get(vehicle_type, 30),
                          VEHICLE_DEFAULT_PER_KM.get(vehicle_type, 14),
                          distance_km)
    return {
        "key": vehicle_type,
        "label": VEHICLE_LABEL.get(vehicle_type, vehicle_type.title()),
        "icon": VEHICLE_ICON.get(vehicle_type, "🚗"),
        "seats": VEHICLE_SEATS.get(vehicle_type, 4),
        "fare": round(best, 2),
        "available": True,
    }


def _ride_parse_when(raw):
    """Parse an ISO datetime from the app ('' when not given)."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        from django.utils.dateparse import parse_datetime

        return parse_datetime(raw)
    except Exception:
        return None


def _ride_profile_phone(user):
    """The number stored on the student's own profile."""
    try:
        from myapp.models import UserProfile

        prof = UserProfile.objects.filter(user_id=user.id).first()
        return (prof.phone if prof and prof.phone else "")[:20]
    except Exception:
        return ""


def _ride_contact_phone(user, body):
    """Who should the rider call? The student, or the "someone else".

    The student's own number is NEVER taken from the phone — it always
    comes from the profile/database.
    """
    own = _ride_profile_phone(user)
    if not body.get("booking_for_other"):
        return own
    other = str(body.get("other_phone", "")).strip()
    digits = "".join(ch for ch in other if ch.isdigit())
    if len(digits) < 8:
        return own
    return digits[-12:]


def _ride_has_free_rider(vehicle_type, when):
    """Is at least one online partner for this vehicle free at `when`?"""
    from ride.models import RideVendor, rider_is_blocked

    field = f"{vehicle_type}_active"
    if vehicle_type not in ("mini", "sedan", "suv", "xl"):
        return False
    riders = RideVendor.objects.filter(**{field: True}, is_online=True)
    return any(not rider_is_blocked(rv, when) for rv in riders)


def _ride_split_amounts(fare):
    """full  -> pay the fare, nothing later.
    split  -> +5% add-on, half now, rest after the ride ends."""
    fare = round(float(fare or 0), 2)
    total_split = round(fare * 1.05, 2)
    first = round(total_split / 2, 2)
    return {
        "fare": fare,
        "full_total": fare,
        "split_total": total_split,
        "split_fee": round(total_split - fare, 2),
        "split_now": first,
        "split_later": round(total_split - first, 2),
    }


def _mask_phone(value):
    """⭐ v77: 9876543210 -> 98XXXXX210 (shown on screen; the real number
    only ever reaches the phone's dialer through the CALL button)."""
    p = (value or "").strip()
    if len(p) < 6:
        return p
    return f"{p[:2]}{'X' * (len(p) - 5)}{p[-3:]}"


def _ride_public(ride, viewer="student"):
    """Serialize a ride.

    ⭐ privacy: the student's phone stays hidden from the rider until
    the ride is accepted (same rule as the print vendor portal).
    """
    from ride.models import VEHICLE_ICON, VEHICLE_LABEL

    rider_phone = ""
    rider_name = ""
    vendor_id = None
    if ride.rider_id:
        rider_name = ride.rider.vendor.business_name
        vendor_id = ride.rider.vendor_id
        rider_phone = ride.rider.vendor.phone or ""

    # ⭐ v77: NOTHING is shared before the rider accepts AND verifies the
    # payment. After that:
    #   * the RIDER may call the student — but only a MASKED number is
    #     ever painted on screen (the digits go to the dialer only)
    #   * the STUDENT sees the rider's number, car model and plate
    confirmed = bool(ride.payment_confirmed)
    is_rider = viewer == "rider"
    phone_visible = (not is_rider) or confirmed      # student's -> rider
    rider_phone_visible = is_rider or confirmed      # rider's -> student
    plate_visible = is_rider or confirmed            # plate -> student
    return {
        "ride_code": ride.ride_code,
        "status": ride.status,
        "vehicle_type": ride.vehicle_type,
        "vehicle_label": VEHICLE_LABEL.get(ride.vehicle_type,
                                           ride.vehicle_type.title()),
        "vehicle_icon": VEHICLE_ICON.get(ride.vehicle_type, "🚗"),
        "pickup_text": ride.pickup_text,
        "pickup_lat": ride.pickup_lat,
        "pickup_lng": ride.pickup_lng,
        "drop_text": ride.drop_text,
        "drop_lat": ride.drop_lat,
        "drop_lng": ride.drop_lng,
        "distance_km": ride.distance_km,
        "scheduled_at": (ride.scheduled_at.isoformat()
                         if ride.scheduled_at else ""),
        "notes": ride.notes,
        "fare": float(ride.fare or 0),
        "split_fee": float(ride.split_fee or 0),
        "total": float(ride.total or 0),
        "base_fare": float(ride.base_fare or 0),
        "per_km": float(ride.per_km or 0),
        "payment_mode": ride.payment_mode,
        "amount_paid": float(ride.amount_paid or 0),
        "balance_due": float(ride.balance_due or 0),
        "payment_done": ride.payment_done,
        "payment_confirmed": bool(ride.payment_confirmed),
        # ⭐ v78: a 50-50 ride is NOT closed until the second half lands.
        # The STUDENT pays it (never the rider) and the ride completes
        # the moment it does.
        "awaiting_balance": bool(ride.awaiting_balance),
        "can_pay_balance": bool(
            viewer != "rider" and not ride.payment_done
            and float(ride.balance_due or 0) > 0),
        # ⭐ v75: the rider must still confirm the payment himself
        "can_confirm": bool(viewer == "rider" and ride.amount_paid > 0
                            and not ride.payment_confirmed
                            and ride.status in ("paid", "arrived",
                                                "ongoing")),
        "amount_now": float(ride.amount_now() or 0),
        "txn_first": ride.txn_first,
        "txn_second": ride.txn_second,
        "rider_name": rider_name,
        # ⭐ v77: the rider's number reaches the student only after the
        # payment has been verified by the rider.
        "rider_phone": rider_phone if rider_phone_visible else "",
        "rider_phone_masked": _mask_phone(rider_phone),
        "rider_phone_hidden": not rider_phone_visible,
        # ⭐ v77: this ride's car — the model/category is public, the
        # plate appears only after the payment is verified.
        "vehicle_name": ride.vehicle_name or "",
        "vehicle_plate": ride.vehicle_plate if plate_visible else "",
        "plate_hidden": not plate_visible,
        "rider_vehicle": (ride.rider.vehicle_number if ride.rider_id else ""),
        "rider_model": (ride.rider.vehicle_model if ride.rider_id else ""),
        "vendor_id": vendor_id,
        "student_name": ride.student_name,
        # ⭐ hidden from the rider until he verifies the payment
        "student_phone": ride.student_phone if phone_visible else "",
        "student_phone_masked": _mask_phone(
            ride.contact_phone or ride.student_phone),
        "phone_hidden": not phone_visible,
        # ⭐ v73: the number to dial = the student's own number, or the
        # "booking for someone else" contact. The RIDER never sees the
        # digits on screen — only the dialer gets them.
        "contact_phone": (ride.contact_phone or ride.student_phone)
        if phone_visible
        else "",
        "booking_for_other": bool(ride.booking_for_other),
        "other_name": ride.other_name or "",
        # ⭐ v74: fare split between the people travelling together
        "pax": [{"id": x.id, "name": x.name, "phone": x.phone,
                 "amount": float(x.amount or 0), "paid": bool(x.paid)}
                for x in ride.pax.all()],
        "otp_required": ride.status == "arrived",
        # ⭐ v68: the OTP goes to the STUDENT — he reads it out and the
        # rider types it into his console. The rider never sees it.
        "otp": ride.otp if (viewer != "rider" and ride.status == "arrived")
        else "",
        # ⭐ v68: live rider position (the student watches him move)
        "rider_lat": ride.rider_lat,
        "rider_lng": ride.rider_lng,
        "rider_at": (ride.rider_at.isoformat() if ride.rider_at else ""),
        # ⭐ v72: the student may share their own live position — the
        # rider only sees it while sharing is ON.
        "share_location": bool(ride.share_location),
        "student_lat": (ride.student_lat if ride.share_location else None),
        "student_lng": (ride.student_lng if ride.share_location else None),
        "student_at": (ride.student_at.isoformat() if ride.student_at
                       else ""),
        "created_at": ride.created_at.isoformat() if ride.created_at else "",
    }


def _ride_push_data(ride, event, **extra):
    """⭐ v74: every ride push carries the event + ride code so the app
    can raise the matching POPUP (accepted / rejected / paid / arrived
    with the OTP / started / completed)."""
    data = {"event": event, "ride_code": ride.ride_code,
            "status": ride.status}
    for k, v in extra.items():
        if v is not None:
            data[k] = str(v)
    return data


def _ride_notify_student(ride, title, message, event, **extra):
    """⭐ v74: in-app row + push for the student on every ride event."""
    if not ride.student_id:
        return None
    return _notify(user_id=ride.student_id, title=title, message=message,
                   route="ride", category="ride", portal="student",
                   push_data=_ride_push_data(ride, event, **extra))


def _ride_notify_rider(ride, title, message, event, **extra):
    """⭐ v74: in-app row + push for the ride partner on every event."""
    if not ride.rider_id or ride.rider is None:
        return None
    return _notify_vendor(ride.rider.vendor, title, message, route="ride",
                          portal="rider",
                          push_data=_ride_push_data(ride, event, **extra))


def _ride_notify_riders(ride):
    """Alert every online ride partner offering this vehicle type."""
    from ride.models import RideVendor, rider_is_blocked

    field = f"{ride.vehicle_type}_active"
    if ride.vehicle_type not in ("mini", "sedan", "suv", "xl"):
        return 0
    # ⭐ v73: partners who blocked this slot are not disturbed at all.
    when = ride.scheduled_at or timezone.now()
    riders = RideVendor.objects.filter(
        **{field: True}, is_online=True).select_related("vendor")
    title = "New ride request 🚗"
    when_txt = ""
    try:
        local = timezone.localtime(when)
        when_txt = (f" · {local.strftime('%d %b, %I:%M %p')}")
    except Exception:
        when_txt = ""
    message = (f"{ride.pickup_text[:34]} → {ride.drop_text[:34]} · "
               f"{ride.distance_km} km · {ride.vehicle_label}{when_txt}")
    count = 0
    for rv in riders:
        if rider_is_blocked(rv, when):
            continue
        _notify_vendor(rv.vendor, title, message, route="ride",
                       portal="rider",
                       push_data=_ride_push_data(ride, "new_request",
                                                 pickup=ride.pickup_text[:40]))
        count += 1
    return count


def _ride_rider_or_none(request):
    """(user, VendorProfile, RideVendor) for a ride partner, else None."""
    from ride.models import RideVendor

    user, profile = vendor_user(request)
    if user is None or profile is None:
        return None, None, None
    rv = RideVendor.objects.filter(vendor_id=profile.id).first()
    return user, profile, rv


def _ride_credit_vendor(ride, amount=None, count_ride=False):
    """⭐ v78: move the ride partner's totals.

    A 50-50 ride only *closes* once the second half has been paid, so
    this runs at that moment (or at a normal completion) and never twice
    for the same ride.
    """
    rv = ride.rider
    if rv is None:
        return
    if count_ride:
        rv.total_rides = (rv.total_rides or 0) + 1
    if amount:
        rv.total_earnings = float(rv.total_earnings or 0) + float(
            amount or 0)
    rv.save(update_fields=["total_rides", "total_earnings"])



@csrf_exempt
@student_required
@throttle("rideest", 120, 600)
def ride_estimate(request, user):
    """Distance + fare per vehicle type, before booking."""
    from ride.models import VEHICLE_KEYS

    if request.method != "POST":
        return fail("POST required.", status=405)
    b = json_body(request)
    distance = _ride_haversine_km(b.get("pickup_lat"), b.get("pickup_lng"),
                                  b.get("drop_lat"), b.get("drop_lng"))
    if distance <= 0:
        return fail("Pick a pickup and a drop point on the map first.")
    if distance > 120:
        return fail("That is too far for a campus ride (max 120 km).")
    # ⭐ v73: a vehicle with nobody free right now (or at the chosen
    # time) is shown as UNAVAILABLE — the student can still book it for
    # another slot, we simply never pretend a driver is waiting.
    when = _ride_parse_when(b.get("scheduled_at")) or timezone.now()
    options = []
    for v in VEHICLE_KEYS:
        opt = _ride_estimate_for(v, distance)
        opt["available"] = _ride_has_free_rider(v, when)
        options.append(opt)
    return ok({
        "distance_km": distance,
        "options": options,
        "split_fee_percent": 5.0,
    })


@csrf_exempt
@student_required
@throttle("ridebook", 12, 600)
def ride_book(request, user):
    """Create a ride request and alert the ride partners."""
    from ride.models import Ride, VEHICLE_KEYS

    if request.method != "POST":
        return fail("POST required.", status=405)
    b = json_body(request)
    vtype = str(b.get("vehicle_type", "")).strip().lower()
    if vtype not in VEHICLE_KEYS:
        return fail("Choose a vehicle type.")
    pickup_text = str(b.get("pickup_text", "")).strip()[:200]
    drop_text = str(b.get("drop_text", "")).strip()[:200]
    if not pickup_text or not drop_text:
        return fail("Enter both the pickup and the drop location.")

    distance = _ride_haversine_km(b.get("pickup_lat"), b.get("pickup_lng"),
                                  b.get("drop_lat"), b.get("drop_lng"))
    if distance <= 0:
        return fail("Pick both points on the map so we can price the ride.")
    if distance > 120:
        return fail("That is too far for a campus ride (max 120 km).")

    # ⭐ trust guard: clear any unpaid balance from an earlier ride first.
    pending = Ride.objects.filter(
        student_id=user.id, payment_done=False, amount_paid__gt=0,
        status__in=["accepted", "paid", "arrived", "ongoing", "completed"]
    ).exists()
    if pending:
        return fail("You have an unpaid ride balance. Please clear it "
                    "before booking a new ride.")

    active = Ride.objects.filter(
        student_id=user.id,
        status__in=["requested", "accepted", "paid", "arrived", "ongoing"]
    ).exists()
    if active:
        return fail("You already have an active ride. Complete or cancel "
                    "it before booking another.")

    estimate = _ride_estimate_for(vtype, distance)
    # ⭐ v73: the time slot is COMPULSORY — even "right now" has to be
    # picked by hand, so the rider always knows when to come.
    scheduled = _ride_parse_when(b.get("scheduled_at"))
    if scheduled is None:
        return fail("Choose the time slot for this ride.")

    ride = Ride.objects.create(
        student_id=user.id,
        vehicle_type=vtype,
        pickup_text=pickup_text,
        pickup_lat=b.get("pickup_lat"),
        pickup_lng=b.get("pickup_lng"),
        drop_text=drop_text,
        drop_lat=b.get("drop_lat"),
        drop_lng=b.get("drop_lng"),
        distance_km=distance,
        scheduled_at=scheduled,
        notes=str(b.get("notes", "")).strip()[:300],
        fare=estimate["fare"],
        total=estimate["fare"],
        student_name=(user.first_name or user.username)[:120],
        # ⭐ v73: the number comes from the PROFILE, never from the phone —
        # the student cannot type a different one.
        student_phone=_ride_profile_phone(user),
        contact_phone=_ride_contact_phone(user, b),
        booking_for_other=bool(b.get("booking_for_other")),
        other_name=str(b.get("other_name", "")).strip()[:80],
        status="requested",
    )
    riders = _ride_notify_riders(ride)
    if riders == 0:
        # ⭐ v74: nobody free at that slot -> say it plainly AND close the
        # ride, so the student's history shows it instead of hanging.
        ride.status = "rejected"
        ride.save(update_fields=["status"])
        _ride_notify_student(
            ride, "No rider available right now 🚫",
            ("Every ride partner is busy at that time. Please book "
             "the ride for another time slot."), "no_rider")
        return ok({
            "ride": _ride_public(ride),
            "riders_notified": 0,
            "message": ("No ride partner is available at that time. "
                        "Please book for another time slot."),
        })
    # ⭐ v74: the student hears straight away that the search started
    _ride_notify_student(
        ride,
        "Looking for your rider 🔎",
        (f"{ride.vehicle_label} · {ride.pickup_text[:26]} → "
         f"{ride.drop_text[:26]} · ₹{ride.fare}"),
        "booked")
    return ok({"ride": _ride_public(ride), "riders_notified": riders})


@csrf_exempt
@student_required
def ride_list(request, user):
    """My rides — active first, then history."""
    from ride.models import Ride

    try:
        limit = max(10, min(200, int(request.GET.get("limit", 100))))
    except (TypeError, ValueError):
        limit = 100
    rides = Ride.objects.filter(student_id=user.id).select_related(
        "rider", "rider__vendor").order_by("-created_at")[:limit]
    data = [_ride_public(r) for r in rides]
    active = [r for r in data
              if r["status"] in ("requested", "accepted", "paid",
                                 "arrived", "ongoing")]
    past = [r for r in data if r not in active]
    return ok({"active": active, "past": past, "history": past})


@csrf_exempt
@student_required
def ride_detail(request, user, ride_code):
    """One ride (student must own it)."""
    from ride.models import Ride

    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(),
        student_id=user.id).select_related("rider", "rider__vendor").first()
    if ride is None:
        return fail("Ride not found.", status=404)
    return ok({"ride": _ride_public(ride)})


@csrf_exempt
@student_required
@throttle("rideact", 60, 600)
def ride_cancel(request, user, ride_code):
    from ride.models import Ride

    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), student_id=user.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.status in ("completed", "cancelled"):
        return fail("This ride is already closed.")
    if ride.status == "ongoing":
        return fail("The ride is already running — it cannot be cancelled.")
    ride.status = "cancelled"
    ride.cancel_reason = "Cancelled by student"
    ride.save(update_fields=["status", "cancel_reason"])
    _ride_notify_rider(ride, "Ride cancelled ❌",
                       f"{ride.ride_code} was cancelled by the student.",
                       "cancelled")
    _ride_notify_student(ride, "Ride cancelled",
                         (f"{ride.ride_code} has been cancelled. "
                          f"You can book a new ride any time."),
                         "cancelled")
    return ok({"ride": _ride_public(ride)})


@csrf_exempt
@student_required
@throttle("ridepay", 40, 600)
def ride_pay(request, user, ride_code):
    """Student pays: 'full' (no extra) or 'split' (+5%, half now).

    ⭐ Prices are ALWAYS recomputed server side from the locked fare —
    the app can never tell us a smaller amount.
    """
    from ride.models import Ride

    if request.method != "POST":
        return fail("POST required.", status=405)
    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), student_id=user.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.rider_id is None:
        return fail("No rider has accepted this ride yet.")
    if ride.payment_done:
        return fail("This ride is already paid.")
    b = json_body(request)
    mode = str(b.get("mode", "")).strip().lower()
    if mode not in ("full", "split"):
        return fail("Choose full payment or the 50-50 split.")
    txn = str(b.get("txn_id", "")).strip()[:120]

    amounts = _ride_split_amounts(ride.fare)
    if mode == "full":
        pay_now = amounts["full_total"]
        ride.split_fee = 0
        ride.total = pay_now
        ride.balance_due = 0
        ride.payment_done = True
    else:
        pay_now = amounts["split_now"]
        ride.split_fee = amounts["split_fee"]
        ride.total = amounts["split_total"]
        ride.balance_due = amounts["split_later"]
        ride.payment_done = False
    ride.payment_mode = mode
    ride.amount_paid = pay_now
    ride.txn_first = txn
    ride.status = "paid"
    # ⭐ v75: NOTHING moves on by itself. The rider has to open the ride
    # portal and tap CONFIRM PAYMENT before he can go any further.
    ride.payment_confirmed = False
    if not ride.accepted_at:
        ride.accepted_at = timezone.now()
    ride.save()
    _ride_notify_rider(
        ride, "Payment received — confirm it 💸",
        (f"{ride.ride_code} · ₹{pay_now} received "
         f"({'FULL payment' if mode == 'full' else 'FIRST HALF'})."
         + ("" if mode == "full"
            else f" Balance ₹{ride.balance_due} to collect at the end.")
         + " Open My Rides and tap CONFIRM PAYMENT to continue."),
        "payment_received", amount=pay_now, mode=mode,
        balance=ride.balance_due, txn=txn)
    _ride_notify_student(
        ride,
        "Payment recorded ✅" if mode == "full" else "First half paid ✅",
        (f"₹{pay_now} received for {ride.ride_code}."
         + ("" if mode == "full"
            else f" Pay the remaining ₹{ride.balance_due} after the ride.")),
        "payment_done", amount=pay_now, mode=mode,
        balance=ride.balance_due)
    return ok({"ride": _ride_public(ride), "paid": pay_now})


@csrf_exempt
@student_required
@throttle("rideqr", 60, 600)
def ride_upi(request, user, ride_code):
    """⭐ v75: payment QR for a ride.

    The QR is built from the RIDER's own UPI id (or the platform account
    as a fallback) with the EXACT amount baked in — scanning it fills the
    amount in the UPI app, and the fare can never be edited by the app.
    """
    import base64
    import io
    from urllib.parse import quote

    import qrcode
    from ride.models import Ride

    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), student_id=user.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    try:
        amount = float(request.GET.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        amount = float(ride.amount_now() or 0)
    if amount <= 0:
        return fail("The fare for this ride is not decided yet.")

    payee = ""
    name = "CUnnect Ride"
    uploaded_qr = ""
    if ride.rider_id and ride.rider is not None:
        vp = ride.rider.vendor
        payee = (vp.upi_id or "").strip()
        if payee:
            name = (vp.business_name or "").strip() or name
        try:
            if vp.upi_qr_image:
                uploaded_qr = vp.upi_qr_image.url
        except Exception:
            uploaded_qr = ""
    # ⭐ v77: a rider who only UPLOADED his QR (no UPI id typed in) used to
    # get no QR at all — his uploaded image is used instead. The platform
    # account is the last resort so a ride can always be paid for.
    if not payee and uploaded_qr:
        return ok({
            "qr_url": uploaded_qr,
            "upi_id": "",
            "upi_link": "",
            "name": name,
            "amount": round(amount, 2),
            "source": "uploaded",
        })
    if not payee:
        payee = os.environ.get("CUNNECT_RIDE_UPI", "").strip()
        name = "CUnnect Ride"
    if not payee:
        return fail("Your rider has not added a UPI ID or QR yet. Ask them "
                    "to add it in the ride portal, or pay them in cash.")

    params = [("pa", payee), ("pn", name), ("am", f"{amount:.2f}"),
              ("cu", "INR"), ("tn", ride.ride_code)]
    upi = "upi://pay?" + "&".join(
        f"{k}={quote(str(v), safe='@' if k == 'pa' else '')}"
        for k, v in params)
    img = qrcode.make(upi)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return ok({
        "qr_b64": base64.b64encode(buf.getvalue()).decode(),
        "qr_url": "data:image/png;base64," + base64.b64encode(
            buf.getvalue()).decode(),
        "upi_id": payee,
        "upi_link": upi,
        "name": name,
        "amount": round(amount, 2),
    })


@csrf_exempt
@student_required
@throttle("ridepay", 40, 600)
def ride_pay_balance(request, user, ride_code):
    """Second half of a 50-50 ride (after the ride is completed)."""
    from ride.models import Ride

    if request.method != "POST":
        return fail("POST required.", status=405)
    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), student_id=user.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.payment_done:
        return fail("Nothing left to pay on this ride.")
    if ride.payment_mode != "split":
        return fail("This ride was not booked on the 50-50 plan.")
    if ride.balance_due <= 0:
        return fail("Nothing left to pay on this ride.")
    txn = str(json_body(request).get("txn_id", "")).strip()[:120]
    due = float(ride.balance_due or 0)
    was_awaiting = bool(ride.awaiting_balance)
    ride.amount_paid = float(ride.amount_paid or 0) + due
    ride.balance_due = 0
    ride.txn_second = txn
    ride.payment_done = True
    # ⭐ v78: if the rider had already ended the trip, THIS payment is what
    # closes the ride (and what moves the partner's totals).
    if was_awaiting:
        ride.awaiting_balance = False
        ride.status = "completed"
        if not ride.completed_at:
            ride.completed_at = timezone.now()
        ride.otp = ""
    ride.save()
    if was_awaiting:
        _ride_credit_vendor(ride, amount=ride.amount_paid, count_ride=True)
        _ride_notify_rider(
            ride, "Balance cleared — ride completed 🏁",
            (f"{ride.ride_code} · ₹{ride.amount_paid} received in full. "
             f"Transaction ID: {txn or '—'}"),
            "balance_paid", amount=due, txn=txn)
        _ride_notify_student(
            ride, "Ride completed 🏁",
            (f"₹{ride.amount_paid} received for {ride.ride_code}. "
             f"Nothing left to pay — thanks for riding with CUnnect!"),
            "balance_done", amount=ride.amount_paid)
    else:
        _ride_notify_rider(ride, "Balance cleared ✅",
                           f"{ride.ride_code} · full payment received.",
                           "balance_paid", amount=ride.amount_paid, txn=txn)
        _ride_notify_student(ride, "Ride fully paid 🎉",
                             (f"₹{ride.amount_paid} received for "
                              f"{ride.ride_code}. Nothing left to pay."),
                             "balance_done", amount=ride.amount_paid)
    return ok({"ride": _ride_public(ride), "paid": float(ride.amount_paid),
               "completed": was_awaiting})


@csrf_exempt
@student_required
@throttle("rideotp", 30, 600)
def ride_verify_otp(request, user, ride_code):
    """Student shares the OTP the rider's app triggered — ride starts."""
    from ride.models import Ride

    if request.method != "POST":
        return fail("POST required.", status=405)
    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), student_id=user.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.status == "ongoing":
        return ok({"ride": _ride_public(ride), "already": True})
    if ride.status != "arrived":
        return fail("Your rider has not reached the pickup point yet.")
    entered = str(json_body(request).get("otp", "")).strip()
    if not entered or entered != (ride.otp or ""):
        return fail("That OTP is not correct. Check with your rider.")
    ride.status = "ongoing"
    ride.started_at = timezone.now()
    ride.otp = ""
    ride.save(update_fields=["status", "started_at", "otp"])
    if ride.rider_id:
        _notify_vendor(ride.rider.vendor, "Ride started ▶️",
                       f"{ride.ride_code} · OTP verified by the student.")
    return ok({"ride": _ride_public(ride)})


# --------------------------- RIDER PORTAL ---------------------------

@csrf_exempt
@student_required
def ride_vendor_profile(request, user):
    """GET/POST the ride partner's vehicles + own pricing."""
    from ride.models import (RideVehicle, RideVendor, VEHICLE_ICON,
                             VEHICLE_KEYS, VEHICLE_LABEL, VEHICLE_SEATS)

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    if request.method != "POST":
        if rv is None:
            rv = RideVendor.objects.create(vendor_id=profile.id)
        vehicles = []
        for key in VEHICLE_KEYS:
            active, base, per_km = (rv.rate_for(key) if rv.pk
                                    else (False, 0, 0))
            vehicles.append({
                "key": key,
                "label": VEHICLE_LABEL[key],
                "icon": VEHICLE_ICON[key],
                "seats": VEHICLE_SEATS[key],
                "active": bool(active),
                "base": float(base or 0),
                "per_km": float(per_km or 0),
            })
        return ok({
            "profile": {
                "business_name": profile.business_name,
                "vehicle_number": rv.vehicle_number,
                "vehicle_model": rv.vehicle_model,
                "is_online": rv.is_online,
                # ⭐ v79: "I drive an auto" — these partners receive the
                # one-tap AUTO calls from the student Ride screen.
                "is_auto": bool(rv.is_auto),
                "upi_id": profile.upi_id or "",
                "total_rides": rv.total_rides,
                "total_earnings": float(rv.total_earnings or 0),
            },
            "vehicles": vehicles,
            # ⭐ v77: the partner's own cars (name + plate) — these are the
            # options he picks from while accepting a request.
            "garage": [c.as_dict() for c in RideVehicle.objects.filter(
                rider_id=rv.id)] if rv.pk else [],
        })

    b = json_body(request)
    if rv is None:
        rv = RideVendor.objects.create(vendor_id=profile.id)
    rv.vehicle_number = str(b.get("vehicle_number", ""))[:24].strip().upper()
    rv.vehicle_model = str(b.get("vehicle_model", ""))[:80].strip()
    if "is_online" in b:
        rv.is_online = bool(b.get("is_online"))
    if "is_auto" in b:
        rv.is_auto = bool(b.get("is_auto"))
    for key in VEHICLE_KEYS:
        spec = b.get(key)
        if not isinstance(spec, dict):
            continue
        try:
            base = round(float(spec.get("base", 0) or 0), 2)
            per_km = round(float(spec.get("per_km", 0) or 0), 2)
        except (TypeError, ValueError):
            continue
        if base < 0 or per_km < 0 or base > 5000 or per_km > 500:
            continue
        rv.set_rate(key, bool(spec.get("active")), base, per_km)
    rv.save()
    return ok({"saved": True, "is_online": rv.is_online,
               "is_auto": rv.is_auto})


@csrf_exempt
@student_required
@throttle("rideauto", 30, 600)
def ride_auto_call(request, user):
    """⭐ v79: ONE TAP -> every auto partner is alerted at the same time.

    There is no booking behind this: no pickup or drop to type, no fare,
    no payment, no OTP and no ride record. The pickup is always the
    campus main gate — the partners know it, so it is never spelled out
    to the student.
    """
    from ride.models import RideVendor

    if request.method != "POST":
        return fail("POST required.", status=405)
    # ⭐ the pickup for an auto is ALWAYS the campus main gate — the
    # partners know it, so the student never has to type or see it.
    lat, lng = 26.621884, 80.687916
    who = (getattr(user, "first_name", "") or user.username or
           "A student").strip()
    title = "Auto needed at the main gate 🛺"
    message = f"{who} is waiting at the campus main gate for an auto."
    sent = 0
    for rv in RideVendor.objects.filter(
            is_auto=True).select_related("vendor"):
        _notify_vendor(
            rv.vendor, title, message, route="ride", portal="rider",
            push_data={"event": "auto_call", "ride_code": "",
                       "lat": str(lat), "lng": str(lng)})
        sent += 1
    return ok({"sent": sent,
               "message": (f"{sent} auto partner"
                           f"{'' if sent == 1 else 's'} alerted."
                           if sent else
                           "No auto partner has joined CUnnect yet.")})


@csrf_exempt
@student_required
def ride_vendor_requests(request, user):
    """Open ride requests this partner can accept (phone stays hidden)."""
    from ride.models import Ride

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    if rv is None:
        return ok({"requests": [], "setup_required": True})
    from ride.models import RideRejection

    mine = rv.active_vehicles()
    skipped = RideRejection.objects.filter(
        vendor=rv).values_list("ride_id", flat=True)
    rides = Ride.objects.filter(
        status="requested", vehicle_type__in=mine
    ).exclude(id__in=list(skipped)).select_related(
        "student").order_by("created_at")[:30]
    return ok({
        "requests": [_ride_public(r, viewer="rider") for r in rides],
        "active_vehicles": mine,
    })


@csrf_exempt
@student_required
def ride_vendor_rides(request, user):
    """This partner's accepted / running / finished rides."""
    from ride.models import Ride

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    if rv is None:
        return ok({"active": [], "past": []})
    try:
        limit = max(10, min(300, int(request.GET.get("limit", 200))))
    except (TypeError, ValueError):
        limit = 200
    rides = Ride.objects.filter(rider_id=rv.id).select_related(
        "student").order_by("-created_at")[:limit]
    data = [_ride_public(r, viewer="rider") for r in rides]
    active = [r for r in data
              if r["status"] in ("accepted", "paid", "arrived", "ongoing")]
    past = [r for r in data if r not in active]
    # ⭐ v74: earnings summary for the rider portal dashboard
    stats = {"rides": 0, "earnings": 0.0, "today": 0.0, "today_rides": 0,
             "month": 0.0, "pending": 0.0, "km": 0.0}
    try:
        from django.utils import timezone as _tzn

        today = _tzn.localdate()
        month_start = today.replace(day=1)
        done = Ride.objects.filter(rider_id=rv.id, status="completed")
        stats["rides"] = done.count()
        stats["earnings"] = round(sum(float(r.amount_paid or 0)
                                      for r in done), 2)
        stats["km"] = round(sum(float(r.distance_km or 0) for r in done), 2)
        todays = [r for r in done if r.completed_at
                  and _tzn.localtime(r.completed_at).date() == today]
        stats["today"] = round(sum(float(r.amount_paid or 0)
                                   for r in todays), 2)
        stats["today_rides"] = len(todays)
        months = [r for r in done if r.completed_at
                  and _tzn.localtime(r.completed_at).date() >= month_start]
        stats["month"] = round(sum(float(r.amount_paid or 0)
                                   for r in months), 2)
        stats["pending"] = round(sum(
            float(r.balance_due or 0) for r in Ride.objects.filter(
                rider_id=rv.id, payment_done=False)), 2)
    except Exception:
        pass
    return ok({"active": active, "past": past, "history": past,
               "stats": stats})


@csrf_exempt
@student_required
@throttle("rideact", 60, 600)
def ride_vendor_accept(request, user, ride_code):
    """Accept a ride — the fare locks to THIS rider's own rate."""
    from ride.models import Ride

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None:
        return fail("Vendor account not found.", status=401)
    if rv is None:
        return fail("Set up your ride profile first.")
    ride = Ride.objects.filter(ride_code=str(ride_code).strip()).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    active, base, per_km = rv.rate_for(ride.vehicle_type)
    if not active:
        return fail(f"You do not offer {ride.vehicle_label} rides.")
    # ⭐ first-come-first-served (atomic — two riders cannot both win)
    updated = Ride.objects.filter(
        id=ride.id, status="requested").update(
            rider_id=rv.id, status="accepted",
            accepted_at=timezone.now(), base_fare=base, per_km=per_km)
    if not updated:
        return fail("Another rider already took this ride.")
    ride.refresh_from_db()
    ride.fare = _ride_fare(base, per_km, ride.distance_km)
    ride.total = ride.fare
    ride.split_fee = 0
    ride.balance_due = 0
    # ⭐ v77: the car for THIS ride — one of the partner's saved vehicles
    # (id) or a one-off (name + plate) that is NOT added to his garage.
    body = {}
    try:
        body = json_body(request) or {}
    except Exception:
        body = {}
    v_name = str(body.get("vehicle_name", "")).strip()[:80]
    v_plate = str(body.get("vehicle_plate", "")).strip()[:24]
    v_id = str(body.get("vehicle_id", "")).strip()
    if v_id.isdigit():
        from ride.models import RideVehicle

        car = RideVehicle.objects.filter(
            id=int(v_id), rider_id=rv.id).first()
        if car is not None:
            v_name = v_name or car.name
            v_plate = v_plate or car.plate
    if v_name or v_plate:
        ride.vehicle_name = v_name
        ride.vehicle_plate = v_plate
    ride.save(update_fields=["fare", "total", "split_fee", "balance_due",
                             "vehicle_name", "vehicle_plate"])
    _ride_notify_student(
        ride,
        "Rider accepted your ride 🚗",
        (f"{profile.business_name} is on the way — complete the payment "
         f"of ₹{ride.fare} to confirm."),
        "accepted", amount=ride.fare, rider=profile.business_name)
    return ok({"ride": _ride_public(ride, viewer="rider")})


@csrf_exempt
@throttle("rideact", 60, 600)
def ride_vendor_confirm(request, ride_code):
    """⭐ v75: the RIDER confirms that the payment actually reached him.

    Nothing is automatic after the student pays — the ride only moves
    forward once the rider taps CONFIRM PAYMENT in his portal.
    """
    from ride.models import Ride

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Ride partner account not found.", status=401)
    ride = Ride.objects.filter(ride_code=str(ride_code).strip()).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.rider_id != rv.id:
        return fail("You have not accepted this ride.", status=403)
    if ride.status in ("completed", "cancelled"):
        return fail("This ride is closed.")
    if ride.amount_paid <= 0:
        return fail("The student has not paid yet.")
    if ride.payment_confirmed:
        return ok({"ride": _ride_public(ride, viewer="rider"),
                   "already": True})
    ride.payment_confirmed = True
    ride.save(update_fields=["payment_confirmed"])
    _ride_notify_student(
        ride, "Rider confirmed your payment ✅",
        (f"{profile.business_name} has confirmed your payment of "
         f"₹{ride.amount_paid} for {ride.ride_code}."),
        "confirmed", amount=ride.amount_paid, rider=profile.business_name)
    return ok({"ride": _ride_public(ride, viewer="rider")})


@csrf_exempt
@throttle("ridegarage", 60, 600)
def ride_vendor_vehicles(request):
    """⭐ v77: the ride partner's garage — every car he owns, per category.

    GET  -> the list (mini / sedan / SUV, each with its number plate)
    POST -> add one {vehicle_type, name, plate}
    """
    from ride.models import VEHICLE_KEYS, RideVehicle

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Ride partner account not found.", status=401)
    if request.method == "POST":
        b = json_body(request) or {}
        vtype = str(b.get("vehicle_type", "")).strip().lower()
        if vtype not in VEHICLE_KEYS:
            return fail("Choose Mini, Sedan or SUV.")
        name = str(b.get("name", "")).strip()[:80]
        plate = str(b.get("plate", "")).strip().upper()[:24]
        if not name and not plate:
            return fail("Give the car a name and its number plate.")
        car = RideVehicle.objects.create(
            rider=rv, vehicle_type=vtype, name=name, plate=plate)
        return ok({"vehicle": car.as_dict()})
    return ok({"vehicles": [c.as_dict() for c in
                            RideVehicle.objects.filter(rider_id=rv.id)]})


@csrf_exempt
@throttle("ridegarage", 60, 600)
def ride_vendor_vehicle_delete(request, vehicle_id):
    """⭐ v77: remove a car from the garage."""
    from ride.models import RideVehicle

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Ride partner account not found.", status=401)
    RideVehicle.objects.filter(id=vehicle_id, rider_id=rv.id).delete()
    return ok({"deleted": True})


@csrf_exempt
@student_required
@throttle("rideact", 60, 600)
def ride_vendor_reject(request, user, ride_code):
    """Decline — the ride simply leaves this partner's list."""
    from ride.models import Ride

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Vendor account not found.", status=401)
    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), status="requested").first()
    if ride is None:
        return fail("This ride is no longer available.")
    from ride.models import RideRejection

    RideRejection.objects.get_or_create(ride=ride, vendor=rv)
    # ⭐ v73: the student hears about it immediately.
    _ride_notify_student(
        ride,
        "Rider unavailable 🚫",
        ("The ride partner cannot take this ride right now. Please book "
         "for another time slot."),
        "rejected")
    # ⭐ v73: if NO partner is left who could still take it, the ride is
    # over — the student's app then says "book for another time"
    # instead of spinning on "finding your rider" forever.
    try:
        from ride.models import RideVendor, rider_is_blocked
        field = f"{ride.vehicle_type}_active"
        when = ride.scheduled_at or timezone.now()
        left = 0
        for other in RideVendor.objects.filter(**{field: True}, is_online=True):
            if RideRejection.objects.filter(ride=ride, vendor=other).exists():
                continue
            if rider_is_blocked(other, when):
                continue
            left += 1
        if left == 0:
            ride.status = "rejected"
            ride.save(update_fields=["status"])
            _ride_notify_student(
                ride,
                "No rider available right now 🚫",
                ("Every ride partner is busy at that time. Please book "
                 "the ride for another time slot."),
                "no_rider")
    except Exception:
        pass
    return ok({"rejected": True, "ride_code": ride.ride_code})


@csrf_exempt
@student_required
@throttle("rideact", 60, 600)
def ride_vendor_arrived(request, user, ride_code):
    """Rider: "I'm on location" -> generate + send the start OTP."""
    from ride.models import Ride

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Vendor account not found.", status=401)
    ride = Ride.objects.filter(ride_code=str(ride_code).strip()).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.rider_id != rv.id:
        return fail("You have not accepted this ride.", status=403)
    if ride.status in ("completed", "cancelled"):
        return fail("This ride is closed.")
    if ride.status == "arrived":
        return ok({"ride": _ride_public(ride, viewer="rider"), "otp": ride.otp})
    if ride.status == "ongoing":
        return fail("This ride is already running.")
    if ride.status == "accepted":
        return fail("The student has not paid yet — you will be notified "
                    "as soon as the payment comes in.")
    if ride.amount_paid <= 0:
        return fail("Payment is not recorded for this ride yet.")
    if not ride.payment_confirmed:
        return fail("Confirm the payment first — open My Rides and tap "
                    "CONFIRM PAYMENT.")
    import random

    otp = "".join(random.choice("0123456789") for _ in range(4))
    ride.otp = otp
    ride.status = "arrived"
    ride.arrived_at = timezone.now()
    ride.save(update_fields=["otp", "status", "arrived_at"])
    _ride_notify_student(
        ride, "Your rider has arrived 📍",
        (f"{profile.business_name} is at the pickup point. "
         f"Share this OTP to start the ride: {otp}"),
        "arrived", otp=otp, rider=profile.business_name)
    return ok({"ride": _ride_public(ride, viewer="rider"), "otp": otp})


@csrf_exempt
@student_required
@throttle("rideblocks", 60, 600)
def ride_vendor_blocks(request, user):
    """⭐ v73: the rider's "I am not available" slots.

    GET  -> list   POST -> add one
    body: {kind: "daily"|"date", weekday, start_min, end_min, date, label}

    Anything added here is skipped when rides are offered, and a student
    booking inside the slot is told to pick another time.
    """
    from ride.models import RiderBlock

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Vendor account not found.", status=401)

    def _slot(b):
        return {
            "id": b.id,
            "kind": b.kind,
            "weekday": b.weekday,
            "weekday_name": b.weekday_name,
            "start_min": b.start_min,
            "end_min": b.end_min,
            "start": f"{b.start_min // 60:02d}:{b.start_min % 60:02d}",
            "end": f"{b.end_min // 60:02d}:{b.end_min % 60:02d}",
            "date": b.date.isoformat() if b.date else "",
            "label": b.label,
        }

    if request.method == "GET":
        return ok({"blocks": [_slot(b) for b in
                              RiderBlock.objects.filter(rider_id=rv.id)]})

    if request.method != "POST":
        return fail("POST required.", status=405)

    b = json_body(request)
    kind = str(b.get("kind", "date")).strip().lower()
    if kind not in ("daily", "date"):
        return fail("Pick either a repeating slot or a specific date.")

    def _minutes(value, default):
        raw = str(value or "").strip()
        if ":" in raw:                      # "14:30"
            try:
                h, m = raw.split(":", 1)
                return max(0, min(1439, int(h) * 60 + int(m)))
            except ValueError:
                return default
        try:                                 # already minutes
            return max(0, min(1439, int(float(raw))))
        except (TypeError, ValueError):
            return default

    start = _minutes(b.get("start_min") or b.get("start"), 0)
    end = _minutes(b.get("end_min") or b.get("end"), 1439)
    if end < start:
        start, end = end, start

    block = RiderBlock(rider_id=rv.id, kind=kind, start_min=start,
                       end_min=end,
                       label=str(b.get("label", "")).strip()[:80])
    if kind == "daily":
        try:
            block.weekday = max(0, min(6, int(b.get("weekday", 0))))
        except (TypeError, ValueError):
            block.weekday = 0
        block.date = None
    else:
        from django.utils.dateparse import parse_date

        day = parse_date(str(b.get("date", "")).strip())
        if day is None:
            return fail("Pick the date you are unavailable on.")
        block.date = day
    block.save()
    return ok({"block": _slot(block)})


@csrf_exempt
@student_required
@throttle("rideblocks", 60, 600)
def ride_vendor_block_delete(request, user, block_id):
    """⭐ v73: remove one unavailability slot."""
    from ride.models import RiderBlock

    if request.method != "POST":
        return fail("POST required.", status=405)
    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Vendor account not found.", status=401)
    deleted, _ = RiderBlock.objects.filter(
        id=block_id, rider_id=rv.id).delete()
    return ok({"deleted": deleted > 0})


@csrf_exempt
@student_required
@throttle("ridestuloc", 120, 600)
def ride_student_location(request, user, ride_code):
    """⭐ v72: the student shares their live position with the rider.

    Only stored while the ride is live, and the rider only ever sees it
    when the student has the "share my live location" switch ON.
    """
    from ride.models import Ride

    if request.method != "POST":
        return fail("POST required.", status=405)
    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), student_id=user.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.status not in ("accepted", "paid", "arrived", "ongoing"):
        return ok({"sent": False})
    b = json_body(request)
    share = b.get("share")
    if share is not None:
        was = bool(ride.share_location)
        ride.share_location = bool(share)
        ride.save(update_fields=["share_location"])
        # ⭐ v74: the rider is told the moment the pin is switched on
        if ride.share_location and not was:
            _ride_notify_rider(
                ride, "Student shared their live location 📍",
                (f"{ride.student_name or 'The student'} is now sharing "
                 f"their exact position for {ride.ride_code}."),
                "location_shared")
    lat = b.get("lat")
    lng = b.get("lng")
    if lat is None or lng is None:
        return ok({"sent": False})
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return fail("Bad coordinates.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return fail("Bad coordinates.")
    ride.student_lat = lat
    ride.student_lng = lng
    ride.student_at = timezone.now()
    ride.save(update_fields=["student_lat", "student_lng", "student_at"])
    return ok({"sent": True})


@csrf_exempt
@student_required
@throttle("rideact", 60, 600)
def ride_vendor_start(request, user, ride_code):
    """⭐ v68: the RIDER types the OTP the student read out to him.

    Earlier the student entered it on his phone; now the OTP lives on
    the student's screen and the rider confirms it on his."""
    from ride.models import Ride

    if request.method != "POST":
        return fail("POST required.", status=405)
    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Vendor account not found.", status=401)
    ride = Ride.objects.filter(ride_code=str(ride_code).strip()).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.rider_id != rv.id:
        return fail("You have not accepted this ride.", status=403)
    if ride.status == "ongoing":
        return ok({"ride": _ride_public(ride, viewer="rider"), "already": True})
    if ride.status != "arrived":
        return fail("Tap \"I'm on location\" first, then enter the OTP.")
    entered = str(json_body(request).get("otp", "")).strip()
    if not entered or entered != (ride.otp or ""):
        return fail("That OTP is not right — ask the student to read it again.")
    ride.status = "ongoing"
    ride.started_at = timezone.now()
    ride.otp = ""
    ride.save(update_fields=["status", "started_at", "otp"])
    _ride_notify_student(
        ride, "Your ride has started ▶️",
        (f"{profile.business_name} verified the OTP — have a safe trip!"),
        "started", rider=profile.business_name)
    _ride_notify_rider(ride, "Ride started ▶️",
                       f"{ride.ride_code} · OTP verified.", "started")
    return ok({"ride": _ride_public(ride, viewer="rider")})


@csrf_exempt
@student_required
@throttle("rideloc", 600, 600)
def ride_vendor_location(request, user, ride_code):
    """⭐ v68: the rider app pushes its GPS position while a ride is
    live, so the student sees the vehicle move on the map."""
    from ride.models import Ride

    if request.method != "POST":
        return fail("POST required.", status=405)
    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Vendor account not found.", status=401)
    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), rider_id=rv.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.status not in ("accepted", "paid", "arrived", "ongoing"):
        return ok({"sent": False})
    b = json_body(request)
    try:
        lat = float(b.get("lat"))
        lng = float(b.get("lng"))
    except (TypeError, ValueError):
        return fail("Bad coordinates.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return fail("Bad coordinates.")
    ride.rider_lat = lat
    ride.rider_lng = lng
    ride.rider_at = timezone.now()
    ride.save(update_fields=["rider_lat", "rider_lng", "rider_at"])
    return ok({"sent": True})


@csrf_exempt
@student_required
@throttle("rideact", 60, 600)
def ride_vendor_complete(request, user, ride_code):
    """Rider finishes the ride -> student is notified."""
    from ride.models import Ride

    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Vendor account not found.", status=401)
    ride = Ride.objects.filter(ride_code=str(ride_code).strip()).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.rider_id != rv.id:
        return fail("You have not accepted this ride.", status=403)
    if ride.status == "completed":
        return ok({"ride": _ride_public(ride, viewer="rider")})
    if ride.status != "ongoing":
        return fail("The ride has not started yet (OTP pending).")
    # ⭐ v78: a 50-50 ride CANNOT be closed while the second half is
    # unpaid. It parks here until the student pays the balance (with its
    # own transaction id) — only then does the ride complete.
    if not ride.payment_done and float(ride.balance_due or 0) > 0:
        if not ride.awaiting_balance:
            ride.awaiting_balance = True
            ride.save(update_fields=["awaiting_balance"])
        _ride_notify_student(
            ride, "Pay the balance to close this ride 💸",
            (f"{ride.ride_code} · ₹{ride.balance_due} is still due. "
             f"Pay it in the app and your ride is complete."),
            "balance_pending", amount=ride.balance_due,
            balance=ride.balance_due)
        _ride_notify_rider(
            ride, "Waiting for the balance ⏳",
            (f"{ride.ride_code} · the student has to pay the remaining "
             f"₹{ride.balance_due} before this ride can be closed."),
            "balance_pending", amount=ride.balance_due,
            balance=ride.balance_due)
        return ok({"ride": _ride_public(ride, viewer="rider"),
                   "awaiting_balance": True,
                   "balance": float(ride.balance_due)})
    ride.status = "completed"
    ride.completed_at = timezone.now()
    ride.otp = ""
    ride.awaiting_balance = False
    ride.save(update_fields=["status", "completed_at", "otp",
                             "awaiting_balance"])
    _ride_credit_vendor(ride, amount=ride.amount_paid, count_ride=True)
    extra = ""
    _ride_notify_student(
        ride, "Your ride has been completed successfully 🏁",
        (f"{ride.pickup_text[:28]} → {ride.drop_text[:28]} · "
         f"₹{ride.total}.{extra}"),
        "completed", amount=ride.total, balance=ride.balance_due)
    _ride_notify_rider(
        ride, "Ride completed 🏁",
        (f"{ride.ride_code} · ₹{ride.amount_paid} collected."
         + ("" if ride.payment_done
            else f" Collect the balance ₹{ride.balance_due} from the "
                 f"student.")),
        "completed", amount=ride.amount_paid, balance=ride.balance_due)
    return ok({"ride": _ride_public(ride, viewer="rider")})


# ---------------------- v74: safety, split fare, stats ----------------

@csrf_exempt
@student_required
@throttle("ridesos", 20, 600)
def ride_sos(request, user, ride_code):
    """⭐ v74: SOS — the passenger's rider and every online partner are
    alerted at once with the live location link."""
    from ride.models import Ride

    if request.method != "POST":
        return fail("POST required.", status=405)
    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), student_id=user.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    b = json_body(request)
    try:
        lat = float(b.get("lat") or ride.pickup_lat or 0)
        lng = float(b.get("lng") or ride.pickup_lng or 0)
    except (TypeError, ValueError):
        lat, lng = 0.0, 0.0
    link = f"https://maps.google.com/?q={lat},{lng}"
    who = (ride.student_name or user.username or "A student").strip()
    _ride_notify_rider(ride, "🚨 SOS FROM YOUR PASSENGER",
                       (f"{who} pressed SOS on {ride.ride_code}. "
                        f"Location: {link}"),
                       "sos", lat=lat, lng=lng)
    # every other online partner hears it too — somebody will respond
    try:
        from ride.models import RideVendor

        for rv in RideVendor.objects.filter(
                is_online=True).select_related("vendor"):
            if ride.rider_id and rv.id == ride.rider_id:
                continue
            _notify_vendor(
                rv.vendor, "🚨 SOS on campus",
                (f"{who} needs help on {ride.ride_code}. {link}"),
                route="ride", portal="rider",
                push_data={"event": "sos", "ride_code": ride.ride_code,
                           "lat": lat, "lng": lng})
    except Exception:
        pass
    return ok({"sent": True, "link": link, "lat": lat, "lng": lng})


@csrf_exempt
@student_required
@throttle("rideact", 60, 600)
def ride_vendor_collect_balance(request, user, ride_code):
    """⭐ v74: the rider confirms he took the remaining cash."""
    from ride.models import Ride

    if request.method != "POST":
        return fail("POST required.", status=405)
    _u, profile, rv = _ride_rider_or_none(request)
    if profile is None or rv is None:
        return fail("Vendor account not found.", status=401)
    ride = Ride.objects.filter(ride_code=str(ride_code).strip()).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    if ride.rider_id != rv.id:
        return fail("You have not accepted this ride.", status=403)
    if ride.payment_done:
        return ok({"ride": _ride_public(ride, viewer="rider"),
                   "collected": 0})
    if float(ride.balance_due or 0) <= 0:
        return fail("Nothing left to collect on this ride.")
    got = float(ride.balance_due)
    was_awaiting = bool(ride.awaiting_balance)
    ride.amount_paid = float(ride.amount_paid or 0) + got
    ride.balance_due = 0
    ride.payment_done = True
    ride.txn_second = (ride.txn_second or "CASH")[:120]
    if was_awaiting:
        ride.awaiting_balance = False
        ride.status = "completed"
        if not ride.completed_at:
            ride.completed_at = timezone.now()
        ride.otp = ""
    ride.save(update_fields=["amount_paid", "balance_due", "payment_done",
                             "txn_second", "awaiting_balance", "status",
                             "completed_at", "otp"])
    if was_awaiting:
        _ride_credit_vendor(ride, amount=ride.amount_paid, count_ride=True)
    _ride_notify_student(
        ride, "Balance received by the rider 💵",
        (f"₹{got} collected in cash — {ride.ride_code} is fully paid. "
         f"Thanks for riding with CUnnect!"),
        "balance_cleared", amount=got)
    return ok({"ride": _ride_public(ride, viewer="rider"),
               "collected": got})


@csrf_exempt
@student_required
@throttle("ridepax", 60, 600)
def ride_pax(request, user, ride_code):
    """⭐ v74: split the fare with the people travelling along.

    GET  -> the co-passengers on this ride
    POST -> add one {name, phone, amount}
    """
    from ride.models import Ride, RidePax

    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), student_id=user.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)

    def _row(p):
        return {"id": p.id, "name": p.name, "phone": p.phone,
                "amount": float(p.amount or 0), "paid": bool(p.paid)}

    if request.method == "GET":
        rows = [_row(p) for p in RidePax.objects.filter(ride_id=ride.id)]
        return ok({"pax": rows, "fare": float(ride.fare or 0),
                   "assigned": round(sum(r["amount"] for r in rows), 2)})

    if request.method != "POST":
        return fail("POST required.", status=405)
    if ride.status in ("completed", "cancelled"):
        return fail("This ride is already closed.")
    b = json_body(request)
    try:
        amount = round(float(b.get("amount") or 0), 2)
    except (TypeError, ValueError):
        amount = 0.0
    if amount <= 0:
        return fail("Enter how much this person pays.")
    # never let the split run away past the fare
    used = sum(float(p.amount or 0)
               for p in RidePax.objects.filter(ride_id=ride.id))
    if used + amount > float(ride.fare or 0) + 0.01:
        return fail("That is more than the fare — check the amounts.")
    p = RidePax.objects.create(
        ride_id=ride.id,
        name=str(b.get("name", "")).strip()[:80],
        phone=str(b.get("phone", "")).strip()[:20],
        amount=amount,
    )
    return ok({"pax": _row(p)})


@csrf_exempt
@student_required
@throttle("ridepax", 60, 600)
def ride_pax_action(request, user, ride_code, pax_id):
    """⭐ v74: mark a co-passenger paid, or remove them."""
    from ride.models import Ride, RidePax

    if request.method != "POST":
        return fail("POST required.", status=405)
    ride = Ride.objects.filter(
        ride_code=str(ride_code).strip(), student_id=user.id).first()
    if ride is None:
        return fail("Ride not found.", status=404)
    p = RidePax.objects.filter(id=pax_id, ride_id=ride.id).first()
    if p is None:
        return fail("Not found.", status=404)
    b = json_body(request)
    if b.get("delete"):
        p.delete()
        return ok({"deleted": True})
    p.name = str(b.get("name", p.name)).strip()[:80] or p.name
    p.phone = str(b.get("phone", p.phone)).strip()[:20]
    if "amount" in b:
        try:
            p.amount = round(float(b.get("amount") or 0), 2)
        except (TypeError, ValueError):
            pass
    if "paid" in b:
        p.paid = bool(b.get("paid"))
    p.save()
    return ok({"pax": {"id": p.id, "name": p.name, "phone": p.phone,
                       "amount": float(p.amount or 0),
                       "paid": bool(p.paid)}})


@csrf_exempt
@student_required
def ride_stats(request, user):
    """⭐ v74: the student's own ride statistics + favourite routes."""
    from ride.models import Ride

    qs = Ride.objects.filter(student_id=user.id)
    done = [r for r in qs if r.status == "completed"]
    rides = len(done)
    km = round(sum(float(r.distance_km or 0) for r in done), 2)
    spent = round(sum(float(r.amount_paid or 0) for r in done), 2)
    routes = {}
    for r in done:
        key = f"{str(r.pickup_text)[:22]} → {str(r.drop_text)[:22]}"
        routes[key] = routes.get(key, 0) + 1
    top = [{"route": k, "times": v}
           for k, v in sorted(routes.items(), key=lambda kv: -kv[1])[:3]]
    return ok({"stats": {
        "rides": rides,
        "km": km,
        "spent": spent,
        "cancelled": sum(1 for r in qs if r.status == "cancelled"),
        # an average auto/bus comparison is guesswork — we show what is real
        "favourites": top,
    }})
