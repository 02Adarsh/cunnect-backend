from django.urls import path

from . import views


urlpatterns = [
    # Food Home
    path("", views.food_home, name="food_home"),

    # Cart
    path("cart/", views.cart_view, name="cart_view"),
    path("cart/add/<int:food_id>/", views.add_to_cart, name="add_to_cart"),
    path(
        "cart/item/<int:item_id>/<str:action>/",
        views.update_cart,
        name="update_cart"
    ),

    # Checkout and coupon
    path("checkout/", views.checkout_view, name="checkout_view"),
    path(
        "checkout/coupon/apply/",
        views.apply_coupon,
        name="apply_coupon"
    ),
    path(
        "checkout/coupon/remove/",
        views.remove_coupon,
        name="remove_coupon"
    ),
    path(
        "checkout/place-order/",
        views.place_order,
        name="place_order"
    ),
    path(
        "order/success/",
        views.order_success,
        name="order_success"
    ),

    # Student orders
    path("my-orders/", views.my_orders, name="my_orders"),
    path(
        "my-orders/status/",
        views.my_orders_status_api,
        name="my_orders_status_api"
    ),

    # Notifications
    path(
        "notifications/",
        views.notifications_page,
        name="notifications_page"
    ),
    path(
        "notifications/api/",
        views.notifications_api,
        name="notifications_api"
    ),

    # Kitchen Off / menu availability realtime endpoint
    path(
        "menu/realtime/",
        views.food_menu_realtime_api,
        name="food_menu_realtime_api"
    ),
]
