from django.contrib import admin, messages
from django.utils.html import format_html
from django.urls import reverse
from .models import VehicleType, RatePlan, Ticket, Payment, Customer, Closure,MonthlyPlate
from django.utils import timezone
from .models import ElectronicInvoiceOutbox
from .utils import emit_electronic_invoice_preview  # 👈 tu función real
from django.db import IntegrityError
def try_send_electronic_invoice(obj: ElectronicInvoiceOutbox) -> None:
    """
    Envía usando Siigo con el payload del outbox.
    Si OK: no retorna nada.
    Si falla: lanza Exception con mensaje amigable.
    """
    emit_electronic_invoice_preview(
        id_number=obj.id_number,
        full_name=obj.full_name,
        email=obj.email,
        total_amount_cop=obj.total_amount_cop,
    )


@admin.register(ElectronicInvoiceOutbox)
class ElectronicInvoiceOutboxAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "status",
        "id_number",
        "full_name",
        "email",
        "total_amount_cop",
        "last_attempt_at",
        "created_at",
    )
    list_filter = ("status", "created_at", "last_attempt_at")
    search_fields = ("id_number", "full_name", "email")
    ordering = ("-created_at",)

    readonly_fields = ("created_at", "updated_at", "last_attempt_at")

    fieldsets = (
        ("Estado", {"fields": ("status", "last_error", "last_attempt_at")}),
        ("Datos de factura (payload)", {"fields": ("id_number", "full_name", "email", "total_amount_cop")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    actions = ("retry_send", "clear_error")

    @admin.action(description="Reintentar envío (si sale OK se elimina)")
    def retry_send(self, request, queryset):
        ok = 0
        fail = 0

        qs = queryset.filter(status="PENDING")

        for obj in qs:
            # marca intento (aunque falle o no)
            obj.last_attempt_at = timezone.now()
            obj.save(update_fields=["last_attempt_at", "updated_at"])

            try:
                # 1) Intento real (Siigo)
                try_send_electronic_invoice(obj)

                # 2) OK => eliminar (regla del outbox)
                obj.delete()
                ok += 1

            except IntegrityError:
                # caso raro por unique_together (concurrencia). No debería pasar en reintento.
                obj.last_error = "Conflicto de duplicado (unique_together). Recarga y revisa registros."
                obj.status = "PENDING"
                obj.save(update_fields=["last_error", "status", "updated_at"])
                fail += 1

            except Exception as e:
                # Error => queda PENDING y registramos mensaje
                obj.last_error = str(e).strip() or "Error desconocido"
                obj.status = "PENDING"
                obj.save(update_fields=["last_error", "status", "updated_at"])
                fail += 1

        if ok:
            messages.success(request, f"Envío OK: {ok} registro(s) enviados y eliminados.")
        if fail:
            messages.error(request, f"Fallaron: {fail} registro(s). Revisa el campo 'Último error'.")
        if not ok and not fail:
            messages.info(request, "No había registros PENDING seleccionados para reintentar.")

    @admin.action(description="Limpiar error (mantener PENDING)")
    def clear_error(self, request, queryset):
        updated = queryset.update(last_error="", status="PENDING")
        messages.success(request, f"Se limpió el error en {updated} registro(s).")



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

