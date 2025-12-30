from django.contrib import admin
from .models import VehicleType, RatePlan, Ticket

@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "active")
    list_filter = ("active",)
    search_fields = ("name",)

@admin.register(RatePlan)
class RatePlanAdmin(admin.ModelAdmin):
    list_display = ("name", "vehicle_type", "price_cop", "billing_unit", "unit_size", "active")
    list_filter = ("vehicle_type", "billing_unit", "active")
    search_fields = ("name", "vehicle_type__name")

@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("plate", "status", "vehicle_type", "rate_plan", "check_in", "check_out", "total_amount_cop")
    list_filter = ("status", "vehicle_type")
    search_fields = ("plate",)
