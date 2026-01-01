from django.urls import path
from . import views
from django.contrib.auth import views as auth_views

urlpatterns = [
    # Redirección post-login por rol
    path("redirect/", views.post_login_redirect, name="post_login_redirect"),

    # Panel único del operario (registro / cobro)
    path("operario/", views.operator_panel, name="operator_panel"),

    # Dashboard del administrador (fuera del admin de Django)
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),

    # Logout
    path(
        "logout/",
        auth_views.LogoutView.as_view(next_page="/login/"),
        name="logout",
    ),
]
