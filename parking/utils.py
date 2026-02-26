import math
from django.utils import timezone
from .models import Ticket, Customer, iter_pricing_segments,ElectronicInvoiceOutbox
# parking/utils.py
import os
from datetime import datetime
from .siigo_client import SiigoClient, SiigoAPIError, SiigoAuthError
# parking/utils.py
import re
from typing import Optional, Tuple
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def normalize_id_number(v: str) -> str:
    """Normaliza cédula/NIT: quita espacios y deja tal cual si tiene letras, pero si es numérico quita separadores."""
    s = (v or "").strip()
    if not s:
        return ""
    # Si contiene letras, no lo toques (por si manejas NITs especiales)
    if any(ch.isalpha() for ch in s):
        return s
    # Dejar solo dígitos (900.123-4 -> 9001234)
    return "".join(ch for ch in s if ch.isdigit())

def is_valid_email(v: str) -> bool:
    return bool(EMAIL_RE.match((v or "").strip()))

def resolve_or_create_customer(
    *,
    id_number: str,
    full_name: str,
    email: str,
    customer_obj: Optional[Customer] = None,
) -> Tuple[Optional[Customer], Optional[str]]:
    """
    Retorna (customer, error_message).
    - Si customer_obj existe => retorna ese.
    - Si existe Customer por id_number => retorna ese.
    - Si no existe => valida y crea uno nuevo.
    """
    if customer_obj:
        return customer_obj, None

    id_number = normalize_id_number(id_number)
    full_name = (full_name or "").strip()
    email = (email or "").strip()

    if id_number:
        c = Customer.objects.filter(id_number=id_number).first()
        if c:
            return c, None

    # Si no existe, validar datos mínimos para crear
    if not id_number:
        return None, "La cédula/NIT es obligatoria para factura electrónica."
    if not full_name:
        return None, "El nombre/razón social es obligatorio si el cliente no existe."
    if not email or not is_valid_email(email):
        return None, "El correo es obligatorio y debe ser válido si el cliente no existe."

    c = Customer.objects.create(
        id_number=id_number,
        full_name=full_name,
        email=email,
        active=True,
        is_company=False,
    )

    # Mantener tu regla: no permitir crédito si no es empresa
    try:
        force_credit_disabled(c)
    except Exception:
        pass

    return c, None



# ==========================
# Credenciales (ENV)
# ==========================
SIIGO_USERNAME = os.getenv("SIIGO_USERNAME", "linda.quintana@siigo.com")
SIIGO_ACCESS_KEY = os.getenv("SIIGO_ACCESS_KEY", "MjBjZTkwMzQtNTc1NS00NmUxLTk2MzgtZWE2ZDQzZDg5MmUzOntTMmpAL1l7MUc=")
SIIGO_APP_NAME = os.getenv("SIIGO_APP_NAME", "Parking")


# ==========================
# Configuración de IDs Siigo
# ==========================
SIIGO_CONFIG = {
    "document_type_id": 1280,  # Required
    "seller_id": 34,         # Required
    "payment_type_id": 289,   # Required
    "tax_id": 13693,            # Optional (None = sin impuesto)
    "product_code": "1",
}

def configure_siigo(
    document_type_id: int,
    seller_id: int,
    payment_type_id: int,
    tax_id: int = None,
    product_code: str = "SERVICIO",
) -> None:
    SIIGO_CONFIG["document_type_id"] = document_type_id
    SIIGO_CONFIG["seller_id"] = seller_id
    SIIGO_CONFIG["payment_type_id"] = payment_type_id
    SIIGO_CONFIG["tax_id"] = tax_id
    SIIGO_CONFIG["product_code"] = product_code


def _validate_config() -> None:
    required = ["document_type_id", "seller_id", "payment_type_id"]
    missing = [k for k in required if not SIIGO_CONFIG.get(k)]
    if missing:
        raise ValueError(
            f"Siigo no configurado. Faltan: {missing}. "
            f"Usa configure_siigo() primero."
        )


def _get_friendly_error(error: Exception) -> str:
    error_str = str(error).lower()

    if any(x in error_str for x in ["connection", "timeout", "network", "refused", "unreachable"]):
        return "Error de conexión con Siigo. Verifica tu conexión a internet."

    if "auth" in error_str or "401" in error_str or "unauthorized" in error_str:
        return "Error de autenticación con Siigo. Verifica las credenciales."

    if "customer" in error_str and any(x in error_str for x in ["doesn't exist", "not found", "invalid"]):
        return "El cliente no existe en Siigo. Debe crearse primero en la plataforma."

    if hasattr(error, "response") and getattr(error, "response", None):
        response = error.response
        if isinstance(response, dict):
            errors = response.get("Errors", [])
            for err in errors:
                err_lower = str(err).lower()
                if "customer" in err_lower:
                    return "El cliente no existe en Siigo. Debe crearse primero en la plataforma."
                if "contact" in err_lower:
                    return "El cliente no tiene información de contacto en Siigo."

    return f"Error al procesar la factura: {str(error)}"

import logging
from datetime import datetime

from django.utils import timezone

logger = logging.getLogger(__name__)


import logging
from datetime import datetime

from django.utils import timezone

logger = logging.getLogger(__name__)

import logging
from datetime import datetime

from django.utils import timezone

logger = logging.getLogger(__name__)


import logging
from datetime import datetime

from django.utils import timezone
from django.db import transaction

logger = logging.getLogger(__name__)

import logging
from datetime import datetime

from django.utils import timezone

logger = logging.getLogger(__name__)

import json
import hashlib

def emit_electronic_invoice_preview(
    *,
    outbox_pk: int | None = None,   # ✅ NUEVO: si viene, actualiza ese mismo registro
    id_number: str,
    full_name: str,
    email: str,
    items: list,
) -> dict:
    """Emite una factura electrónica a través de Siigo.

    ✅ NUEVO FORMATO items (lista de dicts):
      - plate: str
      - parking: int  (monto parqueadero COP)
      - work: int     (monto servicio COP)
      - work_type: str (código MAYÚSCULAS: EMBARILLADO, DESEMBARILLADO, NONE, etc.)

    Reglas:
      - Por cada placa puede generar 0/1/2 líneas (parqueadero y/o servicio).
      - Si parking == 0 no se envía línea de parqueadero.
      - Si work == 0 o work_type == "NONE" no se envía línea de servicio.
    """

    # ✅ IMPORT LOCAL para que _save_outbox tenga acceso al modelo (y no reviente)
    from .models import ElectronicInvoiceOutbox

    data = {
        "id_number": (id_number or "").strip(),
        "full_name": (full_name or "").strip(),
        "email": (email or "").strip(),
    }

    def _normalize_items(raw_items: list) -> list:
        cleaned = []
        if not isinstance(raw_items, list):
            return cleaned

        def _to_int(x):
            try:
                return int(x or 0)
            except Exception:
                return 0

        for it in raw_items:
            if not isinstance(it, dict):
                continue

            plate = (it.get("plate") or "").strip().upper()

            # soporta llaves alternativas por si llegan con nombres distintos
            parking = _to_int(it.get("parking") if "parking" in it else it.get("parking_amount_cop"))
            work = _to_int(it.get("work") if "work" in it else it.get("work_amount_cop"))

            work_type = (it.get("work_type") or it.get("work_type_code") or "NONE").strip().upper() or "NONE"

            # ✅ entra si hay algo que facturar
            if plate and (parking > 0 or work > 0):
                cleaned.append(
                    {
                        "plate": plate,
                        "parking": parking,
                        "work": work,
                        "work_type": work_type,
                    }
                )

        return cleaned

    def _model_has_field(Model, field_name: str) -> bool:
        try:
            Model._meta.get_field(field_name)
            return True
        except Exception:
            return False

    def _safe_status(Model, requested: str) -> str:
        try:
            f = Model._meta.get_field("status")
            valid = {c[0] for c in (getattr(f, "choices", None) or [])}
            if valid and requested not in valid:
                print(">>> OUTBOX: status", requested, "NO está en choices. Bajo a PENDING.")
                return "PENDING"
        except Exception:
            pass
        return requested

    def _save_outbox(
        msg: str,
        outbox_items: list,
        *,
        status: str,
        extra: dict | None = None
    ) -> None:
        from .models import ElectronicInvoiceOutbox

        print(">>> OUTBOX: ENTER _save_outbox() | status=", status, "| msg=", (msg or "")[:120])

        outbox_items = _normalize_items(outbox_items)
        if data.get("total_amount_cop", 0) <= 0:
            print(">>> OUTBOX: SALIÓ porque total_amount_cop <= 0")
            return
        if not outbox_items:
            print(">>> OUTBOX: SALIÓ porque no hay items válidos")
            return

        status = _safe_status(ElectronicInvoiceOutbox, status)

        # --- items_hash ROBUSTO (NUNCA vacío) ---
        tmp = ElectronicInvoiceOutbox(
            id_number=data["id_number"],
            full_name=data["full_name"],
            email=data["email"],
            items=outbox_items,
            status="PENDING",
        )

        items_hash = ""
        if hasattr(tmp, "_compute_items_hash"):
            items_hash = (tmp._compute_items_hash() or "").strip()

        if not items_hash:
            payload = json.dumps(outbox_items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            items_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        print(">>> OUTBOX: items_hash =", items_hash)

        # =========================
        # ✅ MODO REENVÍO: ACTUALIZA MISMO PK
        # =========================
        if outbox_pk is not None:
            obj = ElectronicInvoiceOutbox.objects.filter(pk=outbox_pk).first()
            if obj:
                # ✅ regla: si ya está SENT, NO downgrade a ERROR en el MISMO registro
                if obj.status == "SENT" and status == "ERROR":
                    obj.last_error = (f"WARNING (no downgrade): {msg}" if msg else obj.last_error)[:2000]
                    obj.last_attempt_at = timezone.now()
                    fields = ["last_error", "last_attempt_at"]
                    if _model_has_field(ElectronicInvoiceOutbox, "updated_at"):
                        fields.append("updated_at")
                    obj.save(update_fields=fields)
                    print(">>> OUTBOX: pk=", obj.pk, "ya SENT, se evita downgrade a ERROR (reenvío)")
                    return

                # ✅ actualiza en el MISMO pk
                obj.id_number = data["id_number"]
                obj.full_name = data["full_name"]
                obj.email = data["email"]
                obj.items = outbox_items
                obj.items_hash = items_hash
                obj.status = status
                obj.last_attempt_at = timezone.now()

                if status != "ERROR":
                    obj.last_error = ""
                    if _model_has_field(ElectronicInvoiceOutbox, "sent_at"):
                        obj.sent_at = timezone.now()
                else:
                    obj.last_error = (msg or "")[:2000]

                # ✅ extras (si existen como campos)
                if extra:
                    for k, v in extra.items():
                        if _model_has_field(ElectronicInvoiceOutbox, k):
                            setattr(obj, k, v)

                # arma update_fields seguro
                update_fields = [
                    "id_number", "full_name", "email",
                    "items", "items_hash", "status",
                    "last_attempt_at", "last_error",
                ]
                if status != "ERROR" and _model_has_field(ElectronicInvoiceOutbox, "sent_at"):
                    update_fields.append("sent_at")
                if extra:
                    for k in extra.keys():
                        if _model_has_field(ElectronicInvoiceOutbox, k):
                            update_fields.append(k)
                if _model_has_field(ElectronicInvoiceOutbox, "updated_at"):
                    update_fields.append("updated_at")

                obj.save(update_fields=list(dict.fromkeys(update_fields)))
                print(">>> OUTBOX: UPDATED (forced pk) | pk=", obj.pk, "| status=", obj.status)
                return

            # si no existe el pk, cae al modo normal
            print(">>> OUTBOX: outbox_pk no existe. Caigo a upsert por (id_number,email,items_hash).")

        # =========================
        # ✅ MODO NORMAL (como hoy): upsert por triple llave
        # =========================

        existing = ElectronicInvoiceOutbox.objects.filter(
            id_number=data["id_number"],
            email=data["email"],
            items_hash=items_hash,
        ).first()

        # ✅ regla: si ya está SENT, NO downgrade a ERROR para la MISMA factura
        if existing and existing.status == "SENT" and status == "ERROR":
            existing.last_error = (f"WARNING (no downgrade): {msg}" if msg else existing.last_error)[:2000]
            existing.last_attempt_at = timezone.now()
            fields = ["last_error", "last_attempt_at"]
            if _model_has_field(ElectronicInvoiceOutbox, "updated_at"):
                fields.append("updated_at")
            existing.save(update_fields=fields)
            print(">>> OUTBOX: existing=SENT, se evita downgrade a ERROR (misma factura)")
            return

        defaults = {
            "id_number": data["id_number"],
            "full_name": data["full_name"],
            "email": data["email"],
            "items": outbox_items,
            "items_hash": items_hash,
            "status": status,
            "last_error": (msg or "")[:2000],
            "last_attempt_at": timezone.now(),
        }

        if status != "ERROR":
            defaults["last_error"] = ""
            if _model_has_field(ElectronicInvoiceOutbox, "sent_at"):
                defaults["sent_at"] = timezone.now()

        if extra:
            for k, v in extra.items():
                if _model_has_field(ElectronicInvoiceOutbox, k):
                    defaults[k] = v

        obj, created_flag = ElectronicInvoiceOutbox.objects.update_or_create(
            id_number=data["id_number"],
            email=data["email"],
            items_hash=items_hash,
            defaults=defaults,
        )

        print(">>> OUTBOX:", "CREATED" if created_flag else "UPDATED", "| pk=", obj.pk, "| status=", obj.status)

    # =========================
    # VALIDACIONES BÁSICAS
    # =========================
    if not data["id_number"]:
        return {
            "success": False,
            "error": "Factura electrónica requiere NIT o cédula",
            "code": "MISSING_ID_NUMBER",
            **data,
        }
    if not data["full_name"]:
        return {
            "success": False,
            "error": "Factura electrónica requiere nombre o razón social",
            "code": "MISSING_FULL_NAME",
            **data,
        }
    if not data["email"]:
        return {
            "success": False,
            "error": "Factura electrónica requiere correo electrónico",
            "code": "MISSING_EMAIL",
            **data,
        }
    if not isinstance(items, list) or not items:
        return {
            "success": False,
            "error": "Factura electrónica requiere al menos un ítem",
            "code": "MISSING_ITEMS",
            **data,
        }

    # =========================
    # CONSTRUIR ITEMS + TOTAL
    # =========================
    tax_config = [{"id": SIIGO_CONFIG["tax_id"]}] if SIIGO_CONFIG.get("tax_id") else []
    siigo_items = []
    total = 0

    normalized = _normalize_items(items)
    if not normalized:
        return {
            "success": False,
            "error": "Factura electrónica requiere al menos un ítem válido (parking/work > 0).",
            "code": "MISSING_VALID_ITEMS",
            **data,
        }

    for i, item in enumerate(normalized, 1):
        plate_u = (item.get("plate") or "").strip().upper()
        if not plate_u:
            return {
                "success": False,
                "error": f"Item {i}: falta 'plate'.",
                "code": "MISSING_ITEM_PLATE",
                "item_index": i,
                **data,
            }

        parking = int(item.get("parking") or 0)
        work = int(item.get("work") or 0)
        work_type = (item.get("work_type") or "NONE").strip().upper() or "NONE"

        # ✅ Línea parqueadero (si > 0)
        if parking > 0:
            siigo_items.append(
                {
                    "code": SIIGO_CONFIG["product_code"],
                    "description": f"PARQUEADERO {plate_u}",
                    "quantity": 1,
                    "price": parking,
                    "discount": 0,
                    "taxes": tax_config,
                }
            )
            total += parking

        # ✅ Línea servicio (si > 0 y tipo != NONE)
        if work > 0 and work_type != "NONE":
            siigo_items.append(
                {
                    "code": SIIGO_CONFIG["product_code"],
                    "description": f"{work_type} {plate_u}",
                    "quantity": 1,
                    "price": work,
                    "discount": 0,
                    "taxes": tax_config,
                }
            )
            total += work

    data["total_amount_cop"] = int(total)
    print(">>> EMIT: entro a emit_electronic_invoice_preview()", {**data, "total_amount_cop": data["total_amount_cop"]})

    if data["total_amount_cop"] <= 0 or not siigo_items:
        return {
            "success": False,
            "error": "Monto total inválido para facturación",
            "code": "INVALID_TOTAL",
            **data,
        }

    # =========================
    # VALIDAR CONFIG
    # =========================
    try:
        _validate_config()
    except Exception as e:
        msg = _get_friendly_error(e)
        _save_outbox(msg, normalized, status="ERROR")
        return {"success": False, "error": msg, "code": "SIIGO_NOT_CONFIGURED", **data}

    # =========================
    # ENVIAR A SIIGO
    # =========================
    try:
        client = SiigoClient(
            username=SIIGO_USERNAME,
            access_key=SIIGO_ACCESS_KEY,
            application_name=SIIGO_APP_NAME,
        )

        today = datetime.now().strftime("%Y-%m-%d")

        invoice_data = {
            "document": {"id": SIIGO_CONFIG["document_type_id"]},
            "date": today,
            "customer": {"identification": data["id_number"], "branch_office": 0},
            "seller": SIIGO_CONFIG["seller_id"],
            "items": siigo_items,
            "payments": [
                {
                    "id": SIIGO_CONFIG["payment_type_id"],
                    "value": total,
                    "due_date": today,
                }
            ],
        }

        result = client.create_invoice(invoice_data)

        siigo_id = result.get("id")
        siigo_number = result.get("name")
        siigo_date = result.get("date")

        print(">>> EMIT: Siigo OK. result.id=", siigo_id, "result.name=", siigo_number)

        _save_outbox(
            "",
            normalized,
            status="SENT",
            extra={"siigo_id": siigo_id, "siigo_number": siigo_number, "siigo_date": siigo_date},
        )

        return {
            "success": True,
            "id": siigo_id,
            "number": siigo_number,
            "total": result.get("total"),
            "date": siigo_date,
            **data,
        }

    except (SiigoAuthError, SiigoAPIError) as e:
        msg = _get_friendly_error(e)
        _save_outbox(msg, normalized, status="ERROR")
        return {"success": False, "error": msg, "code": "SIIGO_API_ERROR", **data}

    except Exception as e:
        msg = _get_friendly_error(e)
        _save_outbox(msg, normalized, status="ERROR")
        return {"success": False, "error": msg, "code": "UNEXPECTED_ERROR", **data}

def fmt_cop(n: int) -> str:
    try:
        n = int(n or 0)
    except Exception:
        n = 0
    return "{:,.0f}".format(n).replace(",", ".")


def client_label(ticket: Ticket) -> str:
    try:
        return str(ticket.get_client_kind_display() or "").strip()
    except Exception:
        return str(getattr(ticket, "client_kind", "") or "").strip()


def is_taller_ticket(ticket: Ticket) -> bool:
    raw = str(getattr(ticket, "client_kind", "") or "").strip().upper()
    return raw == "WORKSHOP"


def ticket_has_service(ticket: Ticket) -> bool:
    wtype = (getattr(ticket, "work_type", "") or "").strip().upper()
    wamt = int(getattr(ticket, "work_amount_cop", 0) or 0)
    if wtype and wtype != "NONE" and wamt > 0:
        return True

    stype = (getattr(ticket, "service_type", "") or "").strip().upper()
    samt = int(getattr(ticket, "service_amount_cop", 0) or 0)
    return bool(stype and stype != "NONE" and samt > 0)


def ticket_service_display(ticket: Ticket):
    wtype = (getattr(ticket, "work_type", "") or "").strip()
    wamt = int(getattr(ticket, "work_amount_cop", 0) or 0)
    if wtype and wtype.upper() != "NONE" and wamt > 0:
        return (wtype, wamt)

    stype = (getattr(ticket, "service_type", "") or "").strip()
    samt = int(getattr(ticket, "service_amount_cop", 0) or 0)
    if stype and stype.upper() != "NONE" and samt > 0:
        return (stype, samt)

    return ("NONE", 0)


def force_credit_disabled(customer: Customer):
    """
    Forzar "habilitado para crédito" en False si el campo existe.
    """
    for attr in ("credit_enabled", "enabled_for_credit", "allow_credit", "is_credit_enabled"):
        if hasattr(customer, attr):
            try:
                setattr(customer, attr, False)
                customer.save(update_fields=[attr])
            except Exception:
                pass
            break


# ============================================================
# ✅ Cálculo oficial: la lógica real vive en Ticket.compute_amount_cop()
# ============================================================

def estimate_amount_cop(ticket: Ticket, now=None) -> int:
    """
    Usa la lógica oficial del MODELO:
      - PARKING: segmentos 06-18 (HOUR) / 18-06 (NIGHT) + day_type
      - WORKSHOP: noches tocadas (noche/fracción) + day_type
      - + extras
    """
    now = now or timezone.now()
    try:
        return int(ticket.compute_amount_cop(end_time=now) or 0)
    except Exception:
        return 0


def estimate_units_for_ui(ticket: Ticket, now=None):
    """
    SOLO para mostrar algo en UI (NO afecta el cobro).
    Retorna (units, plan_name)

    - PARKING: suma horas (día) + noches tocadas (noche)
    - WORKSHOP: noches tocadas
    """
    now = now or timezone.now()
    start = getattr(ticket, "check_in", None)
    if not start:
        return (1, "—")

    night_units = 0
    hour_units = 0

    for seg_start, seg_end, bu, _ in iter_pricing_segments(start, now):
        seconds = (seg_end - seg_start).total_seconds()
        if seconds <= 0:
            continue

        if bu == "NIGHT":
            night_units += 1
        else:
            minutes = max(1, int(math.ceil(seconds / 60.0)))
            hour_units += max(1, int(math.ceil(minutes / 60.0)))

    if is_taller_ticket(ticket):
        units = max(1, night_units)
        plan = "Noche / Fracción"
    else:
        units = max(1, night_units + hour_units)
        if night_units > 0 and hour_units > 0:
            plan = "Mixto (Día + Noche)"
        elif night_units > 0:
            plan = "Noche / Fracción"
        else:
            plan = "Hora / Fracción"

    return (units, plan)
