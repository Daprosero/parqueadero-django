import math
from django.utils import timezone

from .models import Ticket, Customer, iter_pricing_segments

# utils.py

def emit_electronic_invoice_preview(
    *,
    id_number: str,
    full_name: str,
    email: str,
    total_amount_cop: int,
) -> dict:
    """
    Construye y valida los datos mínimos para factura electrónica.
    Por ahora solo imprime (preview).
    """

    data = {
        "id_number": (id_number or "").strip(),
        "full_name": (full_name or "").strip(),
        "email": (email or "").strip(),
        "total_amount_cop": int(total_amount_cop or 0),
    }

    # 🔒 Validaciones mínimas
    if not data["id_number"]:
        raise ValueError("Factura electrónica requiere NIT o cédula")

    if not data["full_name"]:
        raise ValueError("Factura electrónica requiere nombre o razón social")

    if not data["email"]:
        raise ValueError("Factura electrónica requiere correo electrónico")

    if data["total_amount_cop"] <= 0:
        raise ValueError("Monto total inválido para facturación")

    # 🧾 PREVIEW (por ahora)
    print("====== FACTURA ELECTRÓNICA (PREVIEW) ======")
    print(f"NIT / Cédula : {data['id_number']}")
    print(f"Nombre       : {data['full_name']}")
    print(f"Correo       : {data['email']}")
    print(f"Total (COP)  : {data['total_amount_cop']:,}".replace(",", "."))
    print("==========================================")

    return data

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
