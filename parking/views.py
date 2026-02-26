
from urllib.parse import quote
from django import views
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
import math
from django.db import transaction
import json
import re
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db.models import Sum, Count
from django.conf import settings
from datetime import timedelta
import traceback
from django.db.models.functions import TruncDate
from django.db.models import Max
from .forms import (
    ClosePaymentForm,
    CompanySettleForm,  # ✅ NUEVO
    CustomerForm,
    EditActiveTicketForm,
    EditPaidServiceForm,
    InspectForm,
    OperarioForm,
    OperarioPasswordForm,
    RatePlanForm,
    TicketCreateForm,
    VehicleTypeForm,
    MonthlyPlateForm,
    EInvoiceOutboxForm,
)

from .models import (
    Closure,
    Customer,
    Payment,
    RatePlan,
    Ticket,
    VehicleType,
    iter_pricing_segments,
    MonthlyPlate,
    ElectronicInvoiceOutbox,
    WorkType,
    ActiveVehiclesReport,
    MonthlyReceipt,
    CompanyReceiptNumber,
)
from .utils import (
    fmt_cop,
    client_label,
    is_taller_ticket,
    ticket_has_service,
    ticket_service_display,
    force_credit_disabled,
    estimate_amount_cop,
    emit_electronic_invoice_preview,
    resolve_or_create_customer, 
    normalize_id_number,
)


##Nuevo##
def _yard_counts():
    """
    Conteo de vehículos en patio.
    - Tickets ACTIVE: vehículos con registro de ingreso (no han salido)
    - MonthlyPlate: clientes mensuales (sin entrada/salida en BD), se suman como 'en patio'
    """
    active_tickets = Ticket.objects.filter(status="ACTIVE").count()
    monthly_in_yard = MonthlyPlate.objects.count()
    return active_tickets, monthly_in_yard, (active_tickets + monthly_in_yard)

def home(request):
    return redirect("parking:post_login_redirect")


@login_required
def post_login_redirect(request):
    user = request.user
    if user.is_staff or user.is_superuser:
        return redirect("parking:gestion")          # ✅ tu panel de gestión
    return redirect("parking:operator_panel") 

def _safe_next(request, next_url: str) -> str:
    """
    Permite redirigir SOLO a URLs internas del mismo host.
    """
    next_url = (next_url or "").strip()
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return next_url
    return ""


# =========================
# Editar ticket ACTIVO
# =========================

@login_required
def ticket_edit_active(request, ticket_id: int):
    ticket = get_object_or_404(Ticket, id=ticket_id)

    next_url = _safe_next(request, request.GET.get("next") or request.POST.get("next") or "")

    if ticket.status != "ACTIVE":
        messages.error(request, "Solo puedes editar tickets ACTIVOS.")
        return redirect(next_url or "parking:operator_panel")

    # ✅ AJUSTE: cualquier operario puede editar ACTIVE (no solo quien lo creó)
    # Solo admin mantiene permisos extra (como eliminar, etc.), pero editar ACTIVE lo puede hacer cualquier operario.
    # is_admin = request.user.is_staff or request.user.is_superuser  # (se mantiene si lo necesitas luego)

    if request.method == "POST":
        form = EditActiveTicketForm(request.POST, instance=ticket)
        if form.is_valid():
            form.save()
            messages.success(request, "Ticket activo actualizado.")
            return redirect(next_url or "parking:operator_panel")
    else:
        form = EditActiveTicketForm(instance=ticket)

    return render(request, "parking/ticket_edit_active.html", {
        "ticket": ticket,
        "form": form,
        "next_url": next_url,
    })



# =========================
# Eliminar ticket ACTIVO (solo admin)
# =========================

@staff_member_required(login_url="parking:login")
@require_POST
def ticket_delete_active(request, ticket_id: int):
    ticket = get_object_or_404(Ticket, id=ticket_id)

    next_url = _safe_next(request, request.GET.get("next") or request.POST.get("next") or "")

    if ticket.status != "ACTIVE":
        messages.error(request, "Solo puedes eliminar tickets ACTIVOS.")
        return redirect(next_url or "parking:gestion")

    plate = ticket.plate
    ticket.delete()
    messages.success(request, f"Ticket {plate} eliminado correctamente.")

    return redirect(next_url or "parking:gestion")


# =========================
# Editar servicio (PAID / PENDING / ASSIGNED)  ✅ SOLO ADMIN
# =========================
@staff_member_required(login_url="parking:login")
def ticket_edit_paid(request, ticket_id: int):
    ticket = get_object_or_404(Ticket, id=ticket_id)

    next_url = _safe_next(request, request.GET.get("next") or request.POST.get("next") or "")

    if ticket.status not in ("PAID", "PENDING", "ASSIGNED"):
        messages.error(request, "Solo puedes editar servicio en tickets PAGADOS, PENDIENTES o ASIGNADOS.")
        return redirect(next_url or "parking:gestion")

    # (staff_member_required ya filtra, pero lo dejo por compatibilidad con tu flujo)
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "No autorizado.")
        return redirect(next_url or "parking:operator_panel")

    if request.method == "POST":
        form = EditPaidServiceForm(request.POST)
        if form.is_valid():
            wt = form.cleaned_data.get("service_type")  # WorkType o None
            new_amt = int(form.cleaned_data.get("service_amount_cop") or 0)

            # NONE => 0 (robusto)
            wt_code = ((getattr(wt, "code", "") or "").strip().upper() if wt else "NONE")
            if wt is None or wt_code in ("", "NONE"):
                wt = None
                new_amt = 0

            with transaction.atomic():
                # Bloquea fila para evitar condiciones raras si se edita en paralelo
                ticket = Ticket.objects.select_for_update().get(id=ticket.id)

                # 1) Congelar base actual (parqueadero/taller) desde el total guardado
                total_old = int(ticket.total_amount_cop or 0)
                old_extras = int(ticket.work_amount_cop or 0) + int(ticket.service_amount_cop or 0)
                base_old = max(0, total_old - old_extras)

                # 2) Aplicar nuevo servicio en work_* (tu estrategia nueva)
                #    Usa tu setter: acepta WorkType o str
                ticket.work_type = wt if wt else "NONE"
                ticket.work_amount_cop = new_amt

                #    Apaga legacy (para que nada "sume dos veces")
                ticket.service_type = "NONE"
                ticket.service_amount_cop = 0

                # 3) Re-armar total = base congelada + extras nuevos
                new_extras = int(ticket.work_amount_cop or 0) + int(ticket.service_amount_cop or 0)
                ticket.total_amount_cop = int(base_old + new_extras)

                # Guardar (incluye tus campos FK/text por el setter)
                ticket.save(update_fields=[
                    "work_type_fk", "work_type_text",
                    "work_amount_cop",
                    "service_type", "service_amount_cop",
                    "total_amount_cop",
                ])

                # 4) Sync Payment para que el recibo use el total correcto
                if getattr(ticket, "payment", None):
                    payment_obj = ticket.payment
                    if int(payment_obj.amount_cop or 0) != int(ticket.total_amount_cop or 0):
                        payment_obj.amount_cop = ticket.total_amount_cop
                        payment_obj.save(update_fields=["amount_cop"])
                else:
                    payment_obj, _ = Payment.objects.get_or_create(
                        ticket=ticket,
                        defaults={
                            "method": "CASH" if ticket.status == "PAID" else "CREDIT",
                            "status": "PAID" if ticket.status == "PAID" else "PENDING",
                            "amount_cop": ticket.total_amount_cop,
                        }
                    )
                    if int(payment_obj.amount_cop or 0) != int(ticket.total_amount_cop or 0):
                        payment_obj.amount_cop = ticket.total_amount_cop
                        payment_obj.save(update_fields=["amount_cop"])

            messages.success(request, "Servicio actualizado. Puedes reimprimir el recibo.")

            receipt_url = reverse("parking:print_receipt", kwargs={"payment_id": payment_obj.id})
            if next_url:
                receipt_url += f"?next={next_url}"
            return redirect(receipt_url)

        messages.error(request, "Revisa los campos marcados.")

    else:
        # Inicial del form:
        # - muestra el servicio actual desde work_type (tu compatibilidad devuelve string code)
        stxt = (ticket.work_type or "NONE").strip().upper() or "NONE"
        samt = int(ticket.work_amount_cop or 0)

        wt_obj = WorkType.objects.filter(code__iexact=stxt, active=True).first()
        form = EditPaidServiceForm(initial={"service_type": wt_obj, "service_amount_cop": samt})

    return render(request, "parking/ticket_edit_paid.html", {
        "ticket": ticket,
        "form": form,
        "next_url": next_url,
        "back_url": next_url,
    })



# =========================
# Imprimir / reimprimir recibo
# =========================

@login_required
def print_receipt(request, payment_id):
    payment = get_object_or_404(Payment, id=payment_id)
    ticket = payment.ticket
    start = ticket.check_in
    end = ticket.check_out or timezone.now()
    is_taller = is_taller_ticket(ticket)

    next_url = _safe_next(request, request.GET.get("next") or "")

    default_finish = (
        reverse("parking:gestion")
        if (request.user.is_staff or request.user.is_superuser)
        else reverse("parking:operator_panel")
    )
    finish_url = next_url or default_finish

    # ✅ BLOQUEO BACKEND: si el total es 0, NO se genera/mostrar recibo
    total_val = int(payment.amount_cop or 0)
    if total_val <= 0:
        messages.warning(request, "No se puede generar recibo con monto $0.")
        return redirect(finish_url)

        # Alternativa (más estricta): devolver 403
        # from django.http import HttpResponseForbidden
        # return HttpResponseForbidden("No se puede generar recibo con monto $0.")

    # ✅ Duración (display)
    if is_taller:
        night_count = 0
        for _, __, bu, ___ in iter_pricing_segments(start, end):
            if bu == "NIGHT":
                night_count += 1
        if night_count <= 0:
            night_count = 1
        duration_str = f"{night_count} Noche(s)"
    else:
        delta = end - start
        total_seconds = int(delta.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 3600 % 60
        duration_str = f"{hours}h {minutes}m {seconds}s"

    service_val = int(ticket.work_amount_cop or 0) + int(ticket.service_amount_cop or 0)
    parking_val = max(0, total_val - service_val)

    service_name = "Ninguno"
    if ticket.work_type and ticket.work_type != "NONE":
        try:
            service_name = ticket.get_work_type_display()
        except Exception:
            service_name = ticket.work_type
    elif ticket.service_type and ticket.service_type != "NONE":
        service_name = ticket.service_type

    context = {
        "payment": payment,
        "ticket": ticket,
        "duration_str": duration_str,
        "parking_val_fmt": fmt_cop(parking_val),
        "service_val_fmt": fmt_cop(service_val),
        "total_fmt": fmt_cop(total_val),
        "has_service": service_val > 0,
        "service_name": service_name,
        "is_taller": is_taller,
        "client_type_label": client_label(ticket),
        "finish_url": finish_url,
    }
    return render(request, "parking/receipt_print.html", context)



# =========================
# Admin: edición de cierres
# =========================

@staff_member_required(login_url="parking:login")
def closure_edit_panel(request):
    # Limpia flags de recalcular
    try:
        keys_to_delete = [k for k in request.session.keys() if k.startswith("closure_") and k.endswith("_recalc_ok")]
        for k in keys_to_delete:
            request.session.pop(k, None)
        if keys_to_delete:
            request.session.modified = True
    except Exception:
        pass

    start = (request.GET.get("start") or "").strip()
    end = (request.GET.get("end") or "").strip()

    qs = Closure.objects.all().order_by("-date")
    tz = timezone.get_current_timezone()

    if start:
        try:
            d = timezone.datetime.fromisoformat(start).date()
            dt_start = timezone.make_aware(
                timezone.datetime(d.year, d.month, d.day, 0, 0, 0),
                tz
            )
            qs = qs.filter(date__gte=dt_start)
        except Exception:
            messages.error(request, "Fecha 'start' inválida (usa YYYY-MM-DD).")

    if end:
        try:
            d = timezone.datetime.fromisoformat(end).date()
            dt_end = timezone.make_aware(
                timezone.datetime(d.year, d.month, d.day, 23, 59, 59, 999999),
                tz
            )
            qs = qs.filter(date__lte=dt_end)
        except Exception:
            messages.error(request, "Fecha 'end' inválida (usa YYYY-MM-DD).")

    closures = list(qs[:200])

    return render(request, "parking/closure_edit_panel.html", {
        "closures": closures,
        "start": start,
        "end": end,
    })


@staff_member_required(login_url="parking:login")
def closure_edit_detail(request, closure_id: int):
    closure = get_object_or_404(Closure, id=closure_id)

    session_key = f"closure_{closure.id}_recalc_ok"

    from_recalc = (request.GET.get("from_recalc") == "1")
    if request.method == "GET" and not from_recalc:
        request.session[session_key] = False
        request.session.modified = True

    recalc_ok = bool(request.session.get(session_key, False))

    next_url_raw = request.get_full_path()
    next_url_q = quote(next_url_raw, safe="/?=&")

    tickets = list(
        closure.tickets
        .all()
        .select_related("vehicle_type")
        .select_related("payment")
        .order_by("-check_out", "-check_in")
    )

    for t in tickets:
        total = int(t.total_amount_cop or 0)
        svc = int(getattr(t, "work_amount_cop", 0) or 0) + int(getattr(t, "service_amount_cop", 0) or 0)
        pkg = max(0, total - svc)

        if t.status == "ASSIGNED":
            method = "CREDIT"
        elif t.status == "PENDING":
            method = "PENDING"
        else:
            method = getattr(getattr(t, "payment", None), "method", None) or "CASH"

        t.ui_total = fmt_cop(total)
        t.ui_parking = fmt_cop(pkg)
        t.ui_service = fmt_cop(svc)
        t.ui_method = method

        t.url_edit = f"{reverse('parking:ticket_edit_paid', kwargs={'ticket_id': t.id})}?next={next_url_q}"

        if getattr(t, "payment", None):
            t.url_receipt = f"{reverse('parking:print_receipt', kwargs={'payment_id': t.payment.id})}?next={next_url_q}"
        else:
            t.url_receipt = ""

    reprint_url = f"{reverse('parking:reprint_closure', kwargs={'closure_id': closure.id})}?next={next_url_q}"

    return render(request, "parking/closure_edit_detail.html", {
        "closure": closure,
        "tickets": tickets,
        "next_url": next_url_raw,
        "reprint_url": reprint_url,
        "recalc_ok": recalc_ok,
    })


@staff_member_required(login_url="parking:login")
def closure_recalc(request, closure_id: int):
    closure = get_object_or_404(Closure, id=closure_id)

    total = 0
    for t in closure.tickets.all().only("total_amount_cop", "status"):
        if getattr(t, "status", "") == "PENDING":
            continue
        total += int(t.total_amount_cop or 0)

    closure.total_amount = int(total)
    closure.save(update_fields=["total_amount"])

    session_key = f"closure_{closure.id}_recalc_ok"
    request.session[session_key] = True
    request.session.modified = True

    messages.success(request, f"Cierre #{closure.id} recalculado: ${fmt_cop(total)}")

    return redirect(f"{reverse('parking:closure_edit_detail', kwargs={'closure_id': closure.id})}?from_recalc=1")

#####Cambio####
@login_required(login_url="parking:login")
def reprint_closure(request, closure_id):
    closure = get_object_or_404(Closure, id=closure_id)
    tickets_list = closure.tickets.all().select_related("payment")

    total_parking = 0
    total_service = 0
    total_general = 0
    total_cash = 0
    total_transfer = 0
    total_credit = 0
    total_pending = 0
    active_tickets_count, monthly_count, active_count = _yard_counts()


    for t in tickets_list:
        amt = int(t.total_amount_cop or 0)
        svc = int(getattr(t, "work_amount_cop", 0) or 0) + int(getattr(t, "service_amount_cop", 0) or 0)
        pkg = max(0, amt - svc)

        if t.status == "ASSIGNED":
            method = "CREDIT"
        elif t.status == "PENDING":
            method = "PENDING"
        else:
            method = getattr(getattr(t, "payment", None), "method", None) or "CASH"

        if method == "PENDING":
            total_pending += amt
        else:
            total_parking += pkg
            total_service += svc
            total_general += amt

            if method == "CASH":
                total_cash += amt
            elif method == "TRANSFER":
                total_transfer += amt
            elif method == "CREDIT":
                total_credit += amt

        t.print_parking = fmt_cop(pkg)
        t.print_service = fmt_cop(svc)
        t.print_payment_method = method

    next_url = _safe_next(request, request.GET.get("next") or "")
    default_finish = (
        reverse("parking:gestion")
        if (request.user.is_staff or request.user.is_superuser)
        else reverse("parking:operator_panel")
    )
    finish_url = next_url or default_finish

    context = {
        "closure": closure,
        "tickets": tickets_list,
        "active_tickets_count": active_tickets_count,
        "monthly_count": monthly_count,
        "active_count": active_count,
        "total_parking": fmt_cop(total_parking),
        "total_service": fmt_cop(total_service),
        "total_general": fmt_cop(total_general),
        "total_cash": fmt_cop(total_cash),
        "total_transfer": fmt_cop(total_transfer),
        "total_credit": fmt_cop(total_credit),
        "total_pending": fmt_cop(total_pending),
        "finish_url": finish_url,
    }
    return render(request, "parking/report_print.html", context)


# =========================
# Operario panel
# =========================

@login_required
def operator_panel(request):
    mode = (request.GET.get("mode") or "menu").strip()
    is_admin = bool(request.user.is_staff or request.user.is_superuser)

    # AJAX lookup customer
    # AJAX lookup customer (NIT o Nombre)
    if mode == "lookup_customer":
        by = (request.GET.get("by") or "nit").strip().lower()   # nit | name
        q  = (request.GET.get("q")  or "").strip()

        # compatibilidad con tu JS viejo (?id=...)
        if not q:
            q = (request.GET.get("id") or "").strip()
            by = "nit"

        if not q:
            return JsonResponse({"found": False, "mode": "single", "results": []})

        # 1) Buscar por NIT/CC exacto (con normalización)
        if by in ("nit", "id", "id_number"):
            q_raw = q
            q_norm = normalize_id_number(q)

            customer = (
                Customer.objects.filter(id_number=q_raw).first()
                or Customer.objects.filter(id_number=q_norm).first()
            )
            if customer:
                return JsonResponse({
                    "found": True,
                    "mode": "single",
                    "full_name": customer.full_name,
                    "email": customer.email,
                    "id_number": customer.id_number,
                    "is_company": customer.is_company,
                    "credit_enabled": getattr(customer, "credit_enabled", False),
                })
            return JsonResponse({"found": False, "mode": "single"})

        # 2) Buscar por nombre (lista de sugerencias)
        qs = (
            Customer.objects.filter(full_name__icontains=q, active=True)
            .order_by("full_name")[:10]
        )
        results = [{
            "id_number": c.id_number,
            "full_name": c.full_name,
            "email": c.email,
            "is_company": c.is_company,
            "credit_enabled": getattr(c, "credit_enabled", False),
        } for c in qs]

        return JsonResponse({
            "found": len(results) > 0,
            "mode": "list",
            "results": results
        })

    # AJAX: quote (estimación)
    # - ACTIVE: usa estimate_amount_cop() (cobro por tramos)
    # - PENDING: devuelve el total ya congelado
    # - ✅ NUEVO: si viene ticket_id, usa ESE ticket (evita ambigüedad por placa)
    # =========================
    if mode == "quote" and request.headers.get("x-requested-with") == "XMLHttpRequest":
        plate = (request.GET.get("plate", "") or "").strip().upper().replace(" ", "").replace("-", "")
        ticket_id = (request.GET.get("ticket_id") or "").strip()

        # ✅ BLOQUEO: placa mensual (para impedir check-in desde operario)
        if plate and MonthlyPlate.objects.filter(plate__iexact=plate).exists():
            return JsonResponse({
                "success": True,
                "amount": fmt_cop(0),
                "units": 0,
                "plan": "Mensual",
                "blocked": "monthly",
                # flags consistentes para el front
                "ticket_status": "",
                "has_company": False,
                "company_name": "",
                "company_credit": False,
                "block_invoice": False,
                "work_type_value": "",
                "work_amount_cop": 0,
            })

        # ✅ NUEVO: si viene ticket_id, resolvemos por ID (sin cierre)
        ticket = None
        if ticket_id.isdigit():
            ticket = (
                Ticket.objects.filter(id=int(ticket_id), closure__isnull=True)
                .select_related("vehicle_type", "company", "payment", "work_type_fk")
                .first()
            )
            if not ticket:
                return JsonResponse({"success": False, "message": "Ticket no encontrado"})
            # seguridad: si mandan plate también, debe coincidir
            if plate and (ticket.plate or "").strip().upper().replace(" ", "").replace("-", "") != plate:
                return JsonResponse({"success": False, "message": "Ticket/placa no coinciden"})

        # ✅ Si no hay ticket por ID, usamos lógica antigua por PLACA
        if not ticket:
            # primero ACTIVE
            ticket = (
                Ticket.objects.filter(plate__iexact=plate, status="ACTIVE")
                .select_related("vehicle_type", "company", "payment", "work_type_fk")
                .order_by("-check_in")
                .first()
            )

            # si no hay ACTIVE, intentamos PENDING (sin cierre)
            if not ticket:
                ticket = (
                    Ticket.objects.filter(
                        plate__iexact=plate,
                        status="PENDING",
                        closure__isnull=True
                    )
                    .select_related("vehicle_type", "company", "payment", "work_type_fk")
                    .order_by("-check_in")
                    .first()
                )

                if not ticket:
                    return JsonResponse({"success": False, "message": "Placa no encontrada"})

        # =========================
        # ✅ FLAGS IMPORTANTES PARA EL FRONT
        # =========================
        has_company = bool(getattr(ticket, "company_id", None))
        company_obj = getattr(ticket, "company", None)
        company_name = (getattr(company_obj, "full_name", "") or "").strip() if company_obj else ""
        company_credit = bool(getattr(company_obj, "credit_enabled", False)) if company_obj else False

        ticket_status = (getattr(ticket, "status", "") or "").upper()

        # 🔒 SOLO bloquear factura por cliente si está ASSIGNED (crédito)
        # PENDING sí puede pagarse en operario (y puede tener FE por cliente si así lo decides)
        block_invoice = (ticket_status == "ASSIGNED") or (has_company and company_credit)

        # ✅ servicio para precargar en el front (si existe)
        work_type_value = ""
        try:
            # si usas FK: work_type_fk_id (pk)
            work_type_value = str(getattr(ticket, "work_type_fk_id", "") or "")
        except Exception:
            work_type_value = ""
        work_amount_cop = int(getattr(ticket, "work_amount_cop", 0) or 0)

        # ✅ Si el ticket es PENDING: devolver total congelado
        if ticket_status == "PENDING":
            total = int(getattr(ticket, "total_amount_cop", 0) or 0)
            if total <= 0 and getattr(ticket, "payment", None):
                try:
                    total = int(ticket.payment.amount_cop or 0)
                except Exception:
                    total = 0

            return JsonResponse({
                "success": True,
                "amount": fmt_cop(total),
                "units": 1,
                "plan": "Pendiente",

                # ✅ flags para el front
                "ticket_status": ticket_status,
                "has_company": has_company,
                "company_name": company_name,
                "company_credit": company_credit,
                "block_invoice": block_invoice,

                # ✅ servicio precargable
                "work_type_value": work_type_value,
                "work_amount_cop": work_amount_cop,
            })

        # ✅ ACTIVE: estimación real con tramos
        now = timezone.now()
        estimated_amount = int(estimate_amount_cop(ticket, now=now) or 0)

        night_units = 0
        hour_units = 0
        for seg_start, seg_end, bu, _ in iter_pricing_segments(ticket.check_in, now):
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
            plan_name = "Noche / Fracción"
        else:
            units = max(1, night_units + hour_units)
            if night_units > 0 and hour_units > 0:
                plan_name = "Mixto (Día + Noche)"
            elif night_units > 0:
                plan_name = "Noche / Fracción"
            else:
                plan_name = "Hora / Fracción"

        return JsonResponse({
            "success": True,
            "amount": fmt_cop(estimated_amount),
            "units": units,
            "plan": plan_name,

            # ✅ flags para el front
            "ticket_status": ticket_status,
            "has_company": has_company,
            "company_name": company_name,
            "company_credit": company_credit,
            "block_invoice": block_invoice,

            # ✅ servicio precargable
            "work_type_value": work_type_value,
            "work_amount_cop": work_amount_cop,
        })


    # modos permitidos
    if mode not in ("menu", "checkin", "charge", "inspect", "open", "pending"):
        mode = "menu"

    # tickets activos globales (si mode=open)
    open_tickets = []
    if mode == "open":
        open_tickets = list(
            Ticket.objects.filter(status="ACTIVE")
            .select_related("vehicle_type", "created_by")
            .order_by("-check_in")[:200]
        )
        for t in open_tickets:
            try:
                t.ui_checkin = timezone.localtime(t.check_in).strftime("%Y-%m-%d %H:%M")
            except Exception:
                t.ui_checkin = str(t.check_in)

    amount = None
    inspect_info = None
    checkin_form = TicketCreateForm()
    inspect_form = InspectForm()

    # precargar placa en charge
    # precargar en charge (plate y/o ticket_id)
    plate_qs = (request.GET.get("plate") or "").strip().upper()
    ticket_id_qs = (request.GET.get("ticket_id") or "").strip()

    initial_close = {}

    if ticket_id_qs.isdigit():
        initial_close["ticket_id"] = int(ticket_id_qs)

        # si no viene plate, lo sacamos del ticket para autocompletar
        if not plate_qs:
            t0 = Ticket.objects.filter(id=int(ticket_id_qs)).only("plate").first()
            if t0:
                plate_qs = (t0.plate or "").strip().upper()

    if plate_qs:
        initial_close["plate"] = plate_qs

    close_form = ClosePaymentForm(initial=initial_close) if (mode == "charge" and initial_close) else ClosePaymentForm()


    # CHECKIN
    if request.method == "POST" and mode == "checkin":
        checkin_form = TicketCreateForm(request.POST)
        if checkin_form.is_valid():
            ticket = checkin_form.save(commit=False)
            ticket.created_by = request.user  # se deja para auditoría
            ticket.save()
            Ticket.objects.filter(id=ticket.id).update(service_type="NONE", service_amount_cop=0)

            messages.success(request, f"Ingreso registrado: {ticket.plate}")
            return redirect(f"{reverse('parking:operator_panel')}?mode=menu")

    # CHARGE############
    if request.method == "POST" and mode == "charge":
        close_form = ClosePaymentForm(request.POST)
        if close_form.is_valid():

            plate = (close_form.cleaned_data["plate"] or "").strip().upper().replace(" ", "").replace("-", "")

            # =========================
            # 🔒 BLOQUEO: NO permitir pagar/salir con placas mensuales
            # =========================
            if plate and MonthlyPlate.objects.filter(plate__iexact=plate).exists():
                messages.error(
                    request,
                    "Esa placa es de cliente MENSUAL. No se procesa salida/pago por Operario."
                )
                return redirect(f"{reverse('parking:operator_panel')}?mode=charge")

            method = close_form.cleaned_data["method"]
            transfer_ref = (close_form.cleaned_data.get("transfer_ref") or "").strip()

            # pueden venir del form
            company = close_form.cleaned_data.get("company")
            work_type = (close_form.cleaned_data.get("work_type") or "NONE").strip()
            work_amount_cop = int(close_form.cleaned_data.get("work_amount_cop") or 0)

            invoice_required = (close_form.cleaned_data.get("invoice_required") == "YES")
            id_number = normalize_id_number(close_form.cleaned_data.get("id_number") or "")
            full_name = (close_form.cleaned_data.get("full_name") or "").strip()
            email = (close_form.cleaned_data.get("email") or "").strip()
            customer_obj = close_form.cleaned_data.get("_customer_obj")

            # ✅ si viene ticket_id (desde sidebar "Pagos pendientes"), usar ese ticket exacto
            ticket_id = close_form.cleaned_data.get("ticket_id")
            ticket = None

            if ticket_id:
                ticket = (
                    Ticket.objects.filter(id=ticket_id, closure__isnull=True)
                    .select_related("vehicle_type", "company", "payment")
                    .first()
                )

                if not ticket:
                    messages.error(request, "El ticket seleccionado ya no existe o ya fue cerrado.")
                    return redirect(f"{reverse('parking:operator_panel')}?mode=charge")

                # seguridad extra: placa del ticket debe coincidir con la digitada
                ticket_plate_cmp = (ticket.plate or "").strip().upper().replace(" ", "").replace("-", "")
                if ticket_plate_cmp != plate:
                    messages.error(request, "La placa no coincide con el ticket seleccionado.")
                    return redirect(f"{reverse('parking:operator_panel')}?mode=charge")

            else:
                # ✅ Desde menú Pagos: SOLO ACTIVE por placa
                ticket = (
                    Ticket.objects.filter(plate__iexact=plate, status="ACTIVE")
                    .select_related("vehicle_type", "company", "payment")
                    .order_by("-check_in")
                    .first()
                )

            # ✅ AJUSTE CRÍTICO: si no hay ticket, DEBES retornar (antes no lo hacías)
            if not ticket:
                messages.error(
                    request,
                    "En Pagos (menú) solo se procesa un ticket ACTIVO. "
                    "Los pendientes se pagan desde la pestaña Pagos pendientes."
                )
                return redirect(f"{reverse('parking:operator_panel')}?mode=charge")

            # =========================
            # 🔒 DOBLE BLOQUEO: si el ticket tiene placa mensual, no permitir (aunque venga por ID)
            # =========================
            ticket_plate_norm = (ticket.plate or "").strip().upper().replace(" ", "").replace("-", "")
            if ticket_plate_norm and MonthlyPlate.objects.filter(plate__iexact=ticket_plate_norm).exists():
                messages.error(
                    request,
                    "Este ticket corresponde a una placa MENSUAL. No debe pagarse aquí."
                )
                return redirect(f"{reverse('parking:operator_panel')}?mode=charge")

            # =========================================================
            # ✅ AJUSTE CLAVE (TU PROBLEMA):
            # Si el ticket YA está asociado a una empresa:
            # - si la empresa tiene crédito => NO se paga aquí (se liquida en company_settle)
            # - si NO tiene crédito => se puede pagar en caja, PERO NO con factura electrónica por cliente
            # =========================================================
            # =========================================================
            # ✅ REGLA CORRECTA:
            # - ASSIGNED (crédito) => NO se paga aquí
            # - PENDING => SÍ se paga aquí (y puede tener FE por cliente)
            # =========================================================
            # =========================================================
            # REGLA CORRECTA:
            # SOLO bloquear si el estado es ASSIGNED
            # =========================================================
            if ticket.status == "ASSIGNED":
                comp = getattr(ticket, "company", None)
                comp_name = getattr(comp, "full_name", "Empresa")

                messages.error(
                    request,
                    f"Este ticket está asignado a la empresa '{comp_name}' (crédito). "
                    "Debe liquidarse por el módulo de Empresas, no por Operario."
                )
                return redirect(f"{reverse('parking:operator_panel')}?mode=charge")


            # ✅ IMPORTANTE:
            # Si es PENDING, NO tocar invoice_required ni limpiar customer.
            # (si el form trae factura electrónica, se permite)


            # -------------------------
            # CASO PENDING: SOLO CASH/TRANSFER
            # -------------------------
            if ticket.status == "PENDING":
                if method not in ("CASH", "TRANSFER"):
                    messages.error(request, "Un ticket PENDIENTE solo puede pagarse con EFECTIVO o TRANSFERENCIA.")
                    return redirect(f"{reverse('parking:operator_panel')}?mode=charge")

                customer = None

                # ✅ Solo resolver/crear customer si realmente se permite factura electrónica
                if method in ("CASH", "TRANSFER") and invoice_required:
                    customer, err = resolve_or_create_customer(
                        id_number=id_number,
                        full_name=full_name,
                        email=email,
                        customer_obj=customer_obj,
                    )
                    if err:
                        messages.error(request, err)
                        if "NIT" in err or "cédula" in err:
                            close_form.add_error("id_number", err)
                        elif "nombre" in err or "razón" in err:
                            close_form.add_error("full_name", err)
                        elif "correo" in err:
                            close_form.add_error("email", err)
                        return redirect(f"{reverse('parking:operator_panel')}?mode=charge")

                now = timezone.now()
                if not ticket.check_out:
                    ticket.check_out = now
                ticket.closed_by = request.user
                ticket.status = "PAID"

                # ✅ Mantén consistencia: en PENDING caja, ticket deja de estar asociado a empresa
                ticket.company = None

                ticket.save(update_fields=["check_out", "closed_by", "status", "company"])
                # --- separar parqueadero vs servicio ---
                total = int(getattr(ticket, "total_amount_cop", 0) or 0)
                work  = int(getattr(ticket, "work_amount_cop", 0) or 0)
                parking = max(0, total - work)


                payment_obj, _ = Payment.objects.update_or_create(
                    ticket=ticket,
                    defaults={
                        "method": method,
                        "status": "PAID",
                        "transfer_ref": transfer_ref if method == "TRANSFER" else "",
                        "company": None,
                        "amount_cop": int(ticket.total_amount_cop or 0),
                        "invoice_required": invoice_required,
                        "customer": customer if invoice_required else None,
                    }
                )

                # ✅ PREVIEW FACTURA ELECTRÓNICA (solo si invoice_required=True y customer existe)
                # (solo si invoice_required=True y customer existe)
                # ✅ FACTURA ELECTRÓNICA (solo si invoice_required=True y customer existe)
                if invoice_required and customer:
                    total = int(getattr(ticket, "total_amount_cop", 0) or 0)
                    work  = int(getattr(ticket, "work_amount_cop", 0) or 0)
                    parking = max(0, total - work)

                    emit_electronic_invoice_preview(
                        id_number=(getattr(customer, "id_number", "") or "").strip(),
                        full_name=(getattr(customer, "full_name", "") or "").strip(),
                        email=(getattr(customer, "email", "") or "").strip(),
                        items=[{
                            "plate": ticket.plate,
                            "parking": parking,
                            "work": work,
                            "work_type": ticket.work_type,  # property: code o NONE
                        }],
                    )
                return redirect("parking:print_receipt", payment_id=payment_obj.id)

            # -------------------------
            # CASO ACTIVE
            # -------------------------
            elif ticket.status == "ACTIVE":
                now = timezone.now()

                customer = None
                if method in ("CASH", "TRANSFER") and invoice_required:
                    customer, err = resolve_or_create_customer(
                        id_number=id_number,
                        full_name=full_name,
                        email=email,
                        customer_obj=customer_obj,
                    )
                    if err:
                        messages.error(request, err)
                        if "NIT" in err or "cédula" in err:
                            close_form.add_error("id_number", err)
                        elif "nombre" in err or "razón" in err:
                            close_form.add_error("full_name", err)
                        elif "correo" in err:
                            close_form.add_error("email", err)
                        return redirect(f"{reverse('parking:operator_panel')}?mode=charge")

                ticket.check_out = now
                ticket.closed_by = request.user

                work_type_u = (work_type or "").upper()
                if work_type_u == "NONE":
                    work_type = "NONE"
                    work_amount_cop = 0

                ticket.work_type = work_type
                ticket.work_amount_cop = work_amount_cop
                ticket.service_type = "NONE"
                ticket.service_amount_cop = 0

                amount = int(estimate_amount_cop(ticket, now=now) or 0)
                ticket.total_amount_cop = amount

                if method in ("CASH", "TRANSFER"):
                    ticket.status = "PAID"
                    ticket.company = None
                    ticket.save()
                    # --- separar parqueadero vs servicio ---
                    total = int(getattr(ticket, "total_amount_cop", 0) or 0)
                    work  = int(getattr(ticket, "work_amount_cop", 0) or 0)
                    parking = max(0, total - work)

                    payment_obj, _ = Payment.objects.update_or_create(
                        ticket=ticket,
                        defaults={
                            "method": method,
                            "status": "PAID",
                            "transfer_ref": transfer_ref if method == "TRANSFER" else "",
                            "company": None,
                            "amount_cop": ticket.total_amount_cop,
                            "invoice_required": invoice_required,
                            "customer": customer if invoice_required else None,
                        }
                    )

                    # ✅ FACTURA ELECTRÓNICA
                    if invoice_required and customer:
                        emit_electronic_invoice_preview(
                            id_number=(getattr(customer, "id_number", "") or "").strip(),
                            full_name=(getattr(customer, "full_name", "") or "").strip(),
                            email=(getattr(customer, "email", "") or "").strip(),
                            items=[{
                                "plate": ticket.plate,
                                "parking": parking,
                                "work": work,
                                "work_type": ticket.work_type,  # property => code / NONE
                            }],
                        )

                    return redirect("parking:print_receipt", payment_id=payment_obj.id)

                else:
                    # empresa con crédito => ASSIGNED; empresa sin crédito => PENDING
                    ticket.company = company
                    ticket.status = "ASSIGNED" if getattr(company, "credit_enabled", False) else "PENDING"
                    ticket.save()

                    payment_obj, _ = Payment.objects.update_or_create(
                        ticket=ticket,
                        defaults={
                            "method": "CREDIT",
                            "status": "PENDING",
                            "company": company,
                            "amount_cop": ticket.total_amount_cop,
                            "invoice_required": False,
                            "customer": None,
                            "transfer_ref": "",
                        }
                    )
                    return redirect("parking:print_receipt", payment_id=payment_obj.id)
        else:
            print("FORM ERRORS:", close_form.errors)


    # INSPECT
    if request.method == "POST" and mode == "inspect":
        inspect_form = InspectForm(request.POST)
        if inspect_form.is_valid():
            plate = inspect_form.cleaned_data["plate"].strip().upper()

            # ✅ AJUSTE: sin created_by
            ticket = Ticket.objects.filter(
                plate__iexact=plate, status="ACTIVE"
            ).select_related("vehicle_type").order_by("-check_in").first()

            if not ticket:
                messages.error(request, "No existe un ticket activo.")
            else:
                now = timezone.now()
                delta = now - ticket.check_in
                total_sec = int(delta.total_seconds())
                minutes = max(0, total_sec // 60)
                hours = minutes // 60
                rem = minutes % 60
                elapsed_text = f"{hours}h {rem}m"

                raw_cost = int(estimate_amount_cop(ticket, now=now) or 0)
                cost_fmt = fmt_cop(raw_cost) if raw_cost >= 0 else "---"

                inspect_info = {
                    "plate": ticket.plate,
                    "vehicle_type": ticket.vehicle_type.name,
                    "client_type": client_label(ticket),
                    "check_in": ticket.check_in,
                    "elapsed_text": elapsed_text,
                    "current_cost": cost_fmt
                }

    # SIDEBAR
    # ✅ AJUSTE: global (sin created_by)
    last_transactions_candidates = list(
        Ticket.objects.filter(closure__isnull=True)
        .select_related("vehicle_type")
        .annotate(last_dt=Coalesce("check_out", "check_in"))
        .order_by("-last_dt")[:10]
    )

    # 2) payments para esos candidatos (botón recibo)
    pay_map = {
        p.ticket_id: p
        for p in Payment.objects.filter(ticket_id__in=[t.id for t in last_transactions_candidates])
    }

    # 3) Enriquecer candidatos (UI)
    for t in last_transactions_candidates:
        flag = ticket_has_service(t)
        t._has_service_ui = flag
        t.has_service_ui = flag

        total = int(getattr(t, "total_amount_cop", 0) or 0)
        if total <= 0 and t.id in pay_map:
            try:
                total = int(pay_map[t.id].amount_cop or 0)
            except Exception:
                total = 0

        s_txt, s_amt = ticket_service_display(t)
        parking_amt = max(total - int(s_amt or 0), 0)

        t.total_amount_ui = fmt_cop(total)
        t.parking_amount_ui = fmt_cop(parking_amt)
        t.service_text_ui = s_txt
        t.service_amount_ui = fmt_cop(s_amt)

        p = pay_map.get(t.id)
        t.payment_id_ui = p.id if p else None

    # 4) Último cierre
    last_closure = (
        Closure.objects
        .order_by("-date")
        .only("id", "date")
        .first()
    )

    # 5) Lista única “transaccional” (tickets + cierre)
    sidebar_last_items = []

    for t in last_transactions_candidates:
        dt = getattr(t, "last_dt", None) or getattr(t, "check_out", None) or getattr(t, "check_in", None)
        sidebar_last_items.append({
            "kind": "ticket",
            "dt": dt,
            "ticket": t,
        })

    if last_closure and getattr(last_closure, "date", None):
        sidebar_last_items.append({
            "kind": "closure",
            "dt": last_closure.date,
            "closure": last_closure,
        })

    # 6) Ordenar por fecha DESC y recortar a 3
    sidebar_last_items.sort(key=lambda x: x["dt"] or timezone.now(), reverse=True)
    sidebar_last_items = sidebar_last_items[:3]

    # 7) Compatibilidad: last_transactions = solo los tickets que quedaron en el top 3
    last_transactions = [it["ticket"] for it in sidebar_last_items if it.get("kind") == "ticket"]


    # pendientes (solo PENDING, sin cierre) ✅ global
    pending_tickets = list(
        Ticket.objects.filter(
            closure__isnull=True,
            status="PENDING",
        )
        .select_related("vehicle_type", "company")
        .annotate(last_dt=Coalesce("check_out", "check_in"))
        .order_by("-last_dt")[:500]
    )

    # últimos pagados ✅ global
    paid_tickets = list(Ticket.objects.filter(status="PAID").order_by("-check_out")[:30])
    paid_payments_by_ticket_id = {
        p.ticket_id: p
        for p in Payment.objects.filter(ticket_id__in=[t.id for t in paid_tickets])
    }

    # ✅ habilitar cierre SOLO si hay tickets realmente cerrables: PAID o ASSIGNED (sin cierre) ✅ global
    can_close = Ticket.objects.filter(
        closure__isnull=True,
        status__in=["PAID", "ASSIGNED"],
    ).exists()

    closable_count = Ticket.objects.filter(
        closure__isnull=True,
        status__in=["PAID", "ASSIGNED"],
    ).count()

    pending_count = Ticket.objects.filter(
        closure__isnull=True,
        status="PENDING",
    ).count()

    active_count = Ticket.objects.filter(
        status="ACTIVE",
    ).count()

    return render(request, "parking/operator_panel.html", {
        "mode": mode,
        "checkin_form": checkin_form,
        "close_form": close_form,
        "inspect_form": inspect_form,
        "amount": amount,
        "inspect_info": inspect_info,

        # ✅ nuevo: lista mezclada que sí “cuenta” el cierre dentro de las 3
        "sidebar_last_items": sidebar_last_items,

        # ✅ se mantiene por compatibilidad (pero ya NO es el “top 3 real mezclado”)
        "last_transactions": last_transactions,

        "pending_tickets": pending_tickets,
        "paid_tickets": paid_tickets,
        "paid_payments_by_ticket_id": paid_payments_by_ticket_id,

        "can_close": can_close,
        "closable_count": closable_count,
        "pending_count": pending_count,
        "active_count": active_count,

        # ✅ se mantiene, por si lo usas en otro lado
        "last_closure": last_closure,

        "open_tickets": open_tickets,
        "is_admin": is_admin,
    })





# =========================
# Generar cierre (operario)
# =========================

@login_required
def generate_closure(request):
    if request.method != "POST":
        return redirect("parking:operator_panel")

    # Tickets aún no cerrados y NO activos
    base_qs = (
        Ticket.objects
        .filter(closure__isnull=True)
        .exclude(status="ACTIVE")
        .select_related("payment")
    )

    # Solo estos habilitan el cierre
    closable_qs = base_qs.exclude(status="PENDING")  # PAID + ASSIGNED
    pending_qs = base_qs.filter(status="PENDING")    # pendientes aparte (solo informativo)

    closable_list = list(closable_qs)
    pending_list = list(pending_qs)

    # ✅ Regla principal: si NO hay PAID/ASSIGNED, NO se puede cerrar
    if not closable_list:
        active_count = Ticket.objects.filter(status="ACTIVE").count()
        pending_count = len(pending_list)

        if pending_count > 0 or active_count > 0:
            messages.warning(
                request,
                "No se puede generar cierre: solo hay tickets Activos o Pendientes. "
                "Primero debe existir al menos un ticket Pagado (PAID) o Asignado (ASSIGNED)."
            )
        else:
            messages.warning(request, "No hay transacciones nuevas.")
        return redirect("parking:operator_panel")

    # Si llegamos aquí, hay algo cerrable
    tickets_list = closable_list + pending_list  # pendientes se muestran, pero NO entran al cierre
    active_tickets_count, monthly_count, active_count = _yard_counts()


    now = timezone.now()
    last_closure = Closure.objects.order_by("-date").first()
    if last_closure:
        period_start = last_closure.date
    else:
        first_ticket = Ticket.objects.order_by("check_in").first()
        period_start = first_ticket.check_in if first_ticket else now
    period_end = now

    total_parking = 0
    total_service = 0
    total_general = 0
    total_cash = 0
    total_transfer = 0
    total_credit = 0
    total_pending = 0

    # PAID + ASSIGNED (entran al cierre)
    for t in closable_list:
        amt = int(getattr(t, "total_amount_cop", 0) or 0)
        svc = int(getattr(t, "work_amount_cop", 0) or 0) + int(getattr(t, "service_amount_cop", 0) or 0)
        pkg = max(0, amt - svc)

        if t.status == "ASSIGNED":
            method = "CREDIT"
        else:
            method = getattr(getattr(t, "payment", None), "method", None) or "CASH"

        total_parking += pkg
        total_service += svc
        total_general += amt

        if method == "CASH":
            total_cash += amt
        elif method == "TRANSFER":
            total_transfer += amt
        elif method == "CREDIT":
            total_credit += amt

        t.print_parking = fmt_cop(pkg)
        t.print_service = fmt_cop(svc)
        t.print_payment_method = method

    # PENDING (NO entra al cierre, solo informativo)
    for t in pending_list:
        amt = int(getattr(t, "total_amount_cop", 0) or 0)
        svc = int(getattr(t, "work_amount_cop", 0) or 0) + int(getattr(t, "service_amount_cop", 0) or 0)
        pkg = max(0, amt - svc)

        total_pending += amt

        t.print_parking = fmt_cop(pkg)
        t.print_service = fmt_cop(svc)
        t.print_payment_method = "PENDING"

    # ✅ Crear cierre SOLO si hubo cerrables (ya garantizado arriba)
    closure = Closure.objects.create(
        total_amount=total_general,
        period_start=period_start,
        period_end=period_end,
        date=period_end
    )
    closable_qs.update(closure=closure)

    context = {
        "closure": closure,
        "tickets": tickets_list,
        "active_tickets_count": active_tickets_count,
        "monthly_count": monthly_count,
        "active_count": active_count,
        "total_parking": fmt_cop(total_parking),
        "total_service": fmt_cop(total_service),
        "total_general": fmt_cop(total_general),
        "total_cash": fmt_cop(total_cash),
        "total_transfer": fmt_cop(total_transfer),
        "total_credit": fmt_cop(total_credit),
        "total_pending": fmt_cop(total_pending),
        "finish_url": reverse("parking:operator_panel"),
    }
    return render(request, "parking/report_print.html", context)

# =========================
# Gestión (admin)
# =========================

@staff_member_required(login_url="parking:login")
def create_customer(request):
    if request.method == "POST":
        post = request.POST.copy()

        # ✅ Normaliza NIT/CC (quita puntos/guiones/espacios)
        post["id_number"] = normalize_id_number(post.get("id_number") or "")

        # ✅ Si ya existe, lo actualizamos (no intentamos crear)
        existing = Customer.objects.filter(id_number=post["id_number"]).first()

        form = CustomerForm(post, instance=existing)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                "Cliente actualizado correctamente." if existing else "Cliente registrado correctamente."
            )
            return redirect("parking:gestion")

        messages.error(request, "Error al guardar. Verifique los datos.")
    else:
        form = CustomerForm()

    return render(request, "parking/customer_form.html", {"form": form})

@staff_member_required(login_url="parking:login")
def gestion(request):
    mode = request.GET.get("mode", "menu")

    # =========================
    # Helpers (locales, no rompen nada)
    # =========================
    def _to_int(v, default=0):
        try:
            if v is None:
                return default
            s = str(v).strip()
            if not s:
                return default
            # acepta "1.234" o "1,234" o "1234"
            s = "".join(ch for ch in s if ch.isdigit())
            return int(s) if s else default
        except Exception:
            return default

    def _safe_json_list(v):
        """
        Espera algo como:
        '[{"id": 12}, {"id": 15}]'
        y devuelve lista de dicts o [].
        """
        try:
            if not v:
                return []
            data = json.loads(v)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _valid_email(s: str) -> bool:
        try:
            s = (s or "").strip()
            if not s:
                return False
            # validación simple, consistente con tu frontend
            return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", s))
        except Exception:
            return False

    # =========================
    # Lookup Customer (AJAX)
    # =========================
    if mode == "lookup_customer":
        by = (request.GET.get("by") or "nit").strip().lower()   # nit | name
        q  = (request.GET.get("q")  or "").strip()

        # fallback: compatibilidad con tu front actual (que manda ?id=...)
        if not q:
            q = (request.GET.get("id") or "").strip()
            by = "nit"

        if not q:
            return JsonResponse({"found": False, "results": []})

        # -------------------------
        # 1) Buscar por NIT/CC exacto
        # -------------------------
        if by in ("nit", "id", "id_number"):
            q_raw = q
            q_norm = normalize_id_number(q)
            customer = (
                Customer.objects.filter(id_number=q_raw).first()
                or Customer.objects.filter(id_number=q_norm).first()
            )

            if customer:
                return JsonResponse({
                    "found": True,
                    "mode": "single",
                    "full_name": customer.full_name,
                    "email": customer.email,
                    "id_number": customer.id_number,
                    "is_company": customer.is_company,
                    "credit_enabled": getattr(customer, "credit_enabled", False),
                })
            return JsonResponse({"found": False, "mode": "single"})

        # -------------------------
        # 2) Buscar por Nombre (sugerencias)
        # -------------------------
        # Nota: usamos icontains para que funcione con fragmentos.
        # Limitamos resultados para no saturar.
        qs = (
            Customer.objects.filter(full_name__icontains=q, active=True)
            .order_by("full_name")[:10]
        )

        results = [{
            "id_number": c.id_number,
            "full_name": c.full_name,
            "email": c.email,
            "is_company": c.is_company,
            "credit_enabled": getattr(c, "credit_enabled", False),
        } for c in qs]

        return JsonResponse({
            "found": len(results) > 0,
            "mode": "list",
            "results": results
        })


    # =========================
    # ✅ NUEVO: CLIENTES MENSUALES (ADMIN)
    # =========================

    if mode == "monthly":
        # =========================
        # ✅ LISTADO PLACAS MENSUALES
        # =========================
        qs = MonthlyPlate.objects.all().order_by("-id")[:500]

        monthly_rows = []
        for r in qs:
            # fecha segura (si existe)
            if hasattr(r, "created_at") and r.created_at:
                try:
                    ui_created = timezone.localtime(r.created_at).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    ui_created = str(r.created_at)
            else:
                ui_created = "—"

            # inyectamos atributo solo para UI
            r.ui_created = ui_created
            monthly_rows.append(r)

        return render(request, "parking/gestion.html", {
            "mode": mode,
            "monthly_rows": monthly_rows,   # 👈 este es el que debe usar el template
            "monthly_count": len(monthly_rows),
            "is_admin": True,
        })

    # =========================
    # OPEN tickets (ADMIN view)
    # =========================
    if mode == "open":
        open_tickets = list(
            Ticket.objects.filter(status="ACTIVE")
            .select_related("vehicle_type", "created_by")
            .order_by("-check_in")[:200]
        )
        for t in open_tickets:
            try:
                t.ui_checkin = timezone.localtime(t.check_in).strftime("%Y-%m-%d %H:%M")
            except Exception:
                t.ui_checkin = str(t.check_in)

        return render(request, "parking/gestion.html", {
            "mode": mode,
            "open_tickets": open_tickets,
            "is_admin": True,
        })

    # =========================
    # USERS (OPERARIOS)
    # =========================
    if mode == "users":
        u_id = request.GET.get("id")
        instance = User.objects.filter(
            id=u_id,
            is_staff=False,
            is_superuser=False
        ).first() if u_id else None

        if request.method == "POST" and request.POST.get("action") == "save_user":
            form = OperarioForm(request.POST, instance=instance)
            if form.is_valid():
                if instance is None:
                    username = form.cleaned_data["username"]
                    existing = User.objects.filter(
                        username__iexact=username,
                        is_staff=False,
                        is_superuser=False
                    ).first()
                    if existing:
                        existing.first_name = form.cleaned_data["first_name"]
                        existing.last_name = form.cleaned_data["last_name"]
                        existing.is_active = form.cleaned_data["is_active"]
                        pwd = form.cleaned_data.get("password1")
                        if pwd:
                            existing.set_password(pwd)
                        existing.save()
                        messages.success(request, f"Operario '{existing.username}' actualizado automáticamente.")
                        return redirect(f"{reverse('parking:gestion')}?mode=users")

                obj = form.save()
                verb = "actualizado" if instance else "creado"
                messages.success(request, f"Operario '{obj.username}' {verb} correctamente.")
                return redirect(f"{reverse('parking:gestion')}?mode=users")

            messages.error(request, "No se pudo guardar el operario.")
        else:
            form = OperarioForm(instance=instance)

        if request.method == "POST" and request.POST.get("action") == "toggle_user":
            u = get_object_or_404(
                User,
                id=request.POST.get("user_id"),
                is_staff=False,
                is_superuser=False
            )
            u.is_active = not u.is_active
            u.save(update_fields=["is_active"])
            messages.success(request, f"Operario '{u.username}' {'activado' if u.is_active else 'desactivado'}.")
            return redirect(f"{reverse('parking:gestion')}?mode=users")

        password_form = OperarioPasswordForm()
        if request.method == "POST" and request.POST.get("action") == "set_password":
            u = get_object_or_404(
                User,
                id=request.POST.get("user_id"),
                is_staff=False,
                is_superuser=False
            )
            password_form = OperarioPasswordForm(request.POST)
            if password_form.is_valid():
                u.set_password(password_form.cleaned_data["password1"])
                u.save(update_fields=["password"])
                messages.success(request, f"Contraseña actualizada para '{u.username}'.")
                return redirect(f"{reverse('parking:gestion')}?mode=users")
            messages.error(request, "No se pudo cambiar la contraseña.")

        users = User.objects.filter(is_staff=False, is_superuser=False).order_by("username")

        return render(request, "parking/gestion.html", {
            "mode": mode,
            "form": form,
            "users": users,
            "editing_user": instance,
            "password_form": password_form,
        })

    # =========================
    # VEHICLE TYPES
    # =========================
    if mode == "vehicletypes":
        vt_id = request.GET.get("id")
        instance = VehicleType.objects.filter(id=vt_id).first() if vt_id else None

        if request.method == "POST" and request.POST.get("action") == "save_vehicle_type":
            form = VehicleTypeForm(request.POST, instance=instance)

            if instance:
                if form.is_valid():
                    obj = form.save()
                    messages.success(request, f"Tipo de vehículo actualizado: {obj.name}")
                    return redirect(f"{reverse('parking:gestion')}?mode=vehicletypes")
                messages.error(request, "No se pudo guardar. Verifica el nombre.")
            else:
                name = (request.POST.get("name") or "").strip()
                active = bool(request.POST.get("active"))

                if not name:
                    messages.error(request, "El nombre es obligatorio.")
                else:
                    existing = VehicleType.objects.filter(name__iexact=name).first()
                    if existing:
                        existing.name = name
                        existing.active = active
                        existing.save(update_fields=["name", "active"])
                        messages.success(request, f"'{name}' ya existía. Se actualizó automáticamente.")
                        return redirect(f"{reverse('parking:gestion')}?mode=vehicletypes")

                    if form.is_valid():
                        obj = form.save()
                        messages.success(request, f"Tipo de vehículo creado: {obj.name}")
                        return redirect(f"{reverse('parking:gestion')}?mode=vehicletypes")

                    messages.error(request, "No se pudo guardar. Verifica el nombre.")
        else:
            form = VehicleTypeForm(instance=instance)

        vehicle_types = VehicleType.objects.order_by("name")

        return render(request, "parking/gestion.html", {
            "mode": mode,
            "form": form,
            "vehicle_types": vehicle_types,
            "editing_vehicle_type": instance,
        })

    # =========================
    # RATEPLANS (ADMIN) - sin cambios funcionales
    # =========================
    if mode == "rateplans":
        rp_id = request.GET.get("id")
        instance = RatePlan.objects.filter(id=rp_id).first() if rp_id else None

        if request.method == "POST" and request.POST.get("action") == "save_rateplan":
            vt_id = (request.POST.get("vehicle_type") or "").strip()
            ck = (request.POST.get("client_kind") or "").strip()
            bu = (request.POST.get("billing_unit") or "").strip()

            dt = (request.POST.get("day_type") or "").strip() if hasattr(RatePlan, "day_type") else ""
            if hasattr(RatePlan, "day_type") and not dt:
                dt = "NORMAL"

            if not ck:
                form = RatePlanForm(request.POST, instance=instance)
                messages.error(request, "Selecciona un tipo de cliente.")
            else:
                if instance is None and vt_id and ck and bu:
                    lookup = dict(
                        vehicle_type_id=vt_id,
                        client_kind=ck,
                        billing_unit=bu,
                    )
                    if hasattr(RatePlan, "day_type"):
                        lookup["day_type"] = dt

                    existing = RatePlan.objects.filter(**lookup).first()

                    if existing:
                        form = RatePlanForm(request.POST, instance=existing)
                        if form.is_valid():
                            obj = form.save()
                            messages.success(request, f"Tarifa actualizada: {obj.label} - {fmt_cop(obj.price_cop)} COP")
                            return redirect(f"{reverse('parking:gestion')}?mode=rateplans")
                    else:
                        form = RatePlanForm(request.POST)
                        if form.is_valid():
                            obj = form.save()
                            messages.success(request, f"Tarifa creada: {obj.label} - {fmt_cop(obj.price_cop)} COP")
                            return redirect(f"{reverse('parking:gestion')}?mode=rateplans")
                else:
                    form = RatePlanForm(request.POST, instance=instance)
                    if form.is_valid():
                        obj = form.save()
                        messages.success(request, f"Tarifa actualizada: {obj.label} - {fmt_cop(obj.price_cop)} COP")
                        return redirect(f"{reverse('parking:gestion')}?mode=rateplans")
        else:
            form = RatePlanForm(instance=instance)

        qs = RatePlan.objects.select_related("vehicle_type").order_by(
            "vehicle_type__name", "client_kind", "billing_unit"
        )
        if hasattr(RatePlan, "day_type"):
            qs = qs.order_by("vehicle_type__name", "client_kind", "billing_unit", "day_type")

        rateplans = qs

        return render(request, "parking/gestion.html", {
            "mode": mode,
            "form": form,
            "rateplans": rateplans,
            "editing_rateplan": instance,
        })

    # =========================
    # ✅ Company settle lookup (AJAX) - AJUSTADO
    # =========================
    if mode == "company_settle_lookup" and request.headers.get("x-requested-with") == "XMLHttpRequest":
        company_id = (request.GET.get("company_id") or "").strip()
        try:
            company_id_int = int(company_id)
        except Exception:
            return JsonResponse({"success": False, "message": "Empresa inválida."})

        company = Customer.objects.filter(
            id=company_id_int, active=True, is_company=True, credit_enabled=True
        ).first()
        if not company:
            return JsonResponse({"success": False, "message": "Empresa no válida o no habilitada para crédito."})

        qs = (
            Ticket.objects.filter(company=company, status="ASSIGNED")
            .select_related("vehicle_type")
            .order_by("-check_in")[:500]
        )

        tickets = []
        sum_total = 0
        sum_service = 0
        sum_parking = 0

        for t in qs:
            total = int(getattr(t, "total_amount_cop", 0) or 0)
            svc = int(getattr(t, "service_amount_cop", 0) or 0)
            wrk = int(getattr(t, "work_amount_cop", 0) or 0)

            # ✅ "Servicio" efectivo (como te venía funcionando): service + work
            service_effective = max(0, svc + wrk)

            sum_total += total
            sum_service += service_effective
            sum_parking += max(0, total - service_effective)

            try:
                ci = timezone.localtime(t.check_in).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ci = str(t.check_in)

            if getattr(t, "check_out", None):
                try:
                    co = timezone.localtime(t.check_out).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    co = str(t.check_out)
            else:
                co = ""

            tickets.append({
                "id": t.id,
                "plate": (t.plate or ""),
                "check_in": ci,
                "check_out": co,
                "total": total,
            })

        return JsonResponse({
            "success": True,
            "tickets": tickets,
            "sum_total": sum_total,
            "sum_service": sum_service,   # ✅ vuelve a traer el "servicio" real (incluye work)
            "sum_parking": sum_parking,   # ✅ parqueadero real (total - servicio_efectivo)
        })
    if mode == "company_settle":
        form = CompanySettleForm(request.POST or None)

        if request.method == "POST":
            if form.is_valid():
                company = form.cleaned_data["company"]
                method = (form.cleaned_data.get("method") or "").strip()
                transfer_ref = (form.cleaned_data.get("transfer_ref") or "").strip()

                invoice_required = (form.cleaned_data.get("invoice_required") or "").strip()  # YES/NO
                id_number = (form.cleaned_data.get("id_number") or "").strip()
                full_name = (form.cleaned_data.get("full_name") or "").strip()
                email = (form.cleaned_data.get("email") or "").strip()
                customer_obj = form.cleaned_data.get("_customer_obj")

                # montos (parking readonly, service editable)
                amount_parking_cop = _to_int(request.POST.get("amount_parking_cop"), 0)
                amount_service_cop = _to_int(request.POST.get("amount_service_cop"), 0)

                # tickets desde frontend (opcional, recomendado)
                payload_list = _safe_json_list(request.POST.get("tickets_payload"))
                payload_ids = [int(x.get("id")) for x in payload_list if isinstance(x, dict) and str(x.get("id", "")).isdigit()]
                payload_ids = list(dict.fromkeys(payload_ids))  # unique preserve order

                if not getattr(company, "credit_enabled", False):
                    messages.error(request, "Esta empresa no está habilitada para crédito.")
                    return redirect(f"{reverse('parking:gestion')}?mode=company_settle")

                # Base queryset: lo elegible en backend
                base_qs = Ticket.objects.filter(company=company, status="ASSIGNED")

                # Si viene payload_ids, procesamos SOLO esos (evita desalineaciones UI/BD)
                if payload_ids:
                    tickets_qs = base_qs.filter(id__in=payload_ids).select_related("payment").order_by("check_in")
                else:
                    tickets_qs = base_qs.select_related("payment").order_by("check_in")

                tickets_list = list(tickets_qs)

                if not tickets_list:
                    messages.warning(request, "Esta empresa no tiene tickets acumulados por cobrar.")
                    return redirect(f"{reverse('parking:gestion')}?mode=company_settle")

                # =========================================================
                # ✅ FACTURA ELECTRÓNICA (EMPRESA)
                # - Si invoice_required == YES => NO pedir datos al usuario
                # - Se factura con los datos guardados en la empresa (company)
                # - Asociamos customer = company (porque company es Customer)
                # =========================================================
                customer = None

                if invoice_required == "YES":
                    # 🔒 Fuerza datos desde la empresa (NO desde POST)
                    id_number = (getattr(company, "id_number", "") or "").strip()
                    full_name = (getattr(company, "full_name", "") or "").strip()
                    email     = (getattr(company, "email", "") or "").strip()

                    if not id_number:
                        messages.error(request, "La empresa no tiene NIT guardado. Actualiza sus datos en Clientes.")
                        return redirect(f"{reverse('parking:gestion')}?mode=company_settle")

                    customer = company
                else:
                    id_number = ""
                    full_name = ""
                    email = ""
                    customer = None

                now = timezone.now()

                # ✅ armar recibo consolidado (para sesión)
                receipt_tickets = []
                sum_total_db = 0

                # ✅ Transacción para consistencia
                with transaction.atomic():
                    for t in tickets_list:
                        if not t.check_out:
                            t.check_out = now
                        t.closed_by = request.user
                        t.status = "PAID"
                        t.save(update_fields=["check_out", "closed_by", "status"])

                        # total del ticket (BD)
                        t_total = int(getattr(t, "total_amount_cop", 0) or 0)
                        sum_total_db += t_total

                        # ✅ Guardamos/actualizamos Payment por ticket (como ya lo haces)
                        Payment.objects.update_or_create(
                            ticket=t,
                            defaults={
                                "method": method,
                                "status": "PAID",
                                "transfer_ref": transfer_ref if method == "TRANSFER" else "",
                                "company": None,
                                "amount_cop": t_total,
                                "invoice_required": (invoice_required == "YES"),
                                "customer": customer if (invoice_required == "YES") else None,
                            }
                        )

                        # ✅ Para el recibo: datos por ticket (ya pagado)
                        try:
                            ci = timezone.localtime(t.check_in).strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            ci = str(t.check_in)
                        try:
                            co = timezone.localtime(t.check_out).strftime("%Y-%m-%d %H:%M") if t.check_out else ""
                        except Exception:
                            co = str(t.check_out) if t.check_out else ""

                        t_total = int(getattr(t, "total_amount_cop", 0) or 0)
                        t_work  = int(getattr(t, "work_amount_cop", 0) or 0)
                        t_park  = max(0, t_total - t_work)

                        receipt_tickets.append({
                            "id": t.id,
                            "plate": (t.plate or ""),
                            "check_in": ci,
                            "check_out": co,
                            "total": int(t_total),

                            "parking_amount": int(t_park),
                            "work_amount": int(t_work),
                            "work_type": (getattr(t, "work_type", "NONE") or "NONE").strip().upper(),
                        })

                # ✅ Guardar recibo en SESSION (sin modelo nuevo)
                receipt_data = {
                    "receipt": {
                        "created_at": timezone.localtime(now).strftime("%Y-%m-%d %H:%M"),
                        "company": {
                            "id": company.id,
                            "full_name": (getattr(company, "full_name", "") or "").strip(),
                            "id_number": (getattr(company, "id_number", "") or "").strip(),
                        },
                        "method": method,
                        "transfer_ref": transfer_ref if method == "TRANSFER" else "",
                        "invoice_required": (invoice_required == "YES"),

                        "invoice": {
                            "id_number": (getattr(company, "id_number", "") or "").strip(),
                            "full_name": (getattr(company, "full_name", "") or "").strip(),
                            "email": (getattr(company, "email", "") or "").strip(),
                        } if (invoice_required == "YES") else None,

                        "amount_parking_cop": int(amount_parking_cop),
                        "amount_service_cop": int(amount_service_cop),
                        "amount_total_cop": int(max(0, amount_parking_cop + amount_service_cop)),

                        "sum_total_db": int(sum_total_db),
                        "tickets_count": len(receipt_tickets),
                    },
                    "tickets": receipt_tickets,
                }

                # =========================
                # ✅ NUEVO: PREVIEW FACTURA ELECTRÓNICA (PRINT) — IGUAL QUE MENSUAL
                # =========================
                if invoice_required == "YES":
                    emit_electronic_invoice_preview(
                        id_number=(getattr(company, "id_number", "") or "").strip(),
                        full_name=(getattr(company, "full_name", "") or "").strip(),
                        email=(getattr(company, "email", "") or "").strip(),
                        items=[
                                {
                                    "plate": x.get("plate", ""),
                                    "parking": int(x.get("parking_amount", 0) or 0),
                                    "work": int(x.get("work_amount", 0) or 0),
                                    "work_type": (x.get("work_type") or "NONE").strip().upper(),
                                }
                                for x in (receipt_tickets or [])
                            ],
                    )


                request.session["last_company_settlement"] = receipt_data
                request.session.modified = True

                messages.success(
                    request,
                    f"Empresa '{company.full_name}' liquidada: {len(tickets_list)} ticket(s). "
                    f"Parqueadero={fmt_cop(amount_parking_cop)} | Servicio={fmt_cop(amount_service_cop)}"
                )

                return redirect(reverse("parking:company_settle_receipt"))

            messages.error(request, "No se pudo liquidar. Revisa los campos.")

        return render(request, "parking/gestion.html", {
            "mode": mode,
            "form": form,
            "is_admin": True,
        })

  

    # =========================
    # CUSTOMER (sin cambios)
    # =========================
    if request.method == "POST" and mode == "customer":
        id_posted = request.POST.get("id_number")
        instance = Customer.objects.filter(id_number=id_posted).first()
        form = CustomerForm(request.POST, instance=instance)

        if form.is_valid():
            c = form.save()
            verb = "actualizado" if instance else "registrado"
            messages.success(request, f"Cliente '{c.full_name}' {verb} correctamente.")
            return redirect(f"{reverse('parking:gestion')}?mode=customer")
        messages.error(request, "Error al guardar. Verifica los datos.")
    else:
        form = CustomerForm()

    return render(request, "parking/gestion.html", {
        "mode": mode,
        "form": form,
        "is_admin": True,
    })

# Asegúrate de importar tu modelo
# from .models import MonthlyPlate

def _normalize_plate(v: str) -> str:
    return (v or "").strip().upper().replace(" ", "").replace("-", "")

def _to_int_money(v, default=0) -> int:
    """
    Acepta '1.234', '1,234', '$ 1.234' etc.
    """
    try:
        s = str(v or "").strip()
        digits = "".join(ch for ch in s if ch.isdigit())
        return int(digits) if digits else default
    except Exception:
        return default

def _monthly_panel_url():
    return f"{reverse('parking:gestion')}?mode=monthly"


@staff_member_required(login_url="parking:login")
def monthly_check(request):
    """
    AJAX: valida si la placa YA está registrada como mensual.
    Retorna: success + exists
    """
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        return JsonResponse({"success": False, "message": "Bad request."})

    plate = _normalize_plate(request.GET.get("plate"))
    if not plate or (not plate.isalnum()) or len(plate) < 5:
        return JsonResponse({"success": True, "exists": False, "reason": "format"})

    exists = MonthlyPlate.objects.filter(plate=plate).exists()
    return JsonResponse({"success": True, "exists": bool(exists)})



@staff_member_required(login_url="parking:login")
@require_POST
def monthly_add(request):
    plate = (request.POST.get("plate") or "").strip().upper().replace(" ", "").replace("-", "")
    if not plate or (not plate.isalnum()) or len(plate) < 5:
        return JsonResponse({"ok": False, "error": "Placa inválida."}, status=400)

    obj, created = MonthlyPlate.objects.get_or_create(plate=plate)

    # ✅ Si viene por AJAX, respondemos JSON
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "created": bool(created), "id": obj.id, "plate": obj.plate})

    # ✅ Si NO es AJAX (POST normal), redirige al panel mensual
    return redirect(f"{reverse('parking:gestion')}?mode=monthly")



@staff_member_required(login_url="parking:login")
@require_POST
def monthly_delete(request, pk: int):
    """
    POST normal: elimina y vuelve al panel.
    """
    next_url = request.GET.get("next") or _monthly_panel_url()

    obj = get_object_or_404(MonthlyPlate, pk=pk)
    plate = obj.plate
    obj.delete()

    messages.success(request, f"Placa mensual eliminada: {plate}")
    return redirect(next_url)

@staff_member_required(login_url="parking:login")
@require_POST
def monthly_charge(request, pk: int):
    obj = get_object_or_404(MonthlyPlate, pk=pk)

    # ✅ monto manual (acepta 1.234 / 1,234 / 1234)
    raw = (request.POST.get("amount_cop") or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    amount = int(digits) if digits else 0
    if amount <= 0:
        return JsonResponse({"ok": False, "error": "Monto inválido."}, status=400)

    # ✅ método
    method = (request.POST.get("method") or "CASH").strip().upper()
    if method not in ("CASH", "TRANSFER"):
        method = "CASH"

    transfer_ref = (request.POST.get("transfer_ref") or "").strip()
    if method == "TRANSFER" and not transfer_ref:
        return JsonResponse({"ok": False, "error": "Referencia requerida."}, status=400)

    # ✅ factura electrónica (mismo patrón que empresa)
    inv_raw = (request.POST.get("invoice_required") or "NO").strip().upper()
    invoice_required = (inv_raw == "YES")

    id_number = (request.POST.get("id_number") or "").strip()
    full_name = (request.POST.get("full_name") or "").strip()
    email = (request.POST.get("email") or "").strip()

    # =========================
    # ✅ AJUSTE SOLICITADO:
    # - Si invoice_required=YES:
    #   - exige cédula/NIT
    #   - si existe Customer: autocompleta (y reactiva si está inactivo)
    #   - si NO existe: exige nombre + email válido y lo CREA en BD para futuras facturas
    # =========================
    customer = None
    if invoice_required:
        if not id_number:
            return JsonResponse({"ok": False, "error": "Falta cédula/NIT para factura electrónica."}, status=400)

        # busca sin filtrar active para no “perder” clientes
        customer = Customer.objects.filter(id_number=id_number).first()

        if customer:
            # autocompleta desde BD si no mandaron datos (o aunque manden, prioriza BD)
            full_name = (customer.full_name or "").strip() or full_name
            email = (customer.email or "").strip() or email

            # asegúralo activo para futuras facturas
            if hasattr(customer, "active") and customer.active is False:
                customer.active = True
                customer.save(update_fields=["active"])
        else:
            # no existe: exigir datos y crearlo
            if not full_name:
                return JsonResponse({"ok": False, "error": "Cliente no existe: falta nombre/razón social."}, status=400)

            try:
                validate_email(email)
            except ValidationError:
                return JsonResponse({"ok": False, "error": "Correo inválido para factura electrónica."}, status=400)

            customer = Customer.objects.create(
                id_number=id_number,
                full_name=full_name,
                email=email,
                active=True,
                is_company=False,
            )
            # si tienes esta función en tu proyecto (ya la usas en empresa)
            try:
                force_credit_disabled(customer)
            except Exception:
                pass
    else:
        # si NO, limpia por seguridad
        id_number = ""
        full_name = ""
        email = ""
        customer = None

    now = timezone.now()

    # ✅ crear Payment solo si tu modelo lo permite
    payment_id = None
    print("DEBUG monthly_charge invoice_required=", invoice_required, "id_number=", id_number)

    try:
        p = Payment.objects.create(
            method=method,
            status="PAID",
            amount_cop=amount,
            transfer_ref=transfer_ref if method == "TRANSFER" else "",
            company=None,
            customer=customer,
            invoice_required=invoice_required,
        )

        # ✅ si tu Payment tiene campos de factura, se llenan; si no, no revienta
        # (y como fallback, lo metemos en note)
        wrote_any = False
        for field_name, value in (
            ("id_number", id_number),
            ("full_name", full_name),
            ("email", email),
        ):
            if invoice_required and value and hasattr(p, field_name):
                setattr(p, field_name, value)
                wrote_any = True

        # ✅ AJUSTE MÍNIMO: solo usar note si existe en el modelo
        if invoice_required and not wrote_any and hasattr(p, "note"):
            p.note = (getattr(p, "note", "") or "").strip()
            extra = f" | FACTURA: {id_number} - {full_name} - {email}"
            p.note = (p.note + extra).strip()

        if invoice_required:
            p.invoice_required = True

        p.save()
        payment_id = p.id

        # =========================
        # ✅ NUEVO: PREVIEW FACTURA ELECTRÓNICA (PRINT)
        # =========================
        if invoice_required:
            emit_electronic_invoice_preview(
                id_number=(getattr(customer, "id_number", "") or id_number).strip(),
                full_name=(getattr(customer, "full_name", "") or full_name).strip(),
                email=(getattr(customer, "email", "") or email).strip(),
                items=[{
                    "plate": obj.plate,  # MonthlyPlate
                    "parking": int(getattr(p, "amount_cop", 0) or 0),
                    "work": 0,
                    "work_type": "NONE",  # mensual no tiene servicio
                }],
            )

    except Exception as e:
        print("❌ ERROR creando Payment en monthly_charge:", repr(e))
        payment_id = None

    # ✅ Guardar recibo en SESSION (para /monthly_receipt/)
    request.session["last_monthly_payment"] = {
        "receipt": {
            "created_at": timezone.localtime(now).strftime("%Y-%m-%d %H:%M"),
            "plate": obj.plate,
            "amount_cop": int(amount),
            "payment_id": payment_id,
            "method": method,
            "transfer_ref": transfer_ref if method == "TRANSFER" else "",
            "invoice_required": bool(invoice_required),
            "id_number": id_number,
            "full_name": full_name,
            "email": email,
        }
    }
    request.session.modified = True

    receipt_url = reverse("parking:monthly_receipt")

    # ✅ Si viene por AJAX, devolvemos JSON
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    if is_ajax:
        return JsonResponse({"ok": True, "receipt_url": receipt_url})

    # ✅ Si es POST normal (form submit), REDIRIGIMOS al recibo
    return redirect(receipt_url)

@staff_member_required(login_url="parking:login")
def monthly_receipt(request):

    data = request.session.get("last_monthly_payment")
    if not data:
        messages.warning(request, "No hay un recibo mensual reciente en sesión.")
        return redirect(_monthly_panel_url())

    receipt = data.get("receipt") or {}

    # 🔒 EVITAR generar consecutivo si ya existe en sesión
    if not receipt.get("receipt_number"):

        last = MonthlyReceipt.objects.aggregate(
            Max("consecutive")
        )["consecutive__max"] or 0

        new_consecutive = last + 1

        receipt_obj = MonthlyReceipt.objects.create(
            consecutive=new_consecutive,
            amount_cop=receipt.get("amount_cop", 0),
        )

        receipt["receipt_number"] = receipt_obj.consecutive

    # ✅ FORMATEO PARA HTML
    receipt["print_amount"] = fmt_cop(receipt.get("amount_cop", 0))

    # Guardar nuevamente en sesión
    data["receipt"] = receipt
    request.session["last_monthly_payment"] = data
    request.session.modified = True

    finish_url = request.GET.get("next") or _monthly_panel_url()

    return render(request, "parking/monthly_receipt.html", {
        "data": data,
        "receipt": receipt,
        "finish_url": finish_url,
    })

from django.db.models import Max
from .models import CompanyReceiptNumber  # el nuevo modelo simple

@staff_member_required(login_url="parking:login")
def company_settle_receipt(request):
    data = request.session.get("last_company_settlement")
    if not data:
        messages.error(request, "No hay un recibo reciente para mostrar.")
        return redirect(f"{reverse('parking:gestion')}?mode=company_settle")

    receipt = data.get("receipt") or {}

    # ✅ Formateos COP
    receipt["print_amount_parking"] = fmt_cop(receipt.get("amount_parking_cop", 0))
    receipt["print_amount_service"] = fmt_cop(receipt.get("amount_service_cop", 0))
    receipt["print_amount_total"] = fmt_cop(receipt.get("amount_total_cop", 0))

    # ✅ Número consecutivo 1,2,3... (solo una vez)
    if not receipt.get("receipt_number"):
        last = CompanyReceiptNumber.objects.aggregate(Max("consecutive"))["consecutive__max"] or 0
        new_consecutive = last + 1

        obj = CompanyReceiptNumber.objects.create(
            consecutive=new_consecutive,
            amount_cop=int(receipt.get("amount_total_cop", 0) or 0),
        )
        receipt["receipt_number"] = obj.consecutive

    # ✅ Guardar sin modificar otras llaves (lista incluida)
    data["receipt"] = receipt
    request.session["last_company_settlement"] = data
    request.session.modified = True

    return render(request, "parking/company_settle_receipt.html", {
        **data,           # mantiene TODO lo que ya venía (incluida la lista si estaba afuera)
        "receipt": receipt,  # y además pasa receipt explícito
    })



@login_required
def active_vehicles_report(request):

    qs = (
        Ticket.objects.filter(status="ACTIVE")
        .select_related("vehicle_type")
        .order_by("check_in")
    )

    active_count = qs.count()

    is_admin = bool(request.user.is_staff or request.user.is_superuser)

    # ✅ MENÚ PRINCIPAL según rol
    if is_admin:
        menu_url = f"{reverse('parking:gestion')}?mode=menu"
    else:
        menu_url = f"{reverse('parking:operator_panel')}?mode=menu"

    finish_url = menu_url

    # ============================
    # ✅ Mensuales (desconectados del ticket)
    # ============================
    def _norm_plate(p: str) -> str:
        return (p or "").strip().upper().replace(" ", "").replace("-", "")

    monthly_qs = MonthlyPlate.objects.all().order_by("plate")  # o .filter(active=True) si aplica
    monthly_plates = list(monthly_qs)
    monthly_set = {_norm_plate(mp.plate) for mp in monthly_plates if mp.plate}
    monthly_count = len(monthly_plates)

    # ============================
    # ✅ Separar activos: mensuales vs habituales (por placa)
    # ============================
    monthly_tickets = []
    habitual_tickets = []

    for t in qs:
        plate_norm = _norm_plate(getattr(t, "plate", ""))
        if plate_norm and plate_norm in monthly_set:
            monthly_tickets.append(t)
        else:
            habitual_tickets.append(t)

    # ✅ Ajuste solicitado:
    # NO bloquear si no hay ACTIVE, siempre que existan mensuales registrados
    if active_count == 0 and monthly_count == 0:
        messages.info(request, "No hay vehículos activos ni placas mensuales registradas para imprimir.")
        return redirect(finish_url)

    # ✅ GENERAR CONSECUTIVO REAL
    last = ActiveVehiclesReport.objects.aggregate(
        Max("consecutive")
    )["consecutive__max"] or 0

    new_consecutive = last + 1

    report = ActiveVehiclesReport.objects.create(
        consecutive=new_consecutive,
        active_count=active_count,  # (activos reales, no incluye mensuales “registrados”)
    )

    return render(
        request,
        "parking/active_vehicles_report.html",
        {
            # ✅ Activos reales
            "active_tickets": qs,
            "active_count": active_count,

            # ✅ Activos separados por tipo
            "habitual_tickets": habitual_tickets,
            "monthly_tickets": monthly_tickets,

            # ✅ Mensuales registrados (aunque no estén como ACTIVE)
            "monthly_plates": monthly_plates,
            "monthly_count": monthly_count,

            "now": timezone.now(),
            "finish_url": finish_url,
            "report_id": report.consecutive,
        },
    )

@staff_member_required(login_url="parking:login")
def admin_dashboard(request):
    vehicle_types = VehicleType.objects.filter(active=True).order_by("name")
    return render(request, "parking/admin_dashboard.html", {
        "is_admin_dashboard": True,
        "vehicle_types": vehicle_types,
    })


@staff_member_required(login_url="parking:login")
def admin_dashboard_data(request):
    try:
        # --- 0) Filtros ---
        vehicle_type = request.GET.get("vehicle_type", "ALL")  # "ALL" o id

        # --- 1) Rango de fechas (default: últimos 30 días) ---
        start_str = request.GET.get("start")
        end_str = request.GET.get("end")

        today = timezone.localdate()
        try:
            if start_str and end_str:
                start_date = timezone.datetime.strptime(start_str, "%Y-%m-%d").date()
                end_date = timezone.datetime.strptime(end_str, "%Y-%m-%d").date()
            else:
                raise ValueError("Sin fechas")
        except ValueError:
            end_date = today
            start_date = today - timedelta(days=29)

        tz = timezone.get_current_timezone()
        start_dt = timezone.make_aware(
            timezone.datetime.combine(start_date, timezone.datetime.min.time()), tz
        )
        end_dt = timezone.make_aware(
            timezone.datetime.combine(end_date, timezone.datetime.max.time()), tz
        )

        # --- 2) KPIs (tickets) ---
        tickets_qs = Ticket.objects.all()
        if vehicle_type != "ALL":
            tickets_qs = tickets_qs.filter(vehicle_type_id=vehicle_type)

        tickets_active = tickets_qs.filter(status="ACTIVE").count()
        tickets_pending = tickets_qs.filter(status="PENDING").count()
        monthly_clients_count = MonthlyPlate.objects.count()

        # Tickets pagados (según ticket)
        tickets_paid_count = tickets_qs.filter(
            status="PAID",
            check_out__range=(start_dt, end_dt)
        ).count()

        # --- 3) INGRESOS SOLO PARQUEADERO (PAGADO) ---
        # ✅ Solo Payment pagado + con ticket (parqueadero)
        payments_qs = Payment.objects.filter(
            status="PAID",
            created_at__range=(start_dt, end_dt),
        ).exclude(ticket__isnull=True)

        if vehicle_type != "ALL":
            payments_qs = payments_qs.filter(ticket__vehicle_type_id=vehicle_type)

        revenue_range = int(payments_qs.aggregate(total=Sum("amount_cop")).get("total") or 0)

        # --- 4) GRÁFICO DIARIO (PARQUEADERO) ---
        qs_daily = (
            payments_qs
            .annotate(d=TruncDate("created_at"))
            .values("d", "ticket__vehicle_type__name")
            .annotate(total=Sum("amount_cop"))
            .order_by("d", "ticket__vehicle_type__name")
        )

        delta = (end_date - start_date).days
        daily_labels = [str(start_date + timedelta(days=i)) for i in range(delta + 1)]

        by_type = {}
        for r in qs_daily:
            tname = r["ticket__vehicle_type__name"] or "Otros"
            d_str = str(r["d"])
            by_type.setdefault(tname, {})[d_str] = int(r["total"] or 0)

        if vehicle_type == "ALL":
            daily_series = [
                {"name": t, "data": [by_type[t].get(day, 0) for day in daily_labels]}
                for t in sorted(by_type.keys())
            ]
        else:
            # una sola serie
            tname = next(iter(by_type.keys()), "Seleccionado")
            daily_series = [{"name": tname, "data": [by_type.get(tname, {}).get(day, 0) for day in daily_labels]}]

        # --- 5) MÉTODO DE PAGO (PARQUEADERO, PAGADO) ---
        qs_method = (
            payments_qs
            .values("method")
            .annotate(total=Sum("amount_cop"))
            .order_by("-total")
        )
        method_labels = [str(x["method"] or "Otros") for x in qs_method]
        method_series = [int(x["total"] or 0) for x in qs_method]

        # --- 6) ESTADO DE TICKETS (en rango) ---
        qs_status = (
            tickets_qs.filter(check_in__range=(start_dt, end_dt))
            .values("status")
            .annotate(count=Count("id"))
            .order_by("status")
        )
        status_labels = [str(x["status"]) for x in qs_status]
        status_series = [int(x["count"] or 0) for x in qs_status]

        # --- 7) ÚLTIMO CIERRE ---
        lc = Closure.objects.order_by("-date").first()
        last_closure_data = None
        if lc:
            last_closure_data = {
                "id": lc.id,
                "date": lc.date.strftime("%Y-%m-%d %H:%M"),
                "total": int(getattr(lc, "total_amount", 0) or 0),
            }

        return JsonResponse({
            "filters": {"start": str(start_date), "end": str(end_date), "vehicle_type": str(vehicle_type)},
            "kpis": {
                "active_now": tickets_active,
                "pending_now": tickets_pending,
                "revenue_range": revenue_range,          # ✅ SOLO parqueadero pagado
                "payments_paid_count": tickets_paid_count,
                "monthly_clients_count": monthly_clients_count,
            },
            "charts": {
                "daily_revenue": {"labels": daily_labels, "series": daily_series},  # ✅ parqueadero pagado
                "payment_method_amount": {"labels": method_labels, "series": method_series},  # ✅ parqueadero pagado
                "tickets_by_status": {"labels": status_labels, "series": status_series},
            },
            "last_closure": last_closure_data,
        })

    except Exception as e:
        print("--- ERROR DASHBOARD ---")
        print(traceback.format_exc())
        return JsonResponse({"error": str(e)}, status=500)

@staff_member_required
def einvoices_outbox(request):
    invoices = ElectronicInvoiceOutbox.objects.all().order_by("-created_at")
    return render(request, "parking/einvoices_outbox.html", {
        "pending_invoices": invoices,
    })


# views.py
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .models import ElectronicInvoiceOutbox, WorkType
from .forms import EInvoiceOutboxForm, EInvoiceItemFormSet

JSON_FIELD = "items"  # 👈 CAMBIA por el nombre real del campo JSON en ElectronicInvoiceOutbox


def _items_from_inv(inv):
    val = getattr(inv, JSON_FIELD, None)
    return val if isinstance(val, list) else []


def _items_to_initial(items_list):
    """
    Convierte [{"work_type":"DESEMBARILLADO", ...}, ...]
    a initial para el formset, resolviendo WorkType por code.
    """
    initials = []
    for it in (items_list or []):
        code = (it.get("work_type") or "NONE").strip().upper()

        wt_obj = None
        if code and code != "NONE":
            wt_obj = WorkType.objects.filter(active=True, code__iexact=code).first()

        initials.append({
            "plate": (it.get("plate") or "").strip().upper(),
            "work_type": wt_obj,  # ModelChoiceField espera objeto o None
            "parking": int(it.get("parking") or 0),
            "work": int(it.get("work") or 0),
        })
    return initials

from .models import ElectronicInvoiceOutbox, WorkType
from .forms import EInvoiceOutboxForm, EInvoiceItemFormSet


@staff_member_required
def einvoice_edit(request, pk):
    inv = get_object_or_404(ElectronicInvoiceOutbox, pk=pk)
    nxt = _safe_next(request, request.GET.get("next"))

    # ===== helpers =====
    def build_initial(items_json):
        initial = []
        for it in (items_json or []):
            if not isinstance(it, dict):
                continue

            plate = (it.get("plate") or "").strip().upper()
            if not plate:
                continue

            parking = int(it.get("parking") or 0)
            work = int(it.get("work") or 0)

            wt_code = (it.get("work_type") or "NONE").strip().upper() or "NONE"
            wt_obj = None
            if wt_code != "NONE":
                wt_obj = WorkType.objects.filter(code__iexact=wt_code, active=True).first()

            initial.append({
                "plate": plate,
                "work_type": wt_obj,   # ModelChoiceField espera instancia
                "parking": parking,
                "work": work,
            })
        return initial

    if request.method == "POST":
        form = EInvoiceOutboxForm(request.POST, instance=inv)
        items_formset = EInvoiceItemFormSet(request.POST, prefix="items")

        form_valid = form.is_valid()
        formset_valid = items_formset.is_valid()

        print("FORM ERRORS:", form.errors)
        print("FORMSET ERRORS:", items_formset.errors)
        print("FORMSET NON FORM ERRORS:", items_formset.non_form_errors())

        if form_valid and formset_valid:
            # construir items_out desde el formset
            items_out = []
            for f in items_formset:
                cd = getattr(f, "cleaned_data", None) or {}
                if not cd:
                    continue
                if cd.get("DELETE"):
                    continue

                plate = (cd.get("plate") or "").strip().upper()

                wt_obj = cd.get("work_type")  # WorkType o None
                wt_code = "NONE"
                if wt_obj:
                    wt_code = (getattr(wt_obj, "code", "") or "").strip().upper() or "NONE"

                parking = int(cd.get("parking") or 0)
                work = int(cd.get("work") or 0)

                items_out.append({
                    "plate": plate,
                    "work_type": wt_code,
                    "parking": parking,
                    "work": work,
                })

            try:
                with transaction.atomic():
                    # NO uses form.save() directo (porque llama save() y full_clean)
                    # Mejor: asigna y guarda una sola vez al final.
                    inv.id_number = (form.cleaned_data.get("id_number") or "").strip()
                    inv.full_name = (form.cleaned_data.get("full_name") or "").strip()
                    inv.email = (form.cleaned_data.get("email") or "").strip()

                    # 🔥 lo importante
                    inv.items = items_out

                    # ✅ que el modelo calcule total/hash y valide todo
                    inv.save()

                messages.success(request, "Registro actualizado.")
                return redirect(nxt or reverse("parking:einvoices_outbox"))

            except ValidationError as e:
                # Esto te mostrará por qué no guardó (items vacíos, total 0, etc.)
                form.add_error(None, e)
                messages.error(request, "No se pudo guardar. Revisa los errores.")
        else:
            messages.error(request, "Revisa los campos marcados.")
    else:
        form = EInvoiceOutboxForm(instance=inv)
        items_formset = EInvoiceItemFormSet(initial=build_initial(inv.items), prefix="items")

    return render(request, "parking/einvoice_edit.html", {
        "inv": inv,
        "form": form,
        "items_formset": items_formset,
        "next": nxt,
    })
@staff_member_required
def einvoice_delete(request, pk):
    inv = get_object_or_404(ElectronicInvoiceOutbox, pk=pk)
    nxt = _safe_next(request, request.GET.get("next"))

    if request.method == "POST":
        inv.delete()
        messages.success(request, "Registro eliminado.")
        return redirect(nxt or reverse("parking:einvoices_outbox"))

    messages.error(request, "Método no permitido.")
    return redirect(nxt or reverse("parking:einvoices_outbox"))


@staff_member_required
def einvoice_retry(request, pk):
    inv = get_object_or_404(ElectronicInvoiceOutbox, pk=pk)
    nxt = _safe_next(request, request.GET.get("next"))

    if request.method != "POST":
        messages.error(request, "Método no permitido.")
        return redirect(nxt or reverse("parking:einvoices_outbox"))

    # marca intento (opcional, emit también lo marca)
    inv.last_attempt_at = timezone.now()
    inv.save(update_fields=["last_attempt_at"])

    try:
        result = emit_electronic_invoice_preview(
            outbox_pk=inv.pk,              # ✅ clave: actualiza ESTE MISMO registro
            id_number=inv.id_number,
            full_name=inv.full_name,
            email=inv.email,
            items=list(inv.items or []),
        )

        # ✅ recarga lo que realmente quedó en BD (status / last_error)
        inv.refresh_from_db()

        if inv.status == "SENT":
            messages.success(request, "Factura enviada exitosamente (marcada como Enviada).")
        elif inv.status == "ERROR":
            messages.error(request, f"No se pudo enviar: {inv.last_error or 'Error no especificado'}")
        else:
            # si por alguna razón queda PENDING
            err = None
            if isinstance(result, dict):
                err = result.get("error") or result.get("errors") or result.get("message_error")
            if err:
                messages.error(request, f"No se pudo enviar: {err}")
            else:
                messages.warning(request, "El envío terminó sin error explícito, pero el estado quedó Pendiente. Revisa logs/Siigo.")

        return redirect(nxt or reverse("parking:einvoices_outbox"))

    except Exception as e:
        # si emit explotó antes de poder guardar estado
        inv.status = "ERROR"
        inv.last_error = str(e)[:2000]
        inv.save(update_fields=["status", "last_error"])
        messages.error(request, f"No se pudo enviar: {e}")
        return redirect(nxt or reverse("parking:einvoices_outbox"))


@staff_member_required
def einvoices_retry_all(request):
    nxt = _safe_next(request, request.GET.get("next"))

    if request.method != "POST":
        messages.error(request, "Método no permitido.")
        return redirect(nxt or reverse("parking:einvoices_outbox"))

    # ✅ reintenta lo reintentable: PENDING y ERROR (NO SENT)
    qs = (ElectronicInvoiceOutbox.objects
          .filter(status__in=["PENDING", "ERROR"])
          .order_by("created_at"))

    ok, fail = 0, 0
    now = timezone.now()
    qs.update(last_attempt_at=now)

    for inv in qs:
        try:
            emit_electronic_invoice_preview(
                outbox_pk=inv.pk,          # ✅ actualiza el mismo registro
                id_number=inv.id_number,
                full_name=inv.full_name,
                email=inv.email,
                items=list(inv.items or []),
            )

            inv.refresh_from_db()
            if inv.status == "SENT":
                ok += 1
            elif inv.status == "ERROR":
                fail += 1
            else:
                # quedó PENDING sin error explícito
                fail += 1

        except Exception as e:
            inv.status = "ERROR"
            inv.last_error = str(e)[:2000]
            inv.save(update_fields=["status", "last_error"])
            fail += 1

    messages.success(request, f"Reintentos finalizados. OK: {ok} | Error: {fail}")
    return redirect(nxt or reverse("parking:einvoices_outbox"))