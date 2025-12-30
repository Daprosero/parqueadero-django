from django.urls import path
from . import views

urlpatterns = [
    path("operario/ingreso/", views.operator_checkin, name="operator_checkin"),
    path("operario/cobro/", views.operator_charge, name="operator_charge"),
]
