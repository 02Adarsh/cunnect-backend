from django.contrib import admin

from .models import Ride, RideVendor


@admin.register(RideVendor)
class RideVendorAdmin(admin.ModelAdmin):
    list_display = ("vendor", "vehicle_number", "is_online", "is_auto",
                    "total_rides", "total_earnings")
    list_filter = ("is_online", "is_auto", "auto_active", "mini_active",
                   "sedan_active", "suv_active")
    search_fields = ("vendor__business_name", "vehicle_number")


@admin.register(Ride)
class RideAdmin(admin.ModelAdmin):
    list_display = ("ride_code", "student", "rider", "vehicle_type",
                    "distance_km", "total", "status", "created_at")
    list_filter = ("status", "vehicle_type", "payment_mode")
    search_fields = ("ride_code", "student__username",
                     "student_name", "student_phone")
    readonly_fields = ("ride_code", "created_at")
