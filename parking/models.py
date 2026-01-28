from datetime import datetime, time, timedelta
import math

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError


# ============================================================
# Helpers de calendario / day_type (NORMAL / SUNDAY / HOLIDAY)
# ============================================================

def _holidays_set():
    """
    Lee festivos desde settings (opcional):
      - PARKING_HOLIDAYS = ["2026-01-01", ...]
      - o HOLIDAYS = ["2026-01-01", ...]
    """
    holidays = getattr(settings, "PARKING_HOLIDAYS", None)
    if holidays is None:
        holidays = getattr(settings, "HOLIDAYS", None)
    if not holidays:
        return set()
    return set(str(x).strip() for x in holidays if str(x).strip())


def is_holiday(d) -> bool:
    try:
        return d.isoformat() in _holidays_set()
    except Exception:
        return False


def get_day_type_for_date(d) -> str:
    """
    NORMAL / SUNDAY / HOLIDAY según fecha local.
    """
    if is_holiday(d):
        return "HOLIDAY"
    try:
        # lunes=0 ... domingo=6
        if d.weekday() == 6:
            return "SUNDAY"
    except Exception:
        pass
    return "NORMAL"


def _localize(dt):
    tz = timezone.get_current_timezone()
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, tz)
    return timezone.localtime(dt, tz)


# ============================================================
# Segmentación de cobro:
#   - Día: 06:00 -> 18:00  (HOUR / hora-fracción)
#   - Noche: 18:00 -> 06:00 (NIGHT / noche-fracción)
#
# IMPORTANTE: Para el day_type de la NOCHE se usa la fecha
#             del inicio de la noche (18:00).
#             Ej: 2026-01-25 02:00 pertenece a la noche
#                 iniciada el 2026-01-24 18:00.
# ============================================================

DAY_START = time(6, 0)
NIGHT_START = time(18, 0)

def iter_pricing_segments(start_dt, end_dt):
    if not start_dt or not end_dt or end_dt <= start_dt:
        return

    s = _localize(start_dt)
    e = _localize(end_dt)

    cur = s
    tz = cur.tzinfo

    while cur < e:
        cur_t = cur.time()
        in_day = (cur_t >= DAY_START) and (cur_t < NIGHT_START)

        if in_day:
            billing_unit = "HOUR"
            anchor_date = cur.date()
            boundary = datetime.combine(cur.date(), NIGHT_START).replace(tzinfo=tz)
        else:
            billing_unit = "NIGHT"
            if cur_t >= NIGHT_START:
                anchor_date = cur.date()
                next_day = cur.date() + timedelta(days=1)
                boundary = datetime.combine(next_day, DAY_START).replace(tzinfo=tz)
            else:
                anchor_date = cur.date() - timedelta(days=1)
                boundary = datetime.combine(cur.date(), DAY_START).replace(tzinfo=tz)

        seg_end = min(boundary, e)
        if seg_end > cur:
            yield (cur, seg_end, billing_unit, anchor_date)
        cur = seg_end



def count_night_blocks_overlapped(start_dt, end_dt) -> int:
    """
    Cuenta cuántos BLOQUES de noche (18:00 -> 06:00) son tocados por el intervalo.
    Cobro "noche/fracción": si toca una noche, cuenta 1.
    """
    n = 0
    for _, __, bu, ___ in iter_pricing_segments(start_dt, end_dt):
        if bu == "NIGHT":
            n += 1
    return n


# Mantengo el nombre por compatibilidad con tu código previo,
# pero ahora cuenta noches según 18:00 -> 06:00 (noche/fracción).
def count_nights_between(start_dt, end_dt, night_start=time(18, 0)) -> int:
    """
    Compatibilidad:
    Para tu nueva lógica, lo correcto es contar bloques de noche tocados.
    """
    return count_night_blocks_overlapped(start_dt, end_dt)


# ============================================================
# Modelos
# ============================================================

class VehicleType(models.Model):
    name = models.CharField(max_length=50, unique=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Customer(models.Model):
    """
    Tabla única para:
    - Factura electrónica (cualquier cliente: persona o empresa)
    - Empresas con dos pasos:
        1) is_company = es empresa
        2) credit_enabled = si es acumulable (crédito) o no

    "Cédula o NIT" se maneja en un solo campo (id_number).
    """
    id_number = models.CharField("Cédula o NIT", max_length=30, unique=True)
    full_name = models.CharField("Nombres y apellidos / Razón social", max_length=160)
    email = models.EmailField("Correo electrónico", max_length=254)

    # Paso 1: es empresa
    is_company = models.BooleanField("Es empresa", default=False)

    # Paso 2: SOLO si es empresa, define si es acumulable (crédito)
    credit_enabled = models.BooleanField("Crédito habilitado (acumulable)", default=False)

    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()
        # Si no es empresa, no puede tener crédito habilitado
        if not self.is_company and self.credit_enabled:
            raise ValidationError({"credit_enabled": "Solo las empresas pueden tener crédito habilitado."})

    def __str__(self):
        return f"{self.full_name} ({self.id_number})"


class RatePlan(models.Model):
    """
    Tarifa por:
      - Tipo de vehículo
      - Tipo de cliente (Parqueadero / Taller)
      - Unidad de cobro (Hora/Fracción / Noche)
      - Tipo de día (Normal / Domingo / Festivo)

    Reglas:
      - PARKING: permite HOUR y NIGHT
      - WORKSHOP: solo permite NIGHT
    """

    CLIENT_KIND_CHOICES = [
        ("PARKING", "Parqueadero"),
        ("WORKSHOP", "Taller"),
    ]

    BILLING_UNIT_CHOICES = [
        ("HOUR", "Hora / Fracción"),
        ("NIGHT", "Noche"),
    ]

    DAY_TYPE_CHOICES = [
        ("NORMAL", "Normal"),
        ("SUNDAY", "Domingo"),
        ("HOLIDAY", "Festivo"),
    ]

    vehicle_type = models.ForeignKey(
        "VehicleType",
        on_delete=models.PROTECT,
        related_name="rate_plans",
    )

    client_kind = models.CharField(
        max_length=12,
        choices=CLIENT_KIND_CHOICES,
        default="PARKING",
        db_index=True,
    )

    billing_unit = models.CharField(
        max_length=10,
        choices=BILLING_UNIT_CHOICES,
        db_index=True,
    )

    day_type = models.CharField(
        max_length=10,
        choices=DAY_TYPE_CHOICES,
        default="NORMAL",
        db_index=True,
    )

    price_cop = models.PositiveIntegerField()  # COP
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("vehicle_type", "client_kind", "billing_unit", "day_type")
        ordering = ("vehicle_type__name", "client_kind", "billing_unit", "day_type")

    def clean(self):
        """
        Enforce reglas de negocio:
          - WORKSHOP solo puede ser NIGHT
          - PARKING puede ser HOUR o NIGHT
        """
        ck = (self.client_kind or "").strip().upper()
        bu = (self.billing_unit or "").strip().upper()

        if ck == "WORKSHOP" and bu != "NIGHT":
            raise ValidationError({
                "billing_unit": "Para Taller la unidad debe ser 'Noche' (NIGHT)."
            })

        if ck == "PARKING" and bu not in ("HOUR", "NIGHT"):
            raise ValidationError({
                "billing_unit": "Para Parqueadero la unidad debe ser 'Hora / Fracción' (HOUR) o 'Noche' (NIGHT)."
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def label(self) -> str:
        return (
            f"{self.vehicle_type.name} - "
            f"{self.get_client_kind_display()} "
            f"({self.get_billing_unit_display()}) - "
            f"{self.get_day_type_display()}"
        )

    def __str__(self):
        return f"{self.label} - {self.price_cop} COP"


class Closure(models.Model):
    """
    Modelo para el Cierre de Caja (Reporte).
    Agrupa tickets que ya han sido reportados para no duplicarlos en siguientes impresiones.
    """
    date = models.DateTimeField(default=timezone.now)
    period_start = models.DateTimeField("Inicio del Periodo", null=True, blank=True)
    period_end = models.DateTimeField("Fin del Periodo", null=True, blank=True)
    total_amount = models.PositiveIntegerField(default=0)

    def __str__(self):
        start = self.period_start.strftime('%d/%m %H:%M') if self.period_start else "?"
        end = self.date.strftime('%d/%m %H:%M')
        return f"Reporte #{self.id} ({start} a {end})"

class Ticket(models.Model):
    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("ASSIGNED", "Assigned"),  # empresa con crédito (acumulable)
        ("PENDING", "Pending"),    # empresa SIN crédito (no acumulable)
        ("PAID", "Paid"),          # pagado/cerrado definitivamente
    ]

    WORK_TYPE_CHOICES = [
        ("NONE", "Ninguno"),
        ("EMBARILLADO", "Embarillado"),
        ("DESEMBARILLADO", "Des-embarillado"),
        ("ENCARPADO", "Encarpado"),
        ("OTHER", "Otros"),
    ]

    CLIENT_KIND_CHOICES = [
        ("PARKING", "Parqueadero"),
        ("WORKSHOP", "Taller"),
    ]

    plate = models.CharField(max_length=12, db_index=True)
    vehicle_type = models.ForeignKey(VehicleType, on_delete=models.PROTECT)

    client_kind = models.CharField(
        max_length=12,
        choices=CLIENT_KIND_CHOICES,
        default="PARKING",
        db_index=True,
    )

    company = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tickets_as_company"
    )

    # Se deja por compatibilidad con tus forms/vistas anteriores,
    # pero con la nueva lógica el cálculo NO depende de un único rate_plan.
    rate_plan = models.ForeignKey(RatePlan, on_delete=models.PROTECT, null=True, blank=True)

    check_in = models.DateTimeField(default=timezone.now)
    check_out = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="ACTIVE")

    closure = models.ForeignKey(
        Closure,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets"
    )

    total_amount_cop = models.PositiveIntegerField(null=True, blank=True)

    service_type = models.CharField(max_length=80, default="NONE")
    service_amount_cop = models.PositiveIntegerField(default=0)

    work_type = models.CharField(
        max_length=20,
        choices=WORK_TYPE_CHOICES,
        default="NONE",
    )
    work_amount_cop = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="tickets_created"
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tickets_closed"
    )

    def clean(self):
        super().clean()

        if self.status in ("ASSIGNED", "PENDING"):
            if not self.company_id:
                raise ValidationError({"company": "Este estado requiere una empresa asignada."})
            if not self.company.is_company:
                raise ValidationError({"company": "El cliente seleccionado no está marcado como empresa."})

        if self.company_id and not self.company.is_company:
            raise ValidationError({"company": "El cliente seleccionado no está marcado como empresa."})

        if self.status == "ASSIGNED":
            if not self.company_id:
                raise ValidationError({"company": "Un ticket ASSIGNED debe tener empresa asignada."})
            if not self.company.credit_enabled:
                raise ValidationError({"status": "La empresa no tiene crédito habilitado, el ticket debe ser PENDING."})

        if self.status == "PENDING":
            if not self.company_id:
                raise ValidationError({"company": "Un ticket PENDING debe tener empresa asignada."})
            if self.company.credit_enabled:
                raise ValidationError({"status": "La empresa tiene crédito habilitado, el ticket debe ser ASSIGNED."})

    def __str__(self):
        return f"{self.plate} - {self.status}"

    @property
    def client_type_display(self) -> str:
        return self.get_client_kind_display()

    @property
    def has_service(self) -> bool:
        st = (self.service_type or "").strip().upper()
        return (st != "" and st != "NONE") and int(self.service_amount_cop or 0) > 0

    @property
    def parking_amount_cop(self) -> int:
        total = int(self.total_amount_cop or 0)
        svc = int(self.service_amount_cop or 0)
        wrk = int(self.work_amount_cop or 0)
        return max(0, total - svc - wrk)

    # -------------------------
    # Tarifa dinámica por segmento
    # -------------------------
    def _get_rateplan_price(self, client_kind: str, billing_unit: str, day_type: str) -> int:
        """
        Retorna price_cop (int) del RatePlan activo.
        Fallback:
          1) exacto day_type
          2) NORMAL
          3) cualquier day_type
          4) 0 si no hay
        """
        ck = (client_kind or "").strip().upper()
        bu = (billing_unit or "").strip().upper()
        dt = (day_type or "NORMAL").strip().upper()

        qs = RatePlan.objects.filter(
            vehicle_type=self.vehicle_type,
            client_kind=ck,
            billing_unit=bu,
            active=True,
        )

        rp = qs.filter(day_type=dt).first()
        if not rp and dt != "NORMAL":
            rp = qs.filter(day_type="NORMAL").first()
        if not rp:
            rp = qs.first()

        return int(rp.price_cop) if rp else 0

    def compute_amount_cop(self, end_time=None) -> int:
        """
        NUEVA LÓGICA (TU REGLA):

        - PARKING:
            * 06:00–18:00 => cobra HOUR (hora/fracción) según day_type del día
            * 18:00–06:00 => cobra NIGHT (noche/fracción) según day_type de la fecha del inicio de noche (18:00)
            * Si cruza segmentos, suma cada segmento con su tarifa correspondiente.
        - WORKSHOP:
            * Solo cobra NOCHE(S) tocadas (noche/fracción)
            * Si toca 2 noches => cobra 2 * tarifa (y cada noche puede tener day_type distinto)
            * Si NO toca ninguna noche => cobra 0 de parqueadero (los extras sí se cobran)

        + extras:
            * service_amount_cop + work_amount_cop
        """
        end_time = end_time or timezone.now()

        extra_work = int(self.work_amount_cop or 0)
        extra_service = int(self.service_amount_cop or 0)

        kind = (self.client_kind or "PARKING").strip().upper()

        # Base = parqueadero/taller según reglas
        base = 0

        if end_time <= self.check_in:
            # si no hay duración, en WORKSHOP base=0 (no cobra parqueadero),
            # en PARKING cobra 1 hora mínima
            if kind == "WORKSHOP":
                base = 0
            else:
                anchor_date = _localize(self.check_in).date()
                dt = get_day_type_for_date(anchor_date)
                base = self._get_rateplan_price("PARKING", "HOUR", dt)
            return base + extra_service + extra_work

        # ===== WORKSHOP: solo noches tocadas (sin mínimo) =====
        if kind == "WORKSHOP":
            for seg_start, seg_end, bu, anchor_date in iter_pricing_segments(self.check_in, end_time):
                if bu != "NIGHT":
                    continue
                dt = get_day_type_for_date(anchor_date)
                price = self._get_rateplan_price("WORKSHOP", "NIGHT", dt)
                base += int(price)  # 1 noche/fracción por cada bloque tocado

            # OJO: si no tocó ninguna noche, base queda en 0 (correcto)
            return base + extra_service + extra_work

        # ===== PARKING: suma día(HOUR) + noche(NIGHT) =====
        for seg_start, seg_end, bu, anchor_date in iter_pricing_segments(self.check_in, end_time):
            seconds = (seg_end - seg_start).total_seconds()
            if seconds <= 0:
                continue

            dt = get_day_type_for_date(anchor_date)

            if bu == "HOUR":
                minutes = max(1, int(math.ceil(seconds / 60.0)))
                hours = int(math.ceil(minutes / 60.0))
                hours = max(1, hours)
                price = self._get_rateplan_price("PARKING", "HOUR", dt)
                base += int(hours * price)
            else:
                # NIGHT: noche/fracción => 1 por bloque tocado
                price = self._get_rateplan_price("PARKING", "NIGHT", dt)
                base += int(price)

        return base + extra_service + extra_work

    # ✅ helper opcional (no rompe nada existente)
    def mark_paid(self, user=None, end_time=None):
        """
        Marca el ticket como PAID. Útil para liquidar ASSIGNED->PAID en bloque.
        No cambia montos (eso lo haces antes si ya lo calculas).
        """
        self.status = "PAID"
        if end_time is not None:
            self.check_out = end_time
        else:
            self.check_out = self.check_out or timezone.now()
        if user is not None:
            self.closed_by = user
        self.save(update_fields=["status", "check_out", "closed_by"])
class Payment(models.Model):
    METHOD_CHOICES = [
        ("CASH", "Efectivo"),
        ("TRANSFER", "Transferencia"),
        ("CREDIT", "Pago de empresas"),
    ]

    STATUS_CHOICES = [
        ("PAID", "Pagado"),
        ("PENDING", "Pendiente"),
    ]

    # ✅ antes: OneToOne obligatorio
    # ✅ ahora: permite null/blank para que exista un "recibo" de empresa (CREDIT) sin ticket único
    ticket = models.OneToOneField(
        Ticket,
        on_delete=models.PROTECT,
        related_name="payment",
        null=True,
        blank=True,
    )

    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PAID")

    transfer_ref = models.CharField("Referencia/Transacción", max_length=80, null=True, blank=True)

    company = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payments_as_company"
    )

    invoice_required = models.BooleanField(default=False)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payments_as_customer"
    )

    # ✅ desglose (para el recibo empresa)
    amount_parking_cop = models.PositiveIntegerField(default=0)
    amount_service_cop = models.PositiveIntegerField(default=0)

    # ✅ total (ya lo tenías)
    amount_cop = models.PositiveIntegerField()

    # ✅ guarda qué tickets se liquidaron (ids/placas/valores) como JSON string
    #    (sin modelo nuevo, esto sirve para reimprimir el recibo)
    tickets_payload = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()

        m = (self.method or "").strip().upper()

        if m == "CREDIT":
            if not self.company_id:
                raise ValidationError({"company": "Si el método es CREDIT, debe indicar la empresa."})
            if not self.company.is_company:
                raise ValidationError({"company": "El cliente seleccionado no está marcado como empresa."})
            # ✅ en CREDIT, ticket puede ser null (recibo global)
        else:
            # ✅ en CASH/TRANSFER debe existir ticket (pago normal)
            if not self.ticket_id:
                raise ValidationError({"ticket": "Este método requiere un ticket asociado."})

    def __str__(self):
        if self.method == "CREDIT" and self.company_id:
            return f"Empresa: {self.company} - {self.get_method_display()} - {self.get_status_display()}"
        if self.ticket_id:
            return f"{self.ticket.plate} - {self.get_method_display()} - {self.get_status_display()}"
        return f"{self.get_method_display()} - {self.get_status_display()}"

class MonthlyPlate(models.Model):
    plate = models.CharField(max_length=12, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.plate
