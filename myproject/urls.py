"""
URL configuration for myproject project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path,include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views
from network import views as network_views
from myapp import views


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('api_app.urls')),
    path('', include('myapp.urls')),
    path('food/', include('food.urls')), 
    #path('network/', include('network.urls')),
    path("chat/", include("network.urls")),
    path("vendor/login/", views.vendor_login, name="vendor_login"),
    path("vendor/dashboard/", views.vendor_dashboard, name="vendor_dashboard"),
     # Password Reset URLs
    path('password-reset/', 
         auth_views.PasswordResetView.as_view(template_name='password_reset.html'), 
         name='password_reset'),

    path('password-reset/done/', 
         auth_views.PasswordResetDoneView.as_view(template_name='password_reset_done.html'), 
         name='password_reset_done'),

    path('reset/<uidb64>/<token>/', 
         auth_views.PasswordResetConfirmView.as_view(template_name='password_reset_confirm.html'), 
         name='password_reset_confirm'),

    path('reset/done/', 
         auth_views.PasswordResetCompleteView.as_view(template_name='password_reset_complete.html'), 
         name='password_reset_complete'),

    path("vendor/menu/", views.vendor_menu, name="vendor_menu"),

    path(
        "vendor/menu/add/",
        views.vendor_add_item,
        name="vendor_add_item"
    ),

    path(
        "vendor/menu/<int:item_id>/edit/",
        views.vendor_edit_item,
        name="vendor_edit_item"
    ),

    path(
        "vendor/menu/<int:item_id>/toggle/",
        views.vendor_toggle_item,
        name="vendor_toggle_item"
    ),
    path(
        "vendor/orders/<int:order_id>/<str:action>/",
        views.vendor_update_order_status,
        name="vendor_update_order_status"
    ),

    path(
        "delivery/login/",
        views.delivery_login,
        name="delivery_login"
    ),

    path(
        "delivery/dashboard/",
        views.delivery_dashboard,
        name="delivery_dashboard"
    ),

    path(
        "delivery/claim/<int:order_id>/",
        views.delivery_claim_order,
        name="delivery_claim_order"
    ),

    path(
        "delivery/verify-otp/<int:order_id>/",
        views.delivery_verify_otp,
        name="delivery_verify_otp"
    ),
    
    path(
        "vendor/delivery/",
        views.vendor_delivery_panel,
        name="vendor_delivery_panel"
    ),

    path(
        "vendor/delivery/start/<int:order_id>/",
        views.vendor_start_delivery,
        name="vendor_start_delivery"
    ),

    path(
        "vendor/delivery/verify-otp/<int:order_id>/",
        views.vendor_verify_delivery_otp,
        name="vendor_verify_delivery_otp"
    ),

    path(
        "vendor/earnings/",
        views.vendor_earnings,
        name="vendor_earnings"
    ),
    path(
        "vendor/profile/",
        views.vendor_profile_page,
        name="vendor_profile"
    ),

    path(
        "printout/",
        views.printout_home,
        name="printout_home"
    ),

    path(
        "print-vendor/dashboard/",
        views.print_vendor_dashboard,
        name="print_vendor_dashboard"
    ),

    path("printout/vendor/<int:vendor_id>/", views.print_vendor_shop, name="print_vendor_shop"),
    path("printout/my-orders/", views.my_print_orders, name="my_print_orders"),
    path("print-vendor/order/<int:order_id>/<str:action>/", views.print_update_order, name="print_update_order"),

    path(
        "vendor/orders/realtime/",
        views.vendor_orders_realtime_api,
        name="vendor_orders_realtime_api"
    ),

    path("store/", views.store_home, name="store_home"),
    path('collegia/', include('scraper_app.urls')),
    path(
        "printout/realtime/",
        views.print_realtime_api,
        name="print_realtime_api"
    ),
    path(
        "print-vendor/prices/",
        views.print_update_prices,
        name="print_update_prices"
    ),

    path(
        "vendor/kitchen-off/",
        views.vendor_kitchen_off,
        name="vendor_kitchen_off"
    ),
    path(
        "vendor/kitchen-off/",
        views.vendor_kitchen_off,
        name="vendor_kitchen_off"
    ),

    path(
        "vendor/kitchen-on/",
        views.vendor_kitchen_on,
        name="vendor_kitchen_on"
    ),
    path(
        "chat-under-construction/",
        views.chat_under_construction,
        name="chat_under_construction"
    ),
    

]

# ⭐ Production (DEBUG=False) pe bhi media serve ho — hostel cards, photos etc.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)