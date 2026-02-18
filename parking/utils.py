import math
from django.utils import timezone
from .models import Ticket, Customer, iter_pricing_segments
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

###Nuevo##
def emit_electronic_invoice_preview(
    *,
    id_number: str,
    full_name: str,
    email: str,
    items: list,
) -> dict:
    """Emite una factura electrónica a través de Siigo."""

    data = {
        "id_number": (id_number or "").strip(),
        "full_name": (full_name or "").strip(),
        "email": (email or "").strip(),
    }

    # Normalización mínima de items para:
    # - usarla en outbox tal cual (plate/price)
    # - asegurar hash estable (si tu modelo lo calcula)
    def _normalize_items(raw_items: list) -> list:
        cleaned = []
        if not isinstance(raw_items, list):
            return cleaned
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            plate = (it.get("plate") or "").strip().upper()
            try:
                price = int(it.get("price") or 0)
            except Exception:
                price = 0
            if plate and price > 0:
                cleaned.append({"plate": plate, "price": price})
        return cleaned

    def _save_outbox(msg: str, outbox_items: list) -> None:
        """
        Guardar/actualizar Outbox SOLO si hay total real > 0.
        Camino 2 (ideal): guardamos items + items_hash.
        """
        try:
            if data.get("total_amount_cop", 0) <= 0:
                return  # 🔒 NUNCA guardar si el total es 0

            from .models import ElectronicInvoiceOutbox

            outbox_items = _normalize_items(outbox_items)
            if not outbox_items:
                return  # sin items válidos, no guardamos (tu modelo lo exige)

            # Calcula hash estable usando el método del modelo
            tmp = ElectronicInvoiceOutbox(
                id_number=data["id_number"],
                full_name=data["full_name"],
                email=data["email"],
                items=outbox_items,
                status="PENDING",
            )
            items_hash = tmp._compute_items_hash()

            # Upsert por id_number + email + items_hash (evita duplicados idénticos)
            ElectronicInvoiceOutbox.objects.update_or_create(
                id_number=data["id_number"],
                email=data["email"],
                items_hash=items_hash,
                defaults={
                    "full_name": data["full_name"],
                    "items": outbox_items,
                    "status": "PENDING",
                    "last_error": (msg or "")[:2000],
                    "last_attempt_at": timezone.now(),
                    # total_amount_cop lo recalcula el clean() desde items
                },
            )

        except Exception:
            # 🔒 Nunca romper flujo por error en Outbox
            pass

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

    for i, item in enumerate(items, 1):
        plate = (item.get("plate") or "").strip() if isinstance(item, dict) else ""
        if not plate:
            return {
                "success": False,
                "error": f"Item {i}: falta 'plate'.",
                "code": "MISSING_ITEM_PLATE",
                "item_index": i,
                **data,
            }

        try:
            price = int(item.get("price")) if isinstance(item, dict) else 0
        except Exception:
            price = 0

        if price <= 0:
            return {
                "success": False,
                "error": f"Item {i}: 'price' debe ser mayor a 0.",
                "code": "INVALID_ITEM_PRICE",
                "item_index": i,
                **data,
            }

        plate_u = plate.strip().upper()

        siigo_items.append(
            {
                "code": SIIGO_CONFIG["product_code"],
                "description": f"PARQUEADERO {plate_u}",
                "quantity": 1,
                "price": price,
                "discount": 0,
                "taxes": tax_config,
            }
        )

        total += price

    data["total_amount_cop"] = int(total)

    if data["total_amount_cop"] <= 0:
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
        _save_outbox(msg, items)
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
            "customer": {
                "identification": data["id_number"],
                "branch_office": 0,
            },
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

        return {
            "success": True,
            "id": result.get("id"),
            "number": result.get("name"),
            "total": result.get("total"),
            "date": result.get("date"),
            **data,
        }

    except (SiigoAuthError, SiigoAPIError) as e:
        msg = _get_friendly_error(e)
        _save_outbox(msg, items)
        return {"success": False, "error": msg, "code": "SIIGO_API_ERROR", **data}

    except Exception as e:
        msg = _get_friendly_error(e)
        _save_outbox(msg, items)
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
