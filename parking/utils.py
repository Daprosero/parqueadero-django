import math
from django.utils import timezone
from .models import Ticket, Customer, iter_pricing_segments
# parking/utils.py
import os
from datetime import datetime
from .siigo_client import SiigoClient, SiigoAPIError, SiigoAuthError



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



def emit_electronic_invoice_preview(
    *,
    id_number: str,
    full_name: str,
    email: str,
    total_amount_cop: int,
) -> dict:
    """
    Emite una factura electrónica a través de Siigo.

    - ✅ Si es EXITOSO: retorna dict con la factura (NO guarda en BD).
    - ✅ Si FALLA: guarda/actualiza ElectronicInvoiceOutbox (sin duplicar)
      y retorna {"success": False, ...} (NO lanza excepción).
    """

    data = {
        "id_number": (id_number or "").strip(),
        "full_name": (full_name or "").strip(),
        "email": (email or "").strip(),
        "total_amount_cop": int(total_amount_cop or 0),
    }

    def _save_outbox(msg: str) -> None:
        """Guardar/actualizar Outbox sin duplicar y sin romper el flujo."""
        try:
            from .models import ElectronicInvoiceOutbox

            obj, created = ElectronicInvoiceOutbox.objects.get_or_create(
                id_number=data["id_number"],
                full_name=data["full_name"],
                email=data["email"],
                total_amount_cop=data["total_amount_cop"],
                defaults={
                    "status": "PENDING",
                    "last_error": (msg or "")[:2000],
                    "last_attempt_at": timezone.now(),
                },
            )
            if not created:
                obj.status = "PENDING"
                obj.last_error = (msg or "")[:2000]
                obj.last_attempt_at = timezone.now()
                obj.save(update_fields=["status", "last_error", "last_attempt_at", "updated_at"])
        except Exception:
            # Nunca rompas el flujo por fallas al persistir outbox
            pass

    # =========================
    # Validaciones (ANTES: raise)
    # AHORA: guardar outbox + return success=False
    # =========================
    if not data["id_number"]:
        msg = "Factura electrónica requiere NIT o cédula"
        _save_outbox(msg)
        return {"success": False, "error": msg, "code": "MISSING_ID_NUMBER", **data}

    if not data["full_name"]:
        msg = "Factura electrónica requiere nombre o razón social"
        _save_outbox(msg)
        return {"success": False, "error": msg, "code": "MISSING_FULL_NAME", **data}

    if not data["email"]:
        msg = "Factura electrónica requiere correo electrónico"
        _save_outbox(msg)
        return {"success": False, "error": msg, "code": "MISSING_EMAIL", **data}

    if data["total_amount_cop"] <= 0:
        msg = "Monto total inválido para facturación"
        _save_outbox(msg)
        return {"success": False, "error": msg, "code": "INVALID_TOTAL", **data}

    # Config Siigo (ANTES podía lanzar ValueError)
    try:
        _validate_config()
    except Exception as e:
        msg = _get_friendly_error(e)
        _save_outbox(msg)
        return {"success": False, "error": msg, "code": "SIIGO_NOT_CONFIGURED", **data}

    # =========================
    # Siigo
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
            "items": [{
                "code": SIIGO_CONFIG["product_code"],
                "description": f"Servicio - {data['full_name']}",
                "quantity": 1,
                "price": data["total_amount_cop"],
                "discount": 0,
                "taxes": (
                    [{"id": SIIGO_CONFIG["tax_id"]}]
                    if SIIGO_CONFIG.get("tax_id") else []
                ),
            }],
            "payments": [{
                "id": SIIGO_CONFIG["payment_type_id"],
                "value": data["total_amount_cop"],
                "due_date": today,
            }],
        }

        result = client.create_invoice(invoice_data)

        # ✅ éxito: NO guardamos outbox
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
        _save_outbox(msg)
        return {"success": False, "error": msg, "code": "SIIGO_API_ERROR", **data}

    except Exception as e:
        msg = _get_friendly_error(e)
        _save_outbox(msg)
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
