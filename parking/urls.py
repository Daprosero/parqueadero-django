from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = "parking"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(next_page="parking:login"),
        name="logout",
    ),

    # (Opcional) si lo usas como inicio:
    # path("", views.home, name="home"),

    path("redirect/", views.post_login_redirect, name="post_login_redirect"),

    path("operario/", views.operator_panel, name="operator_panel"),

    # ===== EDICIÓN / ACCIONES TICKETS =====
    path("ticket/<int:ticket_id>/edit-active/", views.ticket_edit_active, name="ticket_edit_active"),
    path("ticket/<int:ticket_id>/delete-active/", views.ticket_delete_active, name="ticket_delete_active"),
    path("ticket/<int:ticket_id>/edit-paid/", views.ticket_edit_paid, name="ticket_edit_paid"),

    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),

    path("closure/generate/", views.generate_closure, name="generate_closure"),

    path("payment/<int:payment_id>/receipt/", views.print_receipt, name="print_receipt"),

    path("closure/<int:closure_id>/reprint/", views.reprint_closure, name="reprint_closure"),
    # ===== EDITAR CIERRES =====
    path("closure/edit/", views.closure_edit_panel, name="closure_edit_panel"),
    path("closure/<int:closure_id>/edit/", views.closure_edit_detail, name="closure_edit_detail"),
    path("closure/<int:closure_id>/recalc/", views.closure_recalc, name="closure_recalc"),

    # ===== GESTIÓN =====
    path("gestion/", views.gestion, name="gestion"),

    # ===== RECIBO EMPRESA (SESSION) =====
    path("gestion/recibo-empresa/", views.company_settle_receipt, name="company_settle_receipt"),

    # ===== CLIENTES MENSUALES =====
    path("gestion/monthly/check/", views.monthly_check, name="monthly_check"),
    path("gestion/monthly/add/", views.monthly_add, name="monthly_add"),
    path("gestion/monthly/delete/<int:pk>/", views.monthly_delete, name="monthly_delete"),
    path("gestion/monthly/charge/<int:pk>/", views.monthly_charge, name="monthly_charge"),
    path("gestion/monthly/receipt/", views.monthly_receipt, name="monthly_receipt"),

    # ===== REPORTES =====
    path("reports/active-vehicles/", views.active_vehicles_report, name="active_vehicles_report"),
]
