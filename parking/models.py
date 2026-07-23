from datetime import datetime, time, timedelta
import math
import json
import hashlib
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.apps import apps

# ============================================================
# Helpers de calendario / day_type (NORMAL / SUNDAY / HOLIDAY)
# ============================================================
from django.db import models
class ActiveVehiclesReport(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    consecutive = models.PositiveIntegerField(unique=True)
    active_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Reporte Activos #{self.consecutive:05d}"

class BusinessInfo(models.Model):
    name = models.CharField("Nombre del negocio", max_length=150)
    owner_name = models.CharField("Propietario", max_length=150, blank=True)
    nit = models.CharField("NIT", max_length=30)
    phone = models.CharField("Celular", max_length=30, blank=True)
    email = models.EmailField("Correo electrónico", blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "BusinessInfo"
        verbose_name_plural = "BusinessInfo"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """
        Fuerza comportamiento tipo singleton:
        solo puede existir 1 registro.
        """
        if not self.pk and BusinessInfo.objects.exists():
            raise ValueError("Solo puede existir un único registro de Información del Negocio.")
        super().save(*args, **kwargs)

def _holidays_set():
    """
    Prioridad:
      1) BD (modelo Holiday) si existe y hay registros activos
      2) settings.PARKING_HOLIDAYS / settings.HOLIDAYS (fallback)
    Devuelve un set de strings 'YYYY-MM-DD'.
    """
    # 1) Intentar BD (sin romper si no hay migraciones/tabla)
    try:
        Holiday = apps.get_model("parking", "Holiday")  # <-- cambia "parking" si tu app se llama distinto
        qs = Holiday.objects.filter(active=True).values_list("date", flat=True)
        db_dates = {d.isoformat() for d in qs}
        if db_dates:
            return db_dates
    except Exception:
        pass

    # 2) Fallback: settings
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

DEFAULT_DAY_START = time(6, 0)
DEFAULT_NIGHT_START = time(18, 0)
class WorkType(models.Model):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=80)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        verbose_name = "WorkType"
        verbose_name_plural = "WorkType"

    def __str__(self):
        return self.name

class Holiday(models.Model):
    date = models.DateField("Fecha festiva", unique=True, db_index=True)
    name = models.CharField("Nombre/nota", max_length=120, blank=True, default="")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Holiday"
        verbose_name_plural = "Holiday"
        ordering = ("-date",)

    def __str__(self):
        label = self.date.strftime("%Y-%m-%d")
        return f"{label}{' - ' + self.name if self.name else ''}"


class SystemSettings(models.Model):
    day_start = models.TimeField(default=DEFAULT_DAY_START, verbose_name="Inicio Jornada Día")
    night_start = models.TimeField(default=DEFAULT_NIGHT_START, verbose_name="Inicio Jornada Noche")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Time Window"
        verbose_name_plural = "Time Window"

    def __str__(self):
        return f"Día: {self.day_start.strftime('%H:%M')} | Noche: {self.night_start.strftime('%H:%M')}"


def get_day_night_boundaries():
    """
    Lee horarios desde SystemSettings (admin).
    Fallback seguro si no existe tabla/registro (migraciones, arranque).
    """
    try:
        Settings = apps.get_model("parking", "SystemSettings")  # <-- cambia "parking" si tu app tiene otro nombre
        obj = Settings.objects.first()
        if obj:
            return obj.day_start, obj.night_start
    except Exception:
        pass
    return DEFAULT_DAY_START, DEFAULT_NIGHT_START
def iter_pricing_segments(start_dt, end_dt):
    if not start_dt or not end_dt or end_dt <= start_dt:
        return

    # ✅ NUEVO: límites dinámicos desde admin
    DAY_START, NIGHT_START = get_day_night_boundaries()

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
    n = 0
    for _, __, bu, ___ in iter_pricing_segments(start_dt, end_dt):
        if bu == "NIGHT":
            n += 1
    return n
def count_nights_between(start_dt, end_dt, night_start=None) -> int:
    """
    Compatibilidad:
    Tu lógica real cuenta bloques NIGHT tocados (según config admin).
    Si alguien pasaba night_start, no rompemos la firma.
    """
    if night_start is None:
        _, night_start = get_day_night_boundaries()
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
        ("SERVICE", "Servicio"),

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
        # ✅ SERVICE: no debería depender de RatePlan
        if ck == "SERVICE":
            raise ValidationError({"client_kind": "El tipo 'Servicio' no usa planes de tarifa (RatePlan)."})

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

    

    CLIENT_KIND_CHOICES = [
        ("PARKING", "Parqueadero"),
        ("WORKSHOP", "Taller"),
        ("SERVICE", "Servicio"),
    ]

    # ✅ NUEVO: FK real
    work_type_fk = models.ForeignKey(
        "WorkType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tickets",
    )

    # ✅ LEGACY: texto que tu sistema ya usa en mil partes
    work_type_text = models.CharField(max_length=40, default="NONE", blank=True)

    # -------------------------
    # ✅ COMPATIBILIDAD TOTAL:
    # ticket.work_type se comporta como string
    # -------------------------
    @property
    def work_type(self) -> str:
        """
        Siempre devuelve un string en MAYÚSCULAS:
        - Si hay FK: usa WorkType.code
        - Si no: usa work_type_text
        """
        if self.work_type_fk_id:
            code = (getattr(self.work_type_fk, "code", "") or "").strip().upper()
            return code or "NONE"
        return (self.work_type_text or "NONE").strip().upper() or "NONE"

    @work_type.setter
    def work_type(self, value):
        """
        Permite asignar:
        - string ("NONE", "LAVADO")
        - WorkType instance
        - None
        Mantiene sincronizados:
        - work_type_fk
        - work_type_text (MAYÚSCULAS)
        """
        # None / vacío / NONE => limpia
        if value is None or (isinstance(value, str) and value.strip().upper() in ("", "NONE")):
            self.work_type_fk = None
            self.work_type_text = "NONE"
            return

        # si pasan objeto WorkType (instancia)
        # (no importamos nada: solo verificamos que parezca WorkType)
        if hasattr(value, "pk") and hasattr(value, "code"):
            self.work_type_fk = value
            self.work_type_text = (getattr(value, "code", "") or "").strip().upper() or "NONE"
            return

        # si pasan string
        if isinstance(value, str):
            s = value.strip().upper()
            self.work_type_text = s or "NONE"

            # intenta mapear al FK si existe (si no existe, no revienta)
            try:
                wt = WorkType.objects.filter(code__iexact=self.work_type_text).first()
            except Exception:
                wt = None

            self.work_type_fk = wt
            return

        # fallback: cualquier cosa rara => NONE
        self.work_type_fk = None
        self.work_type_text = "NONE"


    def save(self, *args, **kwargs):
        """
        ✅ Blindaje:
        - Si alguien cambia work_type_fk directamente, sincronizamos el texto.
        - Si alguien cambia work_type_text, normalizamos y limpiamos FK si corresponde.
        """
        if self.work_type_fk_id:
            self.work_type_text = (getattr(self.work_type_fk, "code", "") or "").strip().upper() or "NONE"
        else:
            self.work_type_text = (self.work_type_text or "NONE").strip().upper() or "NONE"
            if self.work_type_text == "NONE":
                self.work_type_fk = None

        super().save(*args, **kwargs)

    def get_work_type_display(self):
        """
        Para compatibilidad con tu print_receipt:
        - Si hay FK: muestra WorkType.name
        - Si no: muestra el code (string)
        """
        if self.work_type_fk_id:
            name = (getattr(self.work_type_fk, "name", "") or "").strip()
            return name or self.work_type
        return self.work_type


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
    allow_manual_total = models.BooleanField(
        default=False,
        help_text="Si está activo, permite editar manualmente el total en el admin (no se recalcula automáticamente desde el admin).",
    )
    total_amount_cop = models.PositiveIntegerField(null=True, blank=True)

    service_type = models.CharField(max_length=80, default="NONE")
    service_amount_cop = models.PositiveIntegerField(default=0)
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
            ✅ EXCEPCIÓN: si day_type es SUNDAY u HOLIDAY, cobra tarifa completa (1 vez) por franja tocada
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
        # ✅ NUEVO: SERVICE no cobra parqueo nunca
        if kind == "SERVICE":
            base = 0
            return base + extra_service + extra_work
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
                price = self._get_rateplan_price("PARKING", "HOUR", dt)

                # ✅ CAMBIO PEDIDO: Domingo/Festivo (06:00–18:00) = tarifa plana por franja tocada
                if dt in ("SUNDAY", "HOLIDAY"):
                    base += int(price)
                else:
                    minutes = max(1, int(math.ceil(seconds / 60.0)))
                    hours = int(math.ceil(minutes / 60.0))
                    hours = max(1, hours)
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
    updated_at = models.DateTimeField(auto_now=True)
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
# ============================================================
# Outbox temporal de Facturación Electrónica
# ============================================================

class ElectronicInvoiceOutbox(models.Model):
    STATUS_CHOICES = [
        ("PENDING", "Pendiente"),
        ("ERROR", "Error"),
        ("SENT", "Enviada"),
    ]

    id_number = models.CharField("Cédula o NIT", max_length=30, db_index=True)
    full_name = models.CharField("Nombre o razón social", max_length=160)
    email = models.EmailField("Correo electrónico", max_length=254)

    # Items reales
    items = models.JSONField(default=list, blank=True)

    total_amount_cop = models.PositiveIntegerField(default=0)

    # Hash para upsert idéntico
    items_hash = models.CharField(max_length=64, blank=True, db_index=True, default="")

    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default="PENDING",
        db_index=True,
    )

    last_error = models.TextField("Último error", blank=True, default="")
    last_attempt_at = models.DateTimeField("Último intento", null=True, blank=True)

    # (recomendado)
    sent_at = models.DateTimeField("Enviada el", null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["id_number"]),
            models.Index(fields=["items_hash"]),
        ]

    def _normalize_items(self):
        """
        ✅ Nuevo formato soportado (preferido):
          {"plate": "ABC123", "parking": 8000, "work": 3000, "work_type": "EMBARILLADO"}

        ✅ Compatibilidad formato viejo:
          {"plate": "ABC123", "price": 11000}  -> se convierte a parking=price, work=0, work_type="NONE"

        Devuelve SIEMPRE lista normalizada con llaves:
          plate, parking, work, work_type
        """
        def _to_int(x):
            try:
                return int(x or 0)
            except Exception:
                return 0

        norm = []
        for it in (self.items or []):
            if not isinstance(it, dict):
                continue

            plate = (it.get("plate") or "").strip().upper()
            if not plate:
                continue

            # ✅ formato viejo (price)
            if "price" in it and ("parking" not in it and "work" not in it):
                price = _to_int(it.get("price"))
                if price > 0:
                    norm.append({"plate": plate, "parking": price, "work": 0, "work_type": "NONE"})
                continue

            # ✅ formato nuevo
            parking = _to_int(it.get("parking") if "parking" in it else it.get("parking_amount_cop"))
            work = _to_int(it.get("work") if "work" in it else it.get("work_amount_cop"))
            work_type = (it.get("work_type") or it.get("work_type_code") or "NONE").strip().upper() or "NONE"

            if parking > 0 or work > 0:
                norm.append({"plate": plate, "parking": parking, "work": work, "work_type": work_type})

        return norm

    def _compute_items_hash(self) -> str:
        """
        Hash del contenido normalizado.
        - Incluye plate, parking, work, work_type
        - Esto evita colisiones y mantiene idempotencia del upsert.
        """
        try:
            norm = self._normalize_items()
            payload = json.dumps(norm, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
        except Exception:
            return ""

    def clean(self):
        super().clean()

        self.id_number = (self.id_number or "").strip()
        self.full_name = (self.full_name or "").strip()
        self.email = (self.email or "").strip()

        if not isinstance(self.items, list) or not self.items:
            raise ValidationError({"items": "Debe incluir al menos un ítem."})

        cleaned = self._normalize_items()
        if not cleaned:
            raise ValidationError({"items": "Debe incluir al menos un ítem válido (plate y parking/work > 0)."})


        # ✅ normaliza lo que queda guardado en BD
        self.items = cleaned

        # ✅ total nuevo: suma de parking + work
        self.total_amount_cop = int(sum((it.get("parking", 0) or 0) + (it.get("work", 0) or 0) for it in cleaned))

        # ✅ hash nuevo
        self.items_hash = self._compute_items_hash()

        if not self.id_number:
            raise ValidationError({"id_number": "Este campo es obligatorio."})
        if not self.full_name:
            raise ValidationError({"full_name": "Este campo es obligatorio."})
        if self.total_amount_cop <= 0:
            raise ValidationError({"total_amount_cop": "Debe ser mayor a 0."})

        # ✅ Si está enviada y no tiene sent_at, asignarlo
        if self.status == "SENT" and not self.sent_at:
            self.sent_at = timezone.now()

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def mark_failed(self, message: str = ""):
        self.status = "ERROR"
        self.last_attempt_at = timezone.now()
        self.last_error = (message or "").strip()[:2000]
        self.save(update_fields=["status", "last_attempt_at", "last_error", "updated_at"])

    def mark_sent(self):
        self.status = "SENT"
        self.last_error = ""
        self.last_attempt_at = timezone.now()
        if not self.sent_at:
            self.sent_at = timezone.now()
        self.save(update_fields=["status", "last_error", "last_attempt_at", "sent_at", "updated_at"])

    def __str__(self):
        return f"FE Outbox #{self.pk} - {self.status} - {self.id_number} - {self.total_amount_cop} COP"

class MonthlyReceipt(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    consecutive = models.PositiveIntegerField(unique=True)
    amount_cop = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Recibo Mensual #{self.consecutive}"
class CompanyReceiptNumber(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    consecutive = models.PositiveIntegerField(unique=True)
    amount_cop = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Recibo Empresa #{self.consecutive}"
