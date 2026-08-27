from django.urls import path
from . import views
from django.contrib.auth import views as auth_views


urlpatterns = [
    path('', views.login_step1, name='login_step1'),
    path('step2/', views.login_step2, name='login_step2'),
    path('register/', views.register, name='register'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('otp-verify/', views.otp_verify, name='otp_verify'),
    path('resend-otp/', views.resend_otp, name='resend_otp'),
    path('step3/', views.login_step3, name='login_step3'), 
    path("logout/", views.user_logout, name="user_logout"),
    path(
        "support/",
        views.support_request,
        name="support_request"
    ),
 

    
   
]