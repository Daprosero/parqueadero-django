from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from parking import views  # <- importa home

urlpatterns = [
    # Admin Django
    path("admin/", admin.site.urls),

    # LOGIN / LOGOUT
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="parking/login.html"),
        name="login",
    ),

    # Raíz del sistema
    path("", views.home, name="home"),

    # Rutas de la app parking
    path("", include("parking.urls")),
]
