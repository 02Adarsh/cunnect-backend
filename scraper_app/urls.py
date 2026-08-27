from django.urls import path
from . import views

app_name = 'scraper_app'

urlpatterns = [
    path('', views.login_view, name='login'),
    path('authenticate/', views.authenticate_view, name='authenticate'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('demo/', views.demo_view, name='demo'),
    path("fees/receipt/<str:receipt_id>/", views.fee_receipt_view, name="fee_receipt"),
    path("dashboard/data/", views.dashboard_data_view, name="dashboard_data"),
    path("course-plan/<int:course_index>/", views.course_plan_pdf_view, name="course_plan_pdf"),
    path("logout/", views.logout_view, name="logout"),
    path("profile-photo/", views.profile_photo_view, name="profile_photo"),
    path("idcard/", views.id_card_upload_view, name="id_card"),
    path("idcard/image/", views.id_card_image_view, name="id_card_image"),
    path("idcard/remove/", views.id_card_remove_view, name="id_card_remove"),
]
