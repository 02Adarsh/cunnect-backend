from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from myapp.models import UserProfile

from .models import (
    FoodItem,
    HeroSlide,
    Cart,
    CartItem,
    Coupon,
    CouponUsage,
    Order,
    OrderItem,
    Notification,
    FoodOffer,
)


def food_menu_signature():
    """Snapshot used to detect food-item availability/add/remove changes."""
    return "|".join(
        f"{item_id}:{int(is_available)}"
        for item_id, is_available in FoodItem.objects.order_by("id").values_list(
            "id", "is_available"
        )
    )


def food_menu_realtime_api(request):
    """Public menu availability snapshot for the student Food page."""
    return JsonResponse({
        "success": True,
        "signature": food_menu_signature(),
    })


def get_cart(request):
    if not request.session.session_key:
        request.session.create()

    cart, _ = Cart.objects.get_or_create(
        session_key=request.session.session_key
    )
    return cart


def get_cart_summary(cart):
    cart_items = CartItem.objects.filter(cart=cart).select_related("food_item")
    cart_count = sum(item.quantity for item in cart_items)
    cart_total = sum(
        (item.subtotal for item in cart_items),
        Decimal("0.00")
    )
    return cart_items, cart_count, cart_total


def current_customer_has_used_coupon(request, coupon):
    user = getattr(request, "user", None)

    if user and user.is_authenticated:
        return CouponUsage.objects.filter(coupon=coupon, user=user).exists()

    return CouponUsage.objects.filter(
        coupon=coupon,
        session_key=request.session.session_key
    ).exists()


def mark_coupon_as_used(request, coupon):
    user = getattr(request, "user", None)

    if user and user.is_authenticated:
        CouponUsage.objects.get_or_create(
            coupon=coupon,
            user=user,
            defaults={"session_key": request.session.session_key}
        )
    else:
        CouponUsage.objects.get_or_create(
            coupon=coupon,
            session_key=request.session.session_key
        )


def get_available_coupons(request, cart_total):
    now = timezone.now()
    available_coupons = []

    for coupon in Coupon.objects.filter(is_active=True):
        if coupon.valid_from and coupon.valid_from > now:
            continue
        if coupon.valid_until and coupon.valid_until < now:
            continue
        if cart_total < coupon.minimum_order_value:
            continue
        if coupon.one_time_per_user and current_customer_has_used_coupon(request, coupon):
            continue
        available_coupons.append(coupon)

    return available_coupons


def get_applied_coupon(request, cart_total):
    code = request.session.get("coupon_code")
    if not code:
        return None, Decimal("0.00")

    coupon = Coupon.objects.filter(code__iexact=code, is_active=True).first()
    now = timezone.now()

    if (
        not coupon
        or (coupon.valid_from and coupon.valid_from > now)
        or (coupon.valid_until and coupon.valid_until < now)
        or cart_total < coupon.minimum_order_value
    ):
        request.session.pop("coupon_code", None)
        return None, Decimal("0.00")

    return coupon, coupon.discount_for(cart_total)


def set_coupon_notice(request, notice_type, text):
    if notice_type == "success":
        messages.success(request, text)
    else:
        messages.error(request, text)


@require_POST
def apply_coupon(request):
    cart = get_cart(request)
    _, _, cart_total = get_cart_summary(cart)
    code = request.POST.get("coupon", "").strip().upper()

    coupon = Coupon.objects.filter(code__iexact=code, is_active=True).first()
    now = timezone.now()

    if not code:
        set_coupon_notice(request, "error", "Please enter a coupon code.")
    elif not coupon:
        set_coupon_notice(request, "error", "This coupon code is not valid.")
    elif coupon.valid_from and coupon.valid_from > now:
        set_coupon_notice(request, "error", "This coupon is not active yet.")
    elif coupon.valid_until and coupon.valid_until < now:
        set_coupon_notice(request, "error", "This coupon has expired.")
    elif cart_total < coupon.minimum_order_value:
        set_coupon_notice(
            request,
            "error",
            f"Minimum order amount for this coupon is ₹{coupon.minimum_order_value:.0f}."
        )
    elif coupon.one_time_per_user and current_customer_has_used_coupon(request, coupon):
        set_coupon_notice(request, "error", "You have already used this coupon.")
    else:
        request.session["coupon_code"] = coupon.code
        discount = coupon.discount_for(cart_total)

        # One-time coupons are marked used after a successful Apply action.
        if coupon.one_time_per_user:
            mark_coupon_as_used(request, coupon)

        messages.success(
            request,
            f"{coupon.code} applied — you save ₹{discount:.0f}.",
            extra_tags="coupon-applied"
        )

    return redirect("checkout_view")


@require_POST
def remove_coupon(request):
    request.session.pop("coupon_code", None)
    set_coupon_notice(request, "success", "Coupon removed.")
    return redirect("checkout_view")


@require_POST
def add_to_cart(request, food_id):
    cart = get_cart(request)
    food_item = get_object_or_404(
        FoodItem,
        id=food_id,
        is_available=True
    )

    cart_item, created = CartItem.objects.get_or_create(
        cart=cart,
        food_item=food_item,
        defaults={"quantity": 1}
    )

    if not created:
        cart_item.quantity += 1
        cart_item.save()

    return redirect("food_home")


def cart_view(request):
    cart = get_cart(request)
    cart_items, cart_count, cart_total = get_cart_summary(cart)

    return render(request, "food/cart.html", {
        "cart_items": cart_items,
        "cart_count": cart_count,
        "cart_total": cart_total,
    })


@require_POST
def update_cart(request, item_id, action):
    cart = get_cart(request)
    cart_item = get_object_or_404(CartItem, id=item_id, cart=cart)

    if action == "increase":
        cart_item.quantity += 1
        cart_item.save()

    elif action == "decrease":
        if cart_item.quantity > 1:
            cart_item.quantity -= 1
            cart_item.save()
        else:
            cart_item.delete()

    elif action == "remove":
        cart_item.delete()

    return redirect("cart_view")


def checkout_view(request):
    cart = get_cart(request)
    cart_items, cart_count, cart_total = get_cart_summary(cart)
    applied_coupon, coupon_discount = get_applied_coupon(request, cart_total)
    available_coupons = get_available_coupons(request, cart_total)
    final_total = max(Decimal("0.00"), cart_total - coupon_discount)

    customer_name = ""
    customer_phone = ""

    if request.user.is_authenticated:
        customer_name = (
            request.user.get_full_name().strip()
            or request.user.first_name
            or request.user.username
        )

        profile = UserProfile.objects.filter(user=request.user).first()
        if profile:
            customer_phone = profile.phone or ""

    return render(request, "food/checkout.html", {
        "cart_items": cart_items,
        "cart_count": cart_count,
        "cart_total": cart_total,
        "applied_coupon": applied_coupon,
        "coupon_discount": coupon_discount,
        "available_coupons": available_coupons,
        "final_total": final_total,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
    })


@require_POST
def place_order(request):
    if not request.user.is_authenticated:
        messages.error(request, "Please login before placing an order.")
        return redirect("login_step1")

    cart = get_cart(request)
    cart_items, _, cart_total = get_cart_summary(cart)

    if not cart_items:
        messages.error(request, "Your cart is empty.")
        return redirect("cart_view")

    # Name and phone are always taken from the logged-in student's backend profile.
    customer_name = (
        request.user.get_full_name().strip()
        or request.user.first_name
        or request.user.username
    )

    profile = UserProfile.objects.filter(user=request.user).first()
    customer_phone = profile.phone if profile and profile.phone else ""

    delivery_address = request.POST.get("delivery_address", "").strip()
    landmark = request.POST.get("landmark", "").strip()
    order_note = request.POST.get("order_note", "").strip()
    payment_method = request.POST.get("payment", "cash")

    if not customer_phone:
        messages.error(
            request,
            "Please complete your phone number in your profile before placing an order."
        )
        return redirect("login_step3")

    if not delivery_address:
        messages.error(request, "Please provide a delivery address.")
        return redirect("checkout_view")

    # Real UPI gateway is not connected yet. Cash orders can be placed now.
    if payment_method == "upi":
        messages.error(request, "UPI payment is not active yet. Please choose Pay at Counter.")
        return redirect("checkout_view")

    vendor_groups = defaultdict(list)

    for cart_item in cart_items:
        food_item = cart_item.food_item

        if not food_item.is_available:
            messages.error(request, f"{food_item.name} is unavailable now. Please update your cart.")
            return redirect("cart_view")

        if not food_item.vendor:
            messages.error(request, f"{food_item.name} has no vendor assigned yet.")
            return redirect("cart_view")

        vendor_groups[food_item.vendor].append(cart_item)

    applied_coupon, coupon_discount = get_applied_coupon(request, cart_total)
    created_orders = []
    remaining_discount = coupon_discount
    vendor_groups_list = list(vendor_groups.items())

    for index, (vendor, items) in enumerate(vendor_groups_list):
        vendor_subtotal = sum(
            (item.subtotal for item in items),
            Decimal("0.00")
        )

        # Coupon discount is divided fairly if cart has multiple vendors.
        if index == len(vendor_groups_list) - 1:
            vendor_discount = remaining_discount
        else:
            vendor_discount = (
                coupon_discount * vendor_subtotal / cart_total
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            remaining_discount -= vendor_discount

        order = Order.objects.create(
            vendor=vendor,
            customer=request.user if request.user.is_authenticated else None,
            customer_name=customer_name,
            customer_phone=customer_phone,
            delivery_address=delivery_address,
            landmark=landmark,
            payment_method=payment_method,
            payment_status="pending",
            status="pending",
            subtotal=vendor_subtotal,
            discount=vendor_discount,
            total_amount=max(Decimal("0.00"), vendor_subtotal - vendor_discount),
            order_note=order_note,
        )

        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                food_item=item.food_item,
                item_name=item.food_item.name,
                price=item.food_item.price,
                quantity=item.quantity,
            )
            for item in items
        ])

        created_orders.append(order.order_number)

        # Vendor gets a live alert for every new customer order.
        Notification.objects.create(
            user=vendor.user,
            order=order,
            title="New food order received",
            message=(
                f"{order.order_number} from {customer_name} "
                f"for ₹{order.total_amount:.0f} is waiting for acceptance."
            ),
        )

    CartItem.objects.filter(cart=cart).delete()
    request.session.pop("coupon_code", None)
    request.session["recent_order_numbers"] = created_orders

    # AJAX checkout receives JSON. Browser then opens Order Success once,
    # so session order numbers are not consumed by a background fetch.
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({
            "ok": True,
            "redirect_url": reverse("order_success"),
        })

    return redirect("order_success")


def order_success(request):
    order_numbers = request.session.pop("recent_order_numbers", [])

    if not order_numbers:
        return redirect("food_home")

    orders = Order.objects.filter(order_number__in=order_numbers).select_related("vendor")

    return render(request, "food/order_success.html", {
        "orders": orders,
    })


@login_required(login_url="login_step1")
def my_orders(request):
    orders = Order.objects.filter(
        customer=request.user
    ).select_related(
        "vendor"
    ).prefetch_related(
        "items"
    )

    return render(request, "food/my_orders.html", {
        "orders": orders,
    })


@login_required(login_url="login_step1")
def notifications_page(request):
    notifications = Notification.objects.filter(
        user=request.user
    ).select_related("order")

    Notification.objects.filter(
        user=request.user,
        is_read=False
    ).update(is_read=True)

    return render(request, "food/notifications.html", {
        "notifications": notifications,
    })


@login_required(login_url="login_step1")
def notifications_api(request):
    unread_notifications = Notification.objects.filter(
        user=request.user,
        is_read=False
    )[:10]

    data = []
    for notification in unread_notifications:
        data.append({
            "id": notification.id,
            "title": notification.title,
            "message": notification.message,
            "created_at": notification.created_at.strftime("%I:%M %p"),
        })

    return JsonResponse({
        "unread_count": Notification.objects.filter(
            user=request.user,
            is_read=False
        ).count(),
        "notifications": data,
    })


@login_required(login_url="login_step1")
def my_orders_status_api(request):
    orders = Order.objects.filter(
        customer=request.user
    ).select_related("vendor")

    data = []
    for order in orders:
        data.append({
            "id": order.id,
            "order_number": order.order_number,
            "vendor_name": order.vendor.business_name,
            "status": order.status,
            "status_label": order.get_status_display(),
            "delivery_otp": (
                order.delivery_otp
                if order.status == "out_for_delivery" and not order.otp_verified
                else ""
            ),
            "updated_at": order.updated_at.strftime("%I:%M %p"),
        })

    return JsonResponse({"orders": data})


def food_home(request):
    # Search and category filter are handled through URL query parameters.
    search_query = request.GET.get("q", "").strip()
    selected_category = request.GET.get("category", "").strip()

    db_items = FoodItem.objects.all()

    if search_query:
        db_items = db_items.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query)
        )

    valid_categories = dict(FoodItem.CATEGORY_CHOICES)
    if selected_category in valid_categories:
        db_items = db_items.filter(category=selected_category)
    else:
        selected_category = ""

    food_items = []
    for item in db_items:
        food_items.append({
            "id": item.id,
            "name": item.name,
            "price": float(item.price),
            "description": item.description,
            "image": item.image.url if item.image else "",
            "is_available": item.is_available,
        })

    hero_slides = HeroSlide.objects.filter(is_active=True).order_by("order")

    now = timezone.now()
    offers = FoodOffer.objects.filter(
        is_active=True
    ).filter(
        Q(valid_until__isnull=True) | Q(valid_until__gte=now)
    ).select_related("coupon", "vendor")

    cart = get_cart(request)
    _, cart_count, cart_total = get_cart_summary(cart)

    active_delivery_order = None
    if request.user.is_authenticated:
        active_delivery_order = Order.objects.filter(
            customer=request.user,
            status="out_for_delivery",
            otp_verified=False
        ).select_related("vendor").first()

    context = {
        "food_items": food_items,
        "hero_slides": hero_slides,
        "cart_count": cart_count,
        "cart_total": cart_total,
        "active_delivery_order": active_delivery_order,
        "search_query": search_query,
        "selected_category": selected_category,
        "categories": FoodItem.CATEGORY_CHOICES,
        "offers": offers,
        "food_menu_signature": food_menu_signature(),
    }
    return render(request, "food/food_home.html", context)
