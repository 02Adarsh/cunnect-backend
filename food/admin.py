from django.contrib import admin
from .models import FoodItem,HeroSlide
from django.contrib import admin
from .models import Coupon, CouponUsage
from .models import Notification
from .models import FoodOffer

admin.site.register(FoodItem)
@admin.register(HeroSlide)
class HeroSlideAdmin(admin.ModelAdmin):
    list_display = ("title", "order", "is_active")
    list_editable = ("order", "is_active")

@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "discount_type",
        "discount_value",
        "minimum_order_value",
        "one_time_per_user",
        "is_active",
    )
    list_filter = (
        "discount_type",
        "one_time_per_user",
        "is_active",
    )
    search_fields = ("code",)


@admin.register(CouponUsage)
class CouponUsageAdmin(admin.ModelAdmin):
    list_display = ("coupon", "user", "session_key", "used_at")
    list_filter = ("coupon",)
    readonly_fields = ("coupon", "user", "session_key", "used_at")




@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "title",
        "order",
        "is_read",
        "created_at",
    )

    list_filter = (
        "is_read",
        "created_at",
    )

@admin.register(FoodOffer)
class FoodOfferAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "vendor",
        "coupon",
        "is_active",
        "valid_until",
        "order",
    )

    list_filter = (
        "is_active",
        "vendor",
    )

    search_fields = (
        "title",
        "description",
        "coupon__code",
    )