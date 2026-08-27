import random
import string
import time

from email.mime.image import MIMEImage
from pathlib import Path

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives, send_mail
from django.http import JsonResponse
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from network.models import ChatRoom
from food.models import FoodItem, Order, Notification

from .forms import VendorFoodItemForm
from .models import (
    UserProfile,
    VendorProfile,
    DeliveryProfile,
    ChatMessage,
    Banner,
    SupportRequest,
    PrintOrder,
)


# ====================== OTP EMAIL ======================

OTP_VALID_SECONDS = 5 * 60


def send_cunnect_otp_email(recipient, otp, full_name="", user_id="", is_resend=False):
    """Send a branded plain-text + HTML OTP email with an inline CUnnect image."""
    spaced_otp = str(otp)
    greeting_name = full_name or user_id or "there"
    subject = (
        "Hey, Let's CUnnect👋"
        if is_resend
        else "Hey, Let's CUnnect👋"
    )

    plain_message = (
        "Your ticket's here,\n"
        "The whole campus is trying to get in. Are you?\n\n"
        f"Your OTP: {spaced_otp}\n\n"
        "This OTP is valid for 5 minutes.\n\n"
        "This was built for us,\n"
        "And I'll see you inside."
    )

    html_message = f"""
    <!doctype html>
    <html>
      <body style="margin:0;padding:0;background:#000000;font-family:Arial,sans-serif;color:#ffffff;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#000000;padding:28px 12px;">
          <tr>
            <td align="center">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#000000;border:0;border-radius:0;overflow:hidden;">
                <tr>
                  <td>
                    <img src="cid:cunnect-email-logo" alt="CUnnect" width="560" style="display:block;width:100%;max-width:560px;height:auto;">
                  </td>
                </tr>
                <tr>
                  <td style="padding:28px 30px 32px;">
                    <p style="margin:0 0 8px;font-size:16px;line-height:1.55;color:#f2f2f2;">Your ticket's here,<br>The whole campus is trying to get in. Are you?</p>
                    <div style="margin:25px 0 20px;padding:17px;border:1px solid #f10b1d;border-radius:12px;background:#250d11;text-align:center;">
                      <div style="margin-bottom:8px;font-size:12px;font-weight:700;letter-spacing:1.5px;color:#ff9ca5;">🔐 YOUR OTP</div>
                      <div style="font-size:32px;font-weight:800;letter-spacing:2px;white-space:nowrap;color:#ffffff;">{spaced_otp}</div>
                    </div>
                    <p style="margin:0 0 20px;font-size:14px;color:#d0d0d0;">⏳ Valid for <strong style="color:#ffffff;">5 minutes</strong>.</p>
                    <p style="margin:0;font-size:15px;line-height:1.55;color:#f2f2f2;">This was built for us,<br>And I'll see you inside. 👊</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """

    email = EmailMultiAlternatives(
        subject=subject,
        body=plain_message,
        from_email=f"CUnnect<{settings.EMAIL_HOST_USER}>",
        to=[recipient],
    )
    email.attach_alternative(html_message, "text/html")

    logo_path = Path(settings.BASE_DIR) / "static" / "images" / "cunnect_email_logo_black.png"
    if logo_path.is_file():
        with logo_path.open("rb") as logo_file:
            logo = MIMEImage(logo_file.read(), _subtype="png")
        logo.add_header("Content-ID", "<cunnect-email-logo>")
        logo.add_header("Content-Disposition", "inline", filename="cunnect_email_logo_black.png")
        email.attach(logo)

    email.send(fail_silently=False)


# ====================== STUDENT LOGIN ======================

def login_step1(request):
    # Existing authenticated session: do not ask the user to login again.
    if request.user.is_authenticated:
        try:
            vendor = request.user.vendor_profile
            if vendor.is_approved and vendor.is_active:
                if vendor.vendor_type == "printout":
                    return redirect("print_vendor_dashboard")
                return redirect("vendor_dashboard")
        except VendorProfile.DoesNotExist:
            pass

        try:
            delivery_profile = request.user.delivery_profile
            if delivery_profile.is_approved and delivery_profile.is_active:
                return redirect("delivery_dashboard")
        except DeliveryProfile.DoesNotExist:
            pass

        return redirect("dashboard")

    bg = "images/CU.mp4"

    if request.method == "POST":
        user_id = request.POST.get("user_id", "").strip()

        if user_id:
            if not User.objects.filter(username=user_id).exists():
                messages.error(request, "User not registered. Please register first.")
                return redirect("register")

            request.session["user_id"] = user_id
            return redirect("login_step2")

    return render(request, "login_step1.html", {"bg": bg})


def login_step2(request):
    # Fixed background. The template must not rotate videos with setInterval().
    bg = "images/CU.mp4"

    user_id = request.session.get("user_id")

    if not user_id:
        return redirect("login_step1")

    if request.method == "POST":
        password = request.POST.get("password", "")
        captcha = request.POST.get("captcha", "")
        correct_captcha = request.session.get("captcha", "")

        try:
            user = User.objects.get(username=user_id)

            if user.check_password(password) and captcha == correct_captcha:
                auth_login(request, user)
                request.session.pop("user_id", None)
                request.session.pop("captcha", None)

                profile, _ = UserProfile.objects.get_or_create(user=user)

                if profile.phone and profile.branch:
                    return redirect("dashboard")

                return redirect("login_step3")

            error = "Invalid Password or Captcha"

        except User.DoesNotExist:
            error = "User not found"

        captcha = "".join(
            random.choices(string.ascii_letters + string.digits, k=4)
        )
        request.session["captcha"] = captcha

        return render(request, "login_step2.html", {
            "user_id": user_id,
            "captcha": captcha,
            "error": error,
        })

    captcha = "".join(
        random.choices(string.ascii_letters + string.digits, k=4)
    )
    request.session["captcha"] = captcha

    return render(request, "login_step2.html", {
        "user_id": user_id,
        "captcha": captcha,
    })


# ====================== REGISTER / OTP ======================

def register(request):
    if request.method == "POST":
        full_name = request.POST.get("full_name", "").strip()
        user_id = request.POST.get("user_id", "").strip()
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")

        # Registration is allowed only with the official campus email domain.
        if not email.lower().endswith("@culkomail.in"):
            messages.error(
                request,
                "Please use your official CULKO email ID ending with @culkomail.in. "
                "Example: 25lbcs3056@culkomail.in",
            )
            return render(request, "register.html")

        if User.objects.filter(username=user_id).exists():
            messages.error(request, "User ID already exists!")
            return render(request, "register.html")

        if User.objects.filter(email=email).exists():
            messages.error(request, "Email already registered!")
            return render(request, "register.html")

        otp = UserProfile.generate_otp()

        request.session["temp_user_data"] = {
            "full_name": full_name,
            "user_id": user_id,
            "email": email,
            "password": password,
            "otp": otp,
            "otp_created_at": time.time(),
        }

        try:
            send_cunnect_otp_email(
                recipient=email,
                otp=otp,
                full_name=full_name,
                user_id=user_id,
            )
            messages.success(request, "OTP has been sent to your email.")
            return redirect("otp_verify")

        except Exception:
            messages.error(request, "Error sending email. Please try again.")

    return render(request, "register.html")


def otp_verify(request):
    temp_data = request.session.get("temp_user_data")

    if not temp_data:
        messages.error(request, "Session expired. Please register again.")
        return redirect("register")

    if request.method == "POST":
        entered_otp = request.POST.get("otp", "").strip()
        otp_created_at = float(temp_data.get("otp_created_at", 0))

        if not otp_created_at or time.time() - otp_created_at > OTP_VALID_SECONDS:
            request.session.pop("temp_user_data", None)
            messages.error(request, "OTP expired after 5 minutes. Please register again.")
            return redirect("register")

        if temp_data["otp"] == entered_otp:
            try:
                user = User.objects.create_user(
                    username=temp_data["user_id"],
                    email=temp_data["email"],
                    password=temp_data["password"],
                    first_name=temp_data["full_name"],
                )

                UserProfile.objects.create(
                    user=user,
                    is_verified=True
                )

                del request.session["temp_user_data"]
                messages.success(
                    request,
                    "Registration successful! You can now login."
                )
                return redirect("login_step1")

            except Exception as error:
                messages.error(request, f"Database error: {error}")

        else:
            messages.error(request, "Invalid OTP. Please try again.")

    return render(request, "otp_verify.html")


def resend_otp(request):
    temp_data = request.session.get("temp_user_data")

    if not temp_data:
        return redirect("register")

    new_otp = UserProfile.generate_otp()
    temp_data["otp"] = new_otp
    temp_data["otp_created_at"] = time.time()
    request.session["temp_user_data"] = temp_data
    request.session.modified = True

    try:
        send_cunnect_otp_email(
            recipient=temp_data["email"],
            otp=new_otp,
            full_name=temp_data.get("full_name", ""),
            user_id=temp_data.get("user_id", ""),
            is_resend=True,
        )
        messages.success(request, "New OTP has been sent.")

    except Exception:
        messages.error(request, "Failed to resend OTP.")

    return redirect("otp_verify")


# ====================== STUDENT DASHBOARD ======================

def user_logout(request):
    logout(request)
    request.session.flush()
    return redirect("login_step1")


def dashboard(request):
    if not request.user.is_authenticated:
        return redirect("login_step1")

    banners = Banner.objects.filter(is_active=True)
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    display_name = (
        getattr(profile, "full_name", "")
        or request.user.first_name
        or request.user.username
    )

    return render(request, "dashboard.html", {
        "user": request.user,
        "profile": profile,
        "display_name": display_name,
        "banners": banners,
    })


@login_required(login_url="login_step1")
def login_step3(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if profile.phone and profile.branch:
        return redirect("dashboard")

    if request.method == "POST":
        full_name = request.POST.get("full_name", "").strip()

        if hasattr(profile, "full_name"):
            profile.full_name = full_name

        if full_name:
            request.user.first_name = full_name
            request.user.save(update_fields=["first_name"])

        profile.phone = request.POST.get("phone", "").strip()
        profile.dob = request.POST.get("dob") or None
        profile.gender = request.POST.get("gender", "").strip()
        profile.branch = request.POST.get("branch", "").strip()
        profile.year = request.POST.get("year", "").strip()
        profile.stay_type = request.POST.get("stay_type", "").strip()
        profile.consent = request.POST.get("consent") == "on"

        if request.FILES.get("profile_photo"):
            profile.profile_photo = request.FILES["profile_photo"]

        profile.save()
        messages.success(request, "Profile completed successfully!")
        return redirect("dashboard")

    return render(request, "login_step3.html", {"profile": profile})


# ====================== CHAT ======================

@login_required(login_url="login_step1")
def chat_home(request):
    rooms = ChatRoom.objects.all()
    return render(request, "chat_home.html", {"rooms": rooms})


@login_required(login_url="login_step1")
def chat_room(request, room_name):
    room = get_object_or_404(ChatRoom, name=room_name)
    chat_messages = ChatMessage.objects.filter(room=room)

    return render(request, "chat_room.html", {
        "room": room,
        "messages": chat_messages,
    })


# ====================== VENDOR AUTHORIZATION ======================

def vendor_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, "Please login with an approved vendor account.")
            return redirect("vendor_login")

        try:
            vendor = request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            messages.error(request, "This account is not registered as a vendor.")
            return redirect("vendor_login")

        if not vendor.is_approved:
            messages.error(request, "Your vendor account is waiting for admin approval.")
            return redirect("vendor_login")

        if not vendor.is_active:
            messages.error(request, "Your vendor account is inactive. Contact support.")
            return redirect("vendor_login")

        return view_func(request, *args, **kwargs)

    return wrapper


def vendor_login(request):
    if request.user.is_authenticated:
        try:
            profile = request.user.vendor_profile
            if profile.is_approved and profile.is_active:
                if getattr(profile, "vendor_type", "food") == "printout":
                    return redirect("print_vendor_dashboard")
                return redirect("vendor_dashboard")
        except VendorProfile.DoesNotExist:
            pass

    if request.method == "POST":
        phone = request.POST.get("phone", "").strip()
        password = request.POST.get("password", "")

        try:
            vendor = VendorProfile.objects.select_related("user").get(phone=phone)
        except VendorProfile.DoesNotExist:
            messages.error(request, "Invalid mobile number or password.")
            return redirect("vendor_login")

        user = authenticate(
            request,
            username=vendor.user.username,
            password=password
        )

        if user is None:
            messages.error(request, "Invalid mobile number or password.")
            return redirect("vendor_login")

        if not vendor.is_approved:
            messages.error(request, "Your vendor account is waiting for admin approval.")
            return redirect("vendor_login")

        if not vendor.is_active:
            messages.error(request, "Your vendor account is inactive. Contact support.")
            return redirect("vendor_login")

        auth_login(request, user)

        if getattr(vendor, "vendor_type", "food") == "printout":
            return redirect("print_vendor_dashboard")

        return redirect("vendor_dashboard")

    return render(request, "food/vendor_login.html")


def _print_orders_signature(orders):
    """Create a lightweight snapshot of print-order state for live polling."""
    return "|".join(
        f"{order.id}:{order.status}:{order.final_amount}:{order.updated_at.isoformat()}"
        for order in sorted(orders, key=lambda item: item.id)
    )


@login_required(login_url="login_step1")
def print_realtime_api(request):
    """Return the current print-order signature for student/vendor live updates."""
    scope = request.GET.get("scope", "student")

    if scope == "vendor":
        try:
            vendor = request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return JsonResponse(
                {"success": False, "message": "Vendor profile not found."},
                status=403,
            )

        if getattr(vendor, "vendor_type", "food") != "printout":
            return JsonResponse(
                {"success": False, "message": "This is not a printout vendor."},
                status=403,
            )

        orders = list(
            PrintOrder.objects.filter(vendor=vendor).only(
                "id", "status", "final_amount", "updated_at"
            )
        )

        return JsonResponse({
            "success": True,
            "scope": "vendor",
            "signature": _print_orders_signature(orders),
            "order_count": len(orders),
        })

    if scope == "student":
        orders = list(
            PrintOrder.objects.filter(student=request.user).only(
                "id", "status", "final_amount", "updated_at"
            )
        )

        return JsonResponse({
            "success": True,
            "scope": "student",
            "signature": _print_orders_signature(orders),
            "order_count": len(orders),
        })

    return JsonResponse(
        {"success": False, "message": "Invalid realtime scope."},
        status=400,
    )


def _food_vendor_orders_signature(orders):
    """Create a snapshot of food-order state for vendor dashboard live polling."""
    return "|".join(
        f"{order.id}:{order.status}:{order.total_amount}:{order.updated_at.isoformat()}"
        for order in sorted(orders, key=lambda item: item.id)
    )


def _food_vendor_menu_signature(menu_items):
    """Track this vendor's available/unavailable menu items for live refresh."""
    return "|".join(
        f"{item.id}:{int(item.is_available)}"
        for item in sorted(menu_items, key=lambda item: item.id)
    )


def _food_vendor_dashboard_signature(orders, menu_items):
    return (
        _food_vendor_orders_signature(orders)
        + "#"
        + _food_vendor_menu_signature(menu_items)
    )


@vendor_required
def vendor_orders_realtime_api(request):
    """Return food vendor order signature. Used by vendor dashboard every 3 seconds."""
    vendor = request.user.vendor_profile

    if getattr(vendor, "vendor_type", "food") != "food":
        return JsonResponse(
            {"success": False, "message": "This endpoint is only for food vendors."},
            status=403,
        )

    orders = list(
        Order.objects.filter(vendor=vendor).only(
            "id", "status", "total_amount", "updated_at"
        )
    )
    menu_items = list(
        FoodItem.objects.filter(vendor=vendor).only("id", "is_available")
    )

    return JsonResponse({
        "success": True,
        "signature": _food_vendor_dashboard_signature(orders, menu_items),
        "order_count": len(orders),
    })


@login_required(login_url="login_step1")
def printout_home(request):
    print_vendors = VendorProfile.objects.filter(
        vendor_type="printout",
        is_approved=True,
        is_active=True
    ).select_related("user").order_by("business_name")

    return render(request, "food/printout_home.html", {
        "print_vendors": print_vendors,
    })


def parse_print_page_ranges(raw_value, total_pages):
    """Convert values such as '1-4, 8, 10-12' into a unique page-number set."""
    raw_value = (raw_value or "").strip()

    if not raw_value:
        return set()

    pages = set()

    for part in raw_value.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            bits = [value.strip() for value in part.split("-", 1)]
            if len(bits) != 2:
                raise ValueError
            start = int(bits[0])
            end = int(bits[1])
        else:
            start = end = int(part)

        if start < 1 or end < start or end > total_pages:
            raise ValueError

        pages.update(range(start, end + 1))

    return pages


@login_required(login_url="login_step1")
def print_vendor_shop(request, vendor_id):
    vendor = get_object_or_404(
        VendorProfile,
        id=vendor_id,
        vendor_type="printout",
        is_approved=True,
        is_active=True
    )

    if request.method == "POST":
        document = request.FILES.get("document")
        copies = request.POST.get("copies", "1")
        bw_page_ranges = request.POST.get("bw_page_ranges", "")
        color_page_ranges = request.POST.get("color_page_ranges", "")
        paper_size = "a4"
        print_side = request.POST.get("print_side", "single")
        notes = request.POST.get("notes", "").strip()

        if print_side not in {"single", "double"}:
            print_side = "single"

        if not document:
            messages.error(request, "Please upload a PDF or image document.")
            return redirect("print_vendor_shop", vendor_id=vendor.id)

        if document.size > 20 * 1024 * 1024:
            messages.error(request, "Document must be 20 MB or smaller.")
            return redirect("print_vendor_shop", vendor_id=vendor.id)

        file_name = document.name.lower()
        is_pdf = file_name.endswith(".pdf")
        is_image = file_name.endswith((".jpg", ".jpeg", ".png", ".webp"))

        if not is_pdf and not is_image:
            messages.error(request, "Only PDF, JPG, JPEG, PNG, and WebP files are allowed.")
            return redirect("print_vendor_shop", vendor_id=vendor.id)

        try:
            copies = int(copies)
            if copies < 1:
                raise ValueError
        except (TypeError, ValueError):
            messages.error(request, "Copies must be a valid number.")
            return redirect("print_vendor_shop", vendor_id=vendor.id)

        if is_image:
            pages = 1
        else:
            if PdfReader is None:
                messages.error(request, "Run this once in terminal: pip install pypdf")
                return redirect("print_vendor_shop", vendor_id=vendor.id)

            try:
                reader = PdfReader(document)
                pages = len(reader.pages)
                document.seek(0)
                if pages < 1:
                    raise ValueError
            except Exception:
                messages.error(request, "This PDF could not be read. Please upload a valid PDF.")
                return redirect("print_vendor_shop", vendor_id=vendor.id)

        try:
            bw_page_set = parse_print_page_ranges(bw_page_ranges, pages)
            color_page_set = parse_print_page_ranges(color_page_ranges, pages)
        except (TypeError, ValueError):
            messages.error(
                request,
                "Use valid page ranges, for example: 1-4, 8 or 5-7."
            )
            return redirect("print_vendor_shop", vendor_id=vendor.id)

        if bw_page_set.intersection(color_page_set):
            messages.error(request, "The same page cannot be selected for both B&W and Color.")
            return redirect("print_vendor_shop", vendor_id=vendor.id)

        if bw_page_set.union(color_page_set) != set(range(1, pages + 1)):
            messages.error(
                request,
                f"Assign every document page from 1 to {pages} to B&W or Color."
            )
            return redirect("print_vendor_shop", vendor_id=vendor.id)

        bw_pages = len(bw_page_set)
        color_pages = len(color_page_set)

        if bw_pages and color_pages:
            print_type = "mixed"
        elif color_pages:
            print_type = "color"
        else:
            print_type = "bw"

        final_amount = (
            Decimal(bw_pages * copies) * vendor.bw_price_per_page
            + Decimal(color_pages * copies) * vendor.color_price_per_page
        ).quantize(Decimal("0.01"))

        if final_amount > Decimal("999999.99"):
            messages.error(
                request,
                "This print request total is too high. Please reduce copies or contact the vendor.",
            )
            return redirect("print_vendor_shop", vendor_id=vendor.id)

        print_order = PrintOrder.objects.create(
            vendor=vendor,
            student=request.user,
            document=document,
            pages=pages,
            copies=copies,
            bw_pages=bw_pages,
            color_pages=color_pages,
            bw_page_ranges=bw_page_ranges,
            color_page_ranges=color_page_ranges,
            print_type=print_type,
            paper_size=paper_size,
            print_side=print_side,
            binding=False,
            lamination=False,
            notes=notes,
            final_amount=final_amount,
        )

        messages.success(request, "Your print request has been sent to the vendor.")
        return redirect("my_print_orders")

    return render(request, "food/print_vendor_shop.html", {
        "vendor": vendor,
    })


@login_required(login_url="login_step1")
def my_print_orders(request):
    orders = list(
        PrintOrder.objects.filter(
            student=request.user
        ).select_related("vendor")
    )

    return render(request, "food/my_print_orders.html", {
        "orders": orders,
        "print_realtime_signature": _print_orders_signature(orders),
    })


@vendor_required
def print_vendor_dashboard(request):
    vendor = request.user.vendor_profile

    if getattr(vendor, "vendor_type", "food") != "printout":
        messages.error(request, "This dashboard is only for printout vendors.")
        return redirect("vendor_dashboard")

    vendor_orders = PrintOrder.objects.filter(
        vendor=vendor
    ).select_related("student")

    pending_orders = vendor_orders.filter(status="pending")
    active_orders = vendor_orders.filter(
        status__in=["accepted", "printing", "ready"]
    )
    history_orders = vendor_orders.filter(
        status__in=["completed", "cancelled"]
    )[:10]

    today = timezone.localdate()
    today_earnings = vendor_orders.filter(
        status="completed",
        updated_at__date=today
    ).aggregate(total=Sum("final_amount"))["total"] or 0

    realtime_print_orders = list(
        PrintOrder.objects.filter(vendor=vendor).only(
            "id", "status", "final_amount", "updated_at"
        )
    )

    return render(request, "food/print_vendor_dashboard.html", {
        "vendor": vendor,
        "pending_orders": pending_orders,
        "active_orders": active_orders,
        "history_orders": history_orders,
        "today_earnings": today_earnings,
        "print_realtime_signature": _print_orders_signature(realtime_print_orders),
    })

@vendor_required
@require_POST
def print_update_prices(request):
    vendor = request.user.vendor_profile

    if getattr(vendor, "vendor_type", "food") != "printout":
        return redirect("vendor_dashboard")

    try:
        bw_price = Decimal(request.POST.get("bw_price_per_page", "0"))
        color_price = Decimal(request.POST.get("color_price_per_page", "0"))
        maximum_price = Decimal("9999.99")
        if (
            not bw_price.is_finite()
            or not color_price.is_finite()
            or bw_price < 0
            or color_price < 0
            or bw_price > maximum_price
            or color_price > maximum_price
        ):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        messages.error(
            request,
            "Enter a valid per-page price between ₹0.00 and ₹9999.99.",
        )
        return redirect("print_vendor_dashboard")

    vendor.bw_price_per_page = bw_price
    vendor.color_price_per_page = color_price
    vendor.save(update_fields=["bw_price_per_page", "color_price_per_page"])

    messages.success(request, "Print prices updated successfully.")
    return redirect("print_vendor_dashboard")

@vendor_required
@require_POST
def print_update_order(request, order_id, action):
    vendor = request.user.vendor_profile
    order = get_object_or_404(PrintOrder, id=order_id, vendor=vendor)

    transitions = {
        "pending": {"accept": "accepted", "reject": "cancelled"},
        "accepted": {"printing": "printing"},
        "printing": {"ready": "ready"},
        "ready": {"complete": "completed"},
    }

    next_status = transitions.get(order.status, {}).get(action)

    if not next_status:
        messages.error(request, "This print order status cannot be changed.")
        return redirect("print_vendor_dashboard")

    order.status = next_status
    order.save(update_fields=["status", "updated_at"])

    notification_text = {
        "accepted": (
            f"Print order #{order.id} accepted. "
            f"Total price: ₹{order.final_amount:.2f}"
        ),
        "printing": f"Print order #{order.id} is now printing.",
        "ready": f"Print order #{order.id} is ready for pickup.",
        "completed": f"Print order #{order.id} is completed.",
        "cancelled": f"Print order #{order.id} was rejected by vendor.",
    }[next_status]

    messages.success(request, notification_text)
    return redirect("print_vendor_dashboard")

@vendor_required
def vendor_profile_page(request):
    vendor = request.user.vendor_profile

    if request.method == "POST":
        business_name = request.POST.get("business_name", "").strip()
        phone = request.POST.get("phone", "").strip()

        if not business_name or not phone:
            messages.error(request, "Business name and phone number are required.")
            return redirect("vendor_profile")

        phone_exists = VendorProfile.objects.filter(
            phone=phone
        ).exclude(id=vendor.id).exists()

        if phone_exists:
            messages.error(request, "This mobile number is already used by another vendor.")
            return redirect("vendor_profile")

        vendor.business_name = business_name
        vendor.phone = phone
        vendor.save(update_fields=["business_name", "phone"])

        messages.success(request, "Vendor profile updated successfully.")
        return redirect("vendor_profile")

    return render(request, "food/vendor_profile.html", {
        "vendor": vendor,
        "user": request.user,
    })


@vendor_required
def vendor_dashboard(request):
    vendor = request.user.vendor_profile
    menu_queryset = FoodItem.objects.filter(vendor=vendor)

    vendor_orders = Order.objects.filter(
        vendor=vendor
    ).prefetch_related("items").select_related("customer")

    incoming_order_count = vendor_orders.filter(status="pending").count()
    incoming_orders = vendor_orders.filter(status="pending")[:5]

    active_order_count = vendor_orders.filter(
        status__in=["accepted", "preparing", "ready"]
    ).count()

    active_orders = vendor_orders.filter(
        status__in=["accepted", "preparing", "ready"]
    )[:8]

    order_history = vendor_orders.filter(
        status__in=["completed", "cancelled"]
    )[:10]

    # Earnings are calculated from completed/delivered orders already saved in database.
    today = timezone.localdate()
    completed_orders = vendor_orders.filter(
        status="completed",
        delivered_at__isnull=False
    )

    today_earnings = completed_orders.filter(
        delivered_at__date=today
    ).aggregate(total=Sum("total_amount"))["total"] or 0

    # Previous 7 days earning history. Missing days are saved/displayed as ₹0.
    raw_daily = completed_orders.filter(
        delivered_at__date__gte=today - timedelta(days=6)
    ).annotate(
        earning_date=TruncDate("delivered_at")
    ).values("earning_date").annotate(
        total=Sum("total_amount")
    )

    earnings_by_date = {
        row["earning_date"]: row["total"]
        for row in raw_daily
    }

    weekly_earnings = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        amount = earnings_by_date.get(day, 0)
        weekly_earnings.append({
            "date": day,
            "label": day.strftime("%d %b"),
            "amount": amount,
        })

    max_daily_earning = max(
        [item["amount"] for item in weekly_earnings] or [1]
    )

    for item in weekly_earnings:
        item["bar_height"] = max(
            5,
            int((item["amount"] / max_daily_earning) * 100)
        ) if max_daily_earning else 5

    week_total = sum(item["amount"] for item in weekly_earnings)
    today_sales = today_earnings

    realtime_food_orders = list(
        Order.objects.filter(vendor=vendor).only(
            "id", "status", "total_amount", "updated_at"
        )
    )
    realtime_menu_items = list(
        FoodItem.objects.filter(vendor=vendor).only("id", "is_available")
    )

    return render(request, "food/vendor_dashboard.html", {
        "vendor": vendor,
        "user": request.user,
        "menu_items": menu_queryset[:4],
        "menu_count": menu_queryset.count(),
        "available_item_count": menu_queryset.filter(is_available=True).count(),
        "incoming_orders": incoming_orders,
        "incoming_order_count": incoming_order_count,
        "active_orders": active_orders,
        "active_order_count": active_order_count,
        "order_history": order_history,
        "today_sales": today_sales,
        "today_earnings": today_earnings,
        "weekly_earnings": weekly_earnings,
        "week_total": week_total,
        "food_orders_signature": _food_vendor_dashboard_signature(
            realtime_food_orders,
            realtime_menu_items,
        ),
    })


@vendor_required
def vendor_earnings(request):
    vendor = request.user.vendor_profile
    current_year = timezone.localdate().year

    try:
        selected_year = int(request.GET.get("year", current_year))
    except (TypeError, ValueError):
        selected_year = current_year

    completed_orders = Order.objects.filter(
        vendor=vendor,
        status="completed",
        delivered_at__isnull=False
    )

    # Year options come from already saved completed orders.
    available_years = sorted(
        {date.year for date in completed_orders.dates("delivered_at", "year")},
        reverse=True
    )

    if current_year not in available_years:
        available_years.insert(0, current_year)

    yearly_orders = completed_orders.filter(
        delivered_at__year=selected_year
    )

    raw_months = yearly_orders.annotate(
        earning_month=TruncMonth("delivered_at")
    ).values("earning_month").annotate(
        total=Sum("total_amount"),
        orders=Count("id")
    )

    month_totals = {
        item["earning_month"].month: item["total"]
        for item in raw_months
    }

    month_orders = {
        item["earning_month"].month: item["orders"]
        for item in raw_months
    }

    month_labels = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
    ]

    monthly_sales = []
    for month_number, label in enumerate(month_labels, start=1):
        monthly_sales.append({
            "month": label,
            "amount": month_totals.get(month_number, 0),
            "orders": month_orders.get(month_number, 0),
        })

    max_month_sale = max(
        [item["amount"] for item in monthly_sales] or [1]
    )

    for item in monthly_sales:
        item["bar_height"] = max(
            5,
            int((item["amount"] / max_month_sale) * 100)
        ) if max_month_sale else 5

    yearly_total = yearly_orders.aggregate(
        total=Sum("total_amount")
    )["total"] or 0

    completed_order_count = yearly_orders.count()
    average_order_value = (
        yearly_total / completed_order_count
        if completed_order_count else 0
    )

    recent_earnings = yearly_orders.order_by(
        "-delivered_at"
    )[:12]

    return render(request, "food/vendor_earnings.html", {
        "vendor": vendor,
        "selected_year": selected_year,
        "available_years": available_years,
        "monthly_sales": monthly_sales,
        "yearly_total": yearly_total,
        "completed_order_count": completed_order_count,
        "average_order_value": average_order_value,
        "recent_earnings": recent_earnings,
    })


@vendor_required
def vendor_delivery_panel(request):
    vendor = request.user.vendor_profile

    ready_orders = Order.objects.filter(
        vendor=vendor,
        status="ready"
    ).select_related("customer")

    out_for_delivery_orders = Order.objects.filter(
        vendor=vendor,
        status="out_for_delivery",
        otp_verified=False
    ).select_related("customer")

    delivered_orders = Order.objects.filter(
        vendor=vendor,
        status="completed",
        otp_verified=True
    ).select_related("customer")[:10]

    return render(request, "food/vendor_delivery_panel.html", {
        "vendor": vendor,
        "ready_orders": ready_orders,
        "out_for_delivery_orders": out_for_delivery_orders,
        "delivered_orders": delivered_orders,
    })


@vendor_required
@require_POST
def vendor_start_delivery(request, order_id):
    vendor = request.user.vendor_profile

    order = get_object_or_404(
        Order,
        id=order_id,
        vendor=vendor,
        status="ready"
    )

    order.delivery_partner = None
    order.status = "out_for_delivery"
    order.save()

    if order.customer_id:
        Notification.objects.create(
            user=order.customer,
            order=order,
            title="Delivery is on the way",
            message=(
                f"Your order {order.order_number} is out for delivery. "
                "Open Food Dashboard to see the 4-digit delivery OTP."
            ),
        )

    messages.success(
        request,
        f"{order.order_number} marked as Out for Delivery."
    )

    return redirect("vendor_delivery_panel")


@vendor_required
@require_POST
def vendor_verify_delivery_otp(request, order_id):
    vendor = request.user.vendor_profile

    order = get_object_or_404(
        Order,
        id=order_id,
        vendor=vendor,
        status="out_for_delivery",
        otp_verified=False
    )

    entered_otp = request.POST.get("delivery_otp", "").strip()

    if entered_otp != order.delivery_otp:
        messages.error(
            request,
            "Incorrect OTP. Please ask the customer again."
        )
        return redirect("vendor_delivery_panel")

    order.otp_verified = True
    order.status = "completed"
    order.delivered_at = timezone.now()
    order.save()

    if order.customer_id:
        Notification.objects.create(
            user=order.customer,
            order=order,
            title="Order delivered successfully",
            message=f"Your order {order.order_number} was delivered and OTP verified.",
        )

    messages.success(
        request,
        f"Delivery OTP verified. {order.order_number} is completed."
    )

    return redirect("vendor_delivery_panel")


@vendor_required
@require_POST
def vendor_update_order_status(request, order_id, action):
    vendor = request.user.vendor_profile
    order = get_object_or_404(Order, id=order_id, vendor=vendor)

    allowed_transitions = {
        "pending": {
            "accept": "accepted",
            "reject": "cancelled",
        },
        "accepted": {
            "prepare": "preparing",
            "reject": "cancelled",
        },
        "preparing": {
            "ready": "ready",
        },
    }

    next_status = allowed_transitions.get(
        order.status,
        {}
    ).get(action)

    if not next_status:
        messages.error(request, "This order status cannot be changed.")
        return redirect("vendor_dashboard")

    order.status = next_status
    order.save()

    if order.customer_id:
        notification_details = {
            "accepted": (
                "Order accepted",
                f"{order.vendor.business_name} accepted your order {order.order_number}."
            ),
            "preparing": (
                "Food is being prepared",
                f"Your order {order.order_number} is now being prepared."
            ),
            "ready": (
                "Order ready for delivery",
                f"Your order {order.order_number} is ready and waiting for a delivery partner."
            ),
            "cancelled": (
                "Order cancelled",
                f"Your order {order.order_number} was cancelled by the vendor."
            ),
        }

        title, message = notification_details[next_status]
        Notification.objects.create(
            user=order.customer,
            order=order,
            title=title,
            message=message,
        )

    # Every active delivery partner sees a ready order notification.
    if next_status == "ready":
        for delivery_profile in DeliveryProfile.objects.filter(
            is_approved=True,
            is_active=True
        ).select_related("user"):
            Notification.objects.create(
                user=delivery_profile.user,
                order=order,
                title="New delivery available",
                message=(
                    f"{order.order_number} from {order.vendor.business_name} "
                    "is ready to deliver."
                ),
            )

    messages.success(
        request,
        f"{order.order_number} marked as {order.get_status_display()}."
    )

    return redirect("vendor_dashboard")


# ====================== VENDOR MENU ======================

@vendor_required
def vendor_menu(request):
    vendor = request.user.vendor_profile
    menu_items = FoodItem.objects.filter(vendor=vendor)

    return render(request, "food/vendor_menu.html", {
        "vendor": vendor,
        "menu_items": menu_items,
    })


@vendor_required
def vendor_add_item(request):
    vendor = request.user.vendor_profile

    if request.method == "POST":
        form = VendorFoodItemForm(request.POST, request.FILES)

        if form.is_valid():
            food_item = form.save(commit=False)
            food_item.vendor = vendor
            food_item.save()

            messages.success(request, f"{food_item.name} added to your menu.")
            return redirect("vendor_menu")

    else:
        form = VendorFoodItemForm()

    return render(request, "food/vendor_item_form.html", {
        "vendor": vendor,
        "form": form,
        "editing": False,
    })


@vendor_required
def vendor_edit_item(request, item_id):
    vendor = request.user.vendor_profile
    food_item = get_object_or_404(FoodItem, id=item_id, vendor=vendor)

    if request.method == "POST":
        form = VendorFoodItemForm(
            request.POST,
            request.FILES,
            instance=food_item
        )

        if form.is_valid():
            form.save()
            messages.success(request, f"{food_item.name} updated successfully.")
            return redirect("vendor_menu")

    else:
        form = VendorFoodItemForm(instance=food_item)

    return render(request, "food/vendor_item_form.html", {
        "vendor": vendor,
        "form": form,
        "editing": True,
        "food_item": food_item,
    })


@vendor_required
@require_POST
def vendor_toggle_item(request, item_id):
    vendor = request.user.vendor_profile
    food_item = get_object_or_404(FoodItem, id=item_id, vendor=vendor)

    food_item.is_available = not food_item.is_available
    food_item.save()

    status = "available" if food_item.is_available else "unavailable"
    messages.success(request, f"{food_item.name} is now {status}.")

    return redirect("vendor_menu")


# ====================== KITCHEN CONTROL ======================

@vendor_required
@require_POST
def vendor_kitchen_off(request):
    """Turn off this food vendor's full kitchen in one click."""
    vendor = request.user.vendor_profile

    if getattr(vendor, "vendor_type", "food") != "food":
        messages.error(
            request,
            "Kitchen control is only available for food vendors."
        )
        return redirect("vendor_dashboard")

    updated_items = FoodItem.objects.filter(
        vendor=vendor,
        is_available=True,
    ).update(is_available=False)

    if updated_items:
        messages.success(
            request,
            f"Kitchen is OFF. {updated_items} menu item(s) are now unavailable to students.",
        )
    else:
        messages.info(
            request,
            "Kitchen is already OFF. All menu items are unavailable.",
        )

    return redirect("vendor_dashboard")


@vendor_required
@require_POST
def vendor_kitchen_on(request):
    """Turn on this food vendor's full kitchen in one click."""
    vendor = request.user.vendor_profile

    if getattr(vendor, "vendor_type", "food") != "food":
        messages.error(
            request,
            "Kitchen control is only available for food vendors."
        )
        return redirect("vendor_dashboard")

    updated_items = FoodItem.objects.filter(
        vendor=vendor,
        is_available=False,
    ).update(is_available=True)

    if updated_items:
        messages.success(
            request,
            f"Kitchen is ON. {updated_items} menu item(s) are now available to students.",
        )
    else:
        messages.info(
            request,
            "Kitchen is already ON. All menu items are available."
        )

    return redirect("vendor_dashboard")


# ====================== DELIVERY AUTHORIZATION ======================

def delivery_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, "Please login with a delivery partner account.")
            return redirect("delivery_login")

        try:
            delivery_profile = request.user.delivery_profile
        except DeliveryProfile.DoesNotExist:
            messages.error(request, "This account is not registered as a delivery partner.")
            return redirect("delivery_login")

        if not delivery_profile.is_approved:
            messages.error(request, "Your delivery account is waiting for admin approval.")
            return redirect("delivery_login")

        if not delivery_profile.is_active:
            messages.error(request, "Your delivery account is inactive. Contact support.")
            return redirect("delivery_login")

        return view_func(request, *args, **kwargs)

    return wrapper


def delivery_login(request):
    if request.user.is_authenticated:
        try:
            profile = request.user.delivery_profile
            if profile.is_approved and profile.is_active:
                return redirect("delivery_dashboard")
        except DeliveryProfile.DoesNotExist:
            pass

    if request.method == "POST":
        phone = request.POST.get("phone", "").strip()
        password = request.POST.get("password", "")

        try:
            delivery_profile = DeliveryProfile.objects.select_related("user").get(phone=phone)
        except DeliveryProfile.DoesNotExist:
            messages.error(request, "Invalid mobile number or password.")
            return redirect("delivery_login")

        user = authenticate(
            request,
            username=delivery_profile.user.username,
            password=password
        )

        if user is None:
            messages.error(request, "Invalid mobile number or password.")
            return redirect("delivery_login")

        if not delivery_profile.is_approved:
            messages.error(request, "Your delivery account is waiting for admin approval.")
            return redirect("delivery_login")

        if not delivery_profile.is_active:
            messages.error(request, "Your delivery account is inactive. Contact support.")
            return redirect("delivery_login")

        auth_login(request, user)
        return redirect("delivery_dashboard")

    return render(request, "food/delivery_login.html")


@delivery_required
def delivery_dashboard(request):
    delivery_profile = request.user.delivery_profile

    ready_orders = Order.objects.filter(
        status="ready",
        delivery_partner__isnull=True
    ).select_related("vendor").prefetch_related("items")

    active_orders = Order.objects.filter(
        delivery_partner=delivery_profile,
        status="out_for_delivery",
        otp_verified=False
    ).select_related("vendor").prefetch_related("items")

    delivery_history = Order.objects.filter(
        delivery_partner=delivery_profile,
        status="completed",
        otp_verified=True
    ).select_related("vendor")[:10]

    return render(request, "food/delivery_dashboard.html", {
        "ready_orders": ready_orders,
        "active_orders": active_orders,
        "delivery_history": delivery_history,
        "user": request.user,
    })


@delivery_required
@require_POST
def delivery_claim_order(request, order_id):
    delivery_profile = request.user.delivery_profile

    order = get_object_or_404(
        Order,
        id=order_id,
        status="ready",
        delivery_partner__isnull=True
    )

    order.delivery_partner = delivery_profile
    order.status = "out_for_delivery"
    order.save()

    if order.customer_id:
        Notification.objects.create(
            user=order.customer,
            order=order,
            title="Delivery partner is on the way",
            message=(
                f"Your order {order.order_number} is out for delivery. "
                "Open Food dashboard to view the 4-digit delivery OTP."
            ),
        )

    Notification.objects.create(
        user=order.vendor.user,
        order=order,
        title="Delivery partner assigned",
        message=(
            f"{delivery_profile.user.username} claimed order "
            f"{order.order_number}."
        ),
    )

    messages.success(
        request,
        f"{order.order_number} claimed. Ask the customer for the 4-digit OTP at delivery."
    )

    return redirect("delivery_dashboard")


@delivery_required
@require_POST
def delivery_verify_otp(request, order_id):
    delivery_profile = request.user.delivery_profile

    order = get_object_or_404(
        Order,
        id=order_id,
        delivery_partner=delivery_profile,
        status="out_for_delivery",
        otp_verified=False
    )

    entered_otp = request.POST.get("delivery_otp", "").strip()

    if entered_otp != order.delivery_otp:
        messages.error(request, "Incorrect OTP. Please ask the customer again.")
        return redirect("delivery_dashboard")

    order.otp_verified = True
    order.status = "completed"
    order.delivered_at = timezone.now()
    order.save()

    if order.customer_id:
        Notification.objects.create(
            user=order.customer,
            order=order,
            title="Order delivered successfully",
            message=f"Your order {order.order_number} was delivered and OTP verified.",
        )

    Notification.objects.create(
        user=order.vendor.user,
        order=order,
        title="Order delivered",
        message=(
            f"{order.order_number} was delivered by "
            f"{delivery_profile.user.username}."
        ),
    )

    messages.success(
        request,
        f"Delivery verified. {order.order_number} is now completed."
    )

    return redirect("delivery_dashboard")


@login_required(login_url="login_step1")
@require_POST
def support_request(request):
    subject = request.POST.get("subject", "").strip()
    message = request.POST.get("message", "").strip()

    if not subject or not message:
        messages.error(request, "Please enter subject and support message.")
        return redirect("dashboard")

    SupportRequest.objects.create(
        user=request.user,
        email=request.user.email,
        subject=subject,
        message=message,
    )

    messages.success(
        request,
        "Your support request has been sent successfully."
    )

    return redirect("dashboard")


@login_required(login_url="login_step1")
def store_home(request):
    return render(request, "store_home.html")


@login_required(login_url="login_step1")
def chat_under_construction(request):
    return render(request, "chat_under_construction.html")
