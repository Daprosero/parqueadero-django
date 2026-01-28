from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse

from .models import VehicleType, RatePlan, Ticket, Payment, Customer, Closure,MonthlyPlate


@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "active")
    list_filter = ("active",)
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    # ✅ Se agrega credit_enabled para ver el “paso 2” en admin
    list_display = ("full_name", "id_number", "email", "is_company", "credit_enabled", "active")
    list_filter = ("is_company", "credit_enabled", "active")
    search_fields = ("full_name", "id_number", "email")
    ordering = ("full_name",)

    # ✅ Para que en el formulario se vea claro el flujo de dos pasos
    fieldsets = (
        ("Identificación", {"fields": ("full_name", "id_number", "email")}),
        ("Empresa", {"fields": ("is_company", "credit_enabled")}),
        ("Estado", {"fields": ("active",)}),
    )


@admin.register(RatePlan)
class RatePlanAdmin(admin.ModelAdmin):
    # ✅ Agregado day_type para ver Normal/Domingo/Festivo
    list_display = ("vehicle_type", "client_kind", "billing_unit", "day_type", "price_cop", "active")
    list_filter = ("vehicle_type", "client_kind", "billing_unit", "day_type", "active")
    search_fields = ("vehicle_type__name",)
    ordering = ("vehicle_type__name", "client_kind", "billing_unit", "day_type")


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "plate",
        "status",
        "company",            # ✅ para ver si es empresa
        "company_credit",     # ✅ indicador de si esa empresa tiene crédito
        "vehicle_type",
        "client_kind_label",
        "rate_plan_label",
        "check_in",
        "check_out",
        "total_amount_cop",
        "get_closure_id",  # Agregado para ver rápido si ya se cerró
    )
    # ✅ se agrega company para filtrar por empresa y ver asignados/pendientes
    list_filter = ("status", "vehicle_type", "client_kind", "company", "closure")
    search_fields = ("plate", "company__full_name", "company__id_number")
    ordering = ("-check_in",)
    date_hierarchy = "check_in"

    @admin.display(description="Tipo cliente")
    def client_kind_label(self, obj: Ticket):
        try:
            return obj.get_client_kind_display()
        except Exception:
            return obj.client_kind or ""

    @admin.display(description="Tarifa aplicada")
    def rate_plan_label(self, obj: Ticket):
        rp = getattr(obj, "rate_plan", None)
        if not rp:
            return "-"
        if hasattr(rp, "label"):
            try:
                return rp.label
            except Exception:
                pass
        # Fallback
        try:
            return str(rp)
        except Exception:
            return "Tarifa"

    @admin.display(description="Cierre #")
    def get_closure_id(self, obj: Ticket):
        return obj.closure.id if obj.closure else "-"

    @admin.display(description="Crédito empresa", boolean=True)
    def company_credit(self, obj: Ticket):
        """
        ✅ Muestra si la empresa asociada tiene crédito habilitado.
        Útil para validar rápido:
          - ASSIGNED => True
          - PENDING  => False
        """
        if not obj.company_id:
            return False
        try:
            return bool(obj.company.credit_enabled)
        except Exception:
            return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "ticket",
        "method",
        "status",
        "amount_cop",
        "transfer_ref",
        "invoice_required",
        "customer",
        "company",              # ✅ ver empresa cuando method=CREDIT
        "created_at",
        "print_receipt_link",   # Nuevo botón
    )
    list_filter = ("method", "status", "invoice_required", "company")
    search_fields = (
        "ticket__plate",
        "transfer_ref",
        "customer__id_number",
        "customer__full_name",
        "company__id_number",
        "company__full_name",
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    # === NUEVO: BOTÓN PARA RE-IMPRIMIR RECIBO ===
    @admin.display(description="Acción")
    def print_receipt_link(self, obj):
        url = reverse("parking:print_receipt", args=[obj.id])
        return format_html(
            '<a class="button" href="{}" target="_blank" style="background-color:#2c7be5; color:white; padding:3px 8px; border-radius:4px; text-decoration:none;">🖨️ Recibo</a>',
            url
        )


# === NUEVO REGISTRO: CIERRES DE CAJA ===
@admin.register(Closure)
class ClosureAdmin(admin.ModelAdmin):
    list_display = (
        "id_label",
        "period_start",
        "period_end",
        "total_amount",
        "reprint_report_link",
    )
    list_filter = ("date",)
    ordering = ("-date",)
    date_hierarchy = "date"

    @admin.display(description="Reporte")
    def id_label(self, obj):
        return f"Cierre #{obj.id}"

    @admin.display(description="Acción")
    def reprint_report_link(self, obj):
        url = reverse("parking:reprint_closure", args=[obj.id])
        return format_html(
            '<a class="button" href="{}" target="_blank" style="background-color:#28a745; color:white; padding:3px 8px; border-radius:4px; text-decoration:none;">🖨️ Ver Reporte</a>',
            url
        )
# === ✅ NUEVO REGISTRO: PLACAS MENSUALES ===
@admin.register(MonthlyPlate)
class MonthlyPlateAdmin(admin.ModelAdmin):
    """
    Admin simple para que el administrador:
      - vea todas las placas mensuales
      - busque rápido por placa
      - agregue/edite/elimine
    """
    list_display = ("plate", "created_at")
    search_fields = ("plate",)
    ordering = ("plate",)
    date_hierarchy = "created_at"

