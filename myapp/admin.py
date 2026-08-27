from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import UserProfile
from .models import UserProfile, ChatRoom, ChatMessage
from .models import Banner
from .models import VendorProfile
from .models import DeliveryProfile
from django.contrib import admin
from .models import VendorProfile, DeliveryProfile
from .models import SupportRequest
from .models import PrintOrder

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'branch', 'year', 'stay_type', 'is_verified')
    list_filter = ('is_verified', 'stay_type', 'year')
    search_fields = ('user__username', 'phone', 'branch')

#====================== ChatRoom ======================#
@admin.register(ChatRoom)
class ChatRoomAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at')

# ====================== ChatMessage ======================
@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ('user', 'room', 'message', 'timestamp')
    list_filter = ('room',)


@admin.register(Banner)
class BannerAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "is_active",
        "order",
    )

    list_editable = (
        "is_active",
        "order",
    )

    search_fields = (
        "title",
    )

@admin.register(VendorProfile)
class VendorProfileAdmin(admin.ModelAdmin):
    list_display = (
        "business_name",
        "vendor_type",
        "user",
        "phone",
        "is_approved",
        "is_active",
    )

    list_filter = (
        "vendor_type",
        "is_approved",
        "is_active",
    )

    search_fields = (
        "business_name",
        "user__username",
        "phone",
    )

@admin.register(DeliveryProfile)
class DeliveryProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "phone",
        "is_approved",
        "is_active",
    )

    list_filter = (
        "is_approved",
        "is_active",
    )

    search_fields = (
        "user__username",
        "phone",
    )




@admin.register(SupportRequest)
class SupportRequestAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "subject",
        "status",
        "created_at",
    )

    list_filter = (
        "status",
        "created_at",
    )

    search_fields = (
        "user__username",
        "email",
        "subject",
        "message",
    )

# Add to myapp/admin.py



@admin.register(PrintOrder)
class PrintOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "vendor",
        "student",
        "pages",
        "copies",
        "print_type",
        "status",
        "final_amount",
        "created_at",
    )

    list_filter = (
        "status",
        "print_type",
        "paper_size",
        "vendor",
    )

    search_fields = (
        "vendor__business_name",
        "student__username",
    )
