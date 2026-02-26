from django.contrib import admin, messages
from django.utils.html import format_html
from django.urls import reverse
from .models import VehicleType, RatePlan, Ticket, Payment, Customer, Closure,MonthlyPlate,SystemSettings,WorkType
from django.utils import timezone
from .models import ElectronicInvoiceOutbox
from .utils import emit_electronic_invoice_preview,estimate_amount_cop   # 👈 tu función real
from django.db import IntegrityError,transaction

from rangefilter.filters import DateRangeFilter, DateTimeRangeFilter
from .models import BusinessInfo
from .models import Holiday

@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name", "active")
    list_filter = ("active", ("date", DateRangeFilter))
    search_fields = ("name",)
    ordering = ("-date",)
    date_hierarchy = "date"

@admin.register(BusinessInfo)
class BusinessInfoAdmin(admin.ModelAdmin):
    list_display = ("name", "nit", "phone", "email", "updated_at")
    list_filter = (("updated_at", DateRangeFilter),)
    date_hierarchy = "updated_at"

def try_send_electronic_invoice(obj: ElectronicInvoiceOutbox) -> dict:
    """
    Envía usando Siigo con el payload del outbox (items).
    Devuelve el dict de emit_electronic_invoice_preview.
    """
    return emit_electronic_invoice_preview(
        id_number=obj.id_number,
        full_name=obj.full_name,
        email=obj.email,
        items=list(obj.items or []),  # ✅ NUEVO
    )
@admin.register(WorkType)
class WorkTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "active")
    list_filter = ("active",)

@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ("day_start", "night_start", "updated_at")
    list_filter = (("updated_at", DateRangeFilter),)
    date_hierarchy = "updated_at"

    def has_add_permission(self, request):
        # Solo permitir una configuración
        return not SystemSettings.objects.exists()
@admin.register(ElectronicInvoiceOutbox)
class ElectronicInvoiceOutboxAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "status",
        "id_number",
        "full_name",
        "email",
        "total_amount_cop",
        "items_count",
        "last_attempt_at",
        "created_at",
    )
    list_filter = ("status", ("created_at", DateRangeFilter), ("last_attempt_at", DateRangeFilter))
    search_fields = ("id_number", "full_name", "email")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    readonly_fields = ("created_at", "updated_at", "last_attempt_at", "items_hash", "total_amount_cop")

    fieldsets = (
        ("Estado", {"fields": ("status", "last_error", "last_attempt_at")}),
        ("Datos de factura (payload)", {"fields": ("id_number", "full_name", "email", "items", "total_amount_cop", "items_hash")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    actions = ("retry_send", "clear_error")

    @admin.display(description="# Ítems")
    def items_count(self, obj: ElectronicInvoiceOutbox) -> int:
        try:
            return len(obj.items or [])
        except Exception:
            return 0

    # -------------------------
    # Helpers internos (admin)
    # -------------------------
    def _status_allowed(self, Model, value: str) -> bool:
        try:
            f = Model._meta.get_field("status")
            valid = {c[0] for c in (getattr(f, "choices", None) or [])}
            return (not valid) or (value in valid)
        except Exception:
            return True

    def _pick_success_and_error(self, result):
        """
        Normaliza result para decidir éxito/fracaso sin falsos ERROR.
        Soporta:
        - {"success": True, ...}
        - respuesta estilo Siigo: {"id": "...", "name": "..."} aunque no haya 'success'
        - errors como [] o {} => NO se consideran error
        """
        success = False
        err = None
        extra = {}

        if not isinstance(result, dict):
            # Si no es dict, no sabemos -> fallo con mensaje genérico
            return False, "Respuesta inválida del servicio.", extra

        # error puede venir en varias llaves
        err = result.get("error") or result.get("errors") or result.get("message_error")

        # si errors está vacío, no cuenta como error
        if isinstance(err, (list, dict)) and len(err) == 0:
            err = None
        if isinstance(err, str) and not err.strip():
            err = None

        # success puede venir o no
        if "success" in result:
            success = bool(result.get("success"))
        else:
            # Heurística robusta: si hay id o name/number, lo tratamos como éxito
            rid = result.get("id") or result.get("siigo_id")
            rname = result.get("name") or result.get("number") or result.get("siigo_number")
            success = bool(rid or rname)

        # extras siigo_* si vienen
        for k in ("siigo_id", "siigo_number", "siigo_date"):
            if k in result and result.get(k):
                extra[k] = result.get(k)

        # A veces Siigo devuelve id/name directo
        if "id" in result and result.get("id"):
            extra.setdefault("siigo_id", result.get("id"))
        if "name" in result and result.get("name"):
            extra.setdefault("siigo_number", result.get("name"))
        if "date" in result and result.get("date"):
            extra.setdefault("siigo_date", result.get("date"))

        return success, err, extra

    @admin.action(description="Reintentar envío (si sale OK se marca como enviada y se conserva)")
    def retry_send(self, request, queryset):
        ok = 0
        fail = 0

        qs = queryset.filter(status__in=["PENDING", "ERROR"])

        for obj in qs:
            obj.last_attempt_at = timezone.now()
            obj.save(update_fields=["last_attempt_at", "updated_at"])

            try:
                result = try_send_electronic_invoice(obj)

                success, err, extra = self._pick_success_and_error(result)

                if success and not err:
                    # ✅ NO borrar: conservar y marcar como enviada
                    obj.last_error = ""

                    # marcar SENT solo si el modelo lo permite; si no, deja PENDING
                    if self._status_allowed(ElectronicInvoiceOutbox, "SENT"):
                        obj.status = "SENT"
                    else:
                        obj.status = "PENDING"

                    # guardar extras si existen como campos en el modelo
                    for k, v in extra.items():
                        try:
                            ElectronicInvoiceOutbox._meta.get_field(k)
                            setattr(obj, k, v)
                        except Exception:
                            pass

                    obj.save(update_fields=["last_error", "status", "updated_at"])
                    ok += 1
                else:
                    # ✅ fallo real => ERROR y se conserva
                    if err:
                        obj.last_error = str(err)[:2000]
                    else:
                        obj.last_error = "Falló el envío sin detalle de error."[:2000]

                    obj.status = "ERROR" if self._status_allowed(ElectronicInvoiceOutbox, "ERROR") else obj.status
                    obj.save(update_fields=["last_error", "status", "updated_at"])
                    fail += 1

            except IntegrityError:
                obj.last_error = "Conflicto de duplicado. Recarga y revisa registros."
                obj.status = "ERROR" if self._status_allowed(ElectronicInvoiceOutbox, "ERROR") else obj.status
                obj.save(update_fields=["last_error", "status", "updated_at"])
                fail += 1

            except Exception as e:
                obj.last_error = (str(e).strip() or "Error desconocido")[:2000]
                obj.status = "ERROR" if self._status_allowed(ElectronicInvoiceOutbox, "ERROR") else obj.status
                obj.save(update_fields=["last_error", "status", "updated_at"])
                fail += 1

        if ok:
            messages.success(request, f"Envío OK: {ok} registro(s) marcados como enviados y conservados.")
        if fail:
            messages.error(request, f"Fallaron: {fail} registro(s). Revisa el campo 'Último error'.")
        if not ok and not fail:
            messages.info(request, "No había registros PENDING/ERROR seleccionados para reintentar.")

    @admin.action(description="Limpiar error (mantener PENDING)")
    def clear_error(self, request, queryset):
        # si tu modelo no permite PENDING por choices (raro), igual no rompe: update silencioso
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
        "company",
        "company_credit",
        "vehicle_type",
        "client_kind_label",
        "rate_plan_label",
        "check_in",
        "check_out",
        "parking_amount",
        "work_amount",
        "get_closure_id",
    )
    list_filter = (
        "status",
        "vehicle_type",
        "client_kind",
        "company",
        "closure",
        ("check_in", DateRangeFilter),
        ("check_out", DateRangeFilter),
    )
    search_fields = ("plate", "company__full_name", "company__id_number")
    ordering = ("-check_in",)
    date_hierarchy = "check_in"

    # ✅ NUEVO: total_amount_cop editable SOLO si allow_manual_total=True
    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))

        # total NO editable por defecto
        if obj is None:
            if "total_amount_cop" not in ro:
                ro.append("total_amount_cop")
            return ro

        if not getattr(obj, "allow_manual_total", False):
            if "total_amount_cop" not in ro:
                ro.append("total_amount_cop")
        else:
            if "total_amount_cop" in ro:
                ro.remove("total_amount_cop")

        return ro

    # -----------------------------
    # UI helpers (igual que ya tenías)
    # -----------------------------
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
        try:
            return str(rp)
        except Exception:
            return "Tarifa"

    @admin.display(description="Parqueadero ($)")
    def parking_amount(self, obj: Ticket):
        value = int(getattr(obj, "parking_amount_cop", 0) or 0)
        return f"{value:,.0f}"

    @admin.display(description="Servicio ($)")
    def work_amount(self, obj: Ticket):
        value = int(getattr(obj, "work_amount_cop", 0) or 0)
        return f"{value:,.0f}"

    @admin.display(description="Cierre #")
    def get_closure_id(self, obj: Ticket):
        return obj.closure.id if obj.closure else "-"

    @admin.display(description="Crédito empresa", boolean=True)
    def company_credit(self, obj: Ticket):
        if not obj.company_id:
            return False
        try:
            return bool(obj.company.credit_enabled)
        except Exception:
            return False

    # =========================================================
    # ✅ NÚCLEO: al guardar Ticket, sincroniza Payment + Closure
    # =========================================================
    def save_model(self, request, obj: Ticket, form, change):
        """
        Regla: Ticket es la fuente de verdad.
        - Recalcula total_amount_cop cuando corresponde
        - Sincroniza Payment asociado (si existe o si se debe crear)
        - Recalcula total del Closure si el ticket pertenece a uno
        """
        with transaction.atomic():
            super().save_model(request, obj, form, change)

            # 1) Recalcular total si tiene sentido (tiene salida o ya no está ACTIVE)
            #    - ACTIVE sin check_out: normalmente NO se congela aquí
            #    - PAID/PENDING/ASSIGNED: debe existir total
            # ✅ CAMBIO: si allow_manual_total=True, NO recalcula (respeta edición manual en admin)
            should_recalc_total = (
                (not getattr(obj, "allow_manual_total", False)) and
                (bool(obj.check_out) or (obj.status in ("PAID", "PENDING", "ASSIGNED")))
            )
            if should_recalc_total:
                end_time = obj.check_out or timezone.now()
                try:
                    new_total = int(estimate_amount_cop(obj, now=end_time) or 0)
                except TypeError:
                    # por si tu estimate_amount_cop usa firma estimate_amount_cop(ticket, now=...)
                    new_total = int(estimate_amount_cop(obj, now=end_time) or 0)

                if int(obj.total_amount_cop or 0) != new_total:
                    Ticket.objects.filter(pk=obj.pk).update(total_amount_cop=new_total)
                    obj.total_amount_cop = new_total  # mantener obj consistente en memoria

            # 2) Sincronizar/crear Payment asociado cuando corresponda
            #    - Si hay payment existente: ajusta amount
            #    - Si no hay, lo crea SOLO si el ticket ya no es ACTIVE (o tiene check_out)
            needs_payment = (obj.status in ("PAID", "PENDING", "ASSIGNED")) or bool(obj.check_out)
            if needs_payment:
                p = Payment.objects.filter(ticket=obj).first()

                # método/estado coherente con tu lógica actual
                if obj.status == "ASSIGNED":
                    pay_method = "CREDIT"
                    pay_status = "PENDING"
                    pay_company = obj.company
                elif obj.status == "PENDING":
                    # PENDING “sin crédito” (pero ya existe Payment CREDIT en tu flujo)
                    pay_method = "CREDIT"
                    pay_status = "PENDING"
                    pay_company = obj.company
                else:
                    # PAID: no tocamos method si ya existe, pero si no existe, lo dejamos CASH por defecto
                    pay_method = getattr(p, "method", None) or "CASH"
                    pay_status = "PAID"
                    pay_company = None

                if p:
                    update_fields = []

                    # amount siempre se alinea al total del ticket
                    if int(p.amount_cop or 0) != int(obj.total_amount_cop or 0):
                        p.amount_cop = int(obj.total_amount_cop or 0)
                        update_fields.append("amount_cop")

                    # status/method/company se alinean según estado del ticket
                    if p.status != pay_status:
                        p.status = pay_status
                        update_fields.append("status")

                    if p.method != pay_method:
                        p.method = pay_method
                        update_fields.append("method")

                    if getattr(p, "company_id", None) != getattr(pay_company, "id", None):
                        p.company = pay_company
                        update_fields.append("company")

                    if update_fields:
                        p.save(update_fields=update_fields)
                else:
                    Payment.objects.create(
                        ticket=obj,
                        method=pay_method,
                        status=pay_status,
                        amount_cop=int(obj.total_amount_cop or 0),
                        company=pay_company,
                    )

            # 3) Si el ticket está en un cierre, recalcular total del cierre
            if obj.closure_id:
                closure = Closure.objects.select_for_update().filter(pk=obj.closure_id).first()
                if closure:
                    total = 0
                    for t in closure.tickets.all().only("total_amount_cop", "status"):
                        if getattr(t, "status", "") == "PENDING":
                            continue
                        total += int(t.total_amount_cop or 0)
                    if int(getattr(closure, "total_amount", 0) or 0) != int(total):
                        closure.total_amount = int(total)
                        closure.save(update_fields=["total_amount"])

        messages.success(request, "Ticket actualizado y sincronizado (Payment / Closure).")
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
    list_filter = ("method", "status", "invoice_required", "company", ("created_at", DateRangeFilter))
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
    list_filter = ("date", ("date", DateRangeFilter))
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
    list_filter = (("created_at", DateRangeFilter),)
    search_fields = ("plate",)
    ordering = ("plate",)
    date_hierarchy = "created_at"