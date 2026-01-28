# views.py (ajustado para usar utils.py con lógica por tramos 06-18 / 18-06)
# - Helpers locales eliminados (fmt, client label, etc.)
# - Cotización (quote) y Inspect usan estimate_amount_cop()
# - Cierre de ACTIVE usa estimate_amount_cop() para congelar total_amount_cop con la nueva lógica
# - ticket_edit_paid recalcula total con estimate_amount_cop() (con check_out si existe)

from urllib.parse import quote

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
)

from .models import (
    Closure,
    Customer,
    Payment,
    RatePlan,
    Ticket,
    VehicleType,
    iter_pricing_segments,
    MonthlyPlate
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
)



def home(request):
    return redirect("parking:post_login_redirect")


@login_required
def post_login_redirect(request):
    user = request.user
    if user.is_staff or user.is_superuser:
        return redirect("/admin/")
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

    is_admin = request.user.is_staff or request.user.is_superuser
    if not is_admin:
        messages.error(request, "No autorizado.")
        return redirect(next_url or "parking:operator_panel")

    if request.method == "POST":
        form = EditPaidServiceForm(request.POST)
        if form.is_valid():
            new_type = (form.cleaned_data.get("service_type") or "NONE").strip()
            new_type_u = new_type.upper()
            new_amt = int(form.cleaned_data.get("service_amount_cop") or 0)

            if new_type_u == "NONE":
                new_type = "NONE"
                new_amt = 0

            # ✅ Actualiza “servicio principal” en work_*
            ticket.work_type = new_type
            ticket.work_amount_cop = new_amt

            # Legacy off
            ticket.service_type = "NONE"
            ticket.service_amount_cop = 0

            end_time = ticket.check_out or timezone.now()

            # ✅ Recalcula con la nueva lógica por tramos
            ticket.total_amount_cop = int(estimate_amount_cop(ticket, now=end_time) or 0)

            ticket.save(update_fields=[
                "work_type", "work_amount_cop",
                "service_type", "service_amount_cop",
                "total_amount_cop"
            ])

            payment_obj = None
            if hasattr(ticket, "payment") and ticket.payment:
                ticket.payment.amount_cop = ticket.total_amount_cop
                ticket.payment.save(update_fields=["amount_cop"])
                payment_obj = ticket.payment
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

    else:
        stxt = (ticket.work_type or "NONE").strip() or "NONE"
        samt = int(ticket.work_amount_cop or 0)
        form = EditPaidServiceForm(initial={"service_type": stxt, "service_amount_cop": samt})

    return render(request, "parking/ticket_edit_paid.html", {
        "ticket": ticket,
        "form": form,
        "next_url": next_url,
        "back_url": next_url,
    })


# =========================
# Imprimir / reimprimir recibo
# =========================

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.contrib import messages  # ✅ agrega esto

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


@staff_member_required
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
        "active_count": 0,
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
    if mode == "lookup_customer":
        id_val = request.GET.get("id", "").strip()
        customer = Customer.objects.filter(id_number=id_val).first()
        if customer:
            return JsonResponse({
                "found": True,
                "full_name": customer.full_name,
                "email": customer.email,
                "is_company": customer.is_company,
                "credit_enabled": getattr(customer, "credit_enabled", False),
            })
        return JsonResponse({"found": False})

    # =========================
    # AJAX: quote (estimación)
    # - ACTIVE: usa estimate_amount_cop() (cobro por tramos)
    # - PENDING: devuelve el total ya congelado
    # =========================
    if mode == "quote" and request.headers.get("x-requested-with") == "XMLHttpRequest":
        plate = request.GET.get("plate", "").strip().upper()

        # ✅ AJUSTE: ya NO filtramos por created_by
        ticket = (
            Ticket.objects.filter(plate__iexact=plate, status="ACTIVE")
            .select_related("vehicle_type")
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
                .select_related("vehicle_type")
                .order_by("-check_in")
                .first()
            )

            if not ticket:
                return JsonResponse({"success": False, "message": "Placa no encontrada"})

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
                "plan": "Pendiente"
            })

        # ✅ ACTIVE: estimación real con tramos
        now = timezone.now()
        estimated_amount = int(estimate_amount_cop(ticket, now=now) or 0)

        # unidades “informativas” (no afecta cálculo)
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
            "plan": plan_name
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
    plate_qs = (request.GET.get("plate") or "").strip().upper()
    if mode == "charge" and plate_qs:
        close_form = ClosePaymentForm(initial={"plate": plate_qs})
    else:
        close_form = ClosePaymentForm()

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

    # CHARGE
    if request.method == "POST" and mode == "charge":
        close_form = ClosePaymentForm(request.POST)
        if close_form.is_valid():
            plate = close_form.cleaned_data["plate"].strip().upper()
            method = close_form.cleaned_data["method"]
            transfer_ref = (close_form.cleaned_data.get("transfer_ref") or "").strip()

            # pueden venir del form, pero para PENDING NO se usan
            company = close_form.cleaned_data.get("company")
            work_type = (close_form.cleaned_data.get("work_type") or "NONE").strip()
            work_amount_cop = int(close_form.cleaned_data.get("work_amount_cop") or 0)

            invoice_required = (close_form.cleaned_data.get("invoice_required") == "YES")
            id_number = (close_form.cleaned_data.get("id_number") or "").strip()
            full_name = (close_form.cleaned_data.get("full_name") or "").strip()
            email = (close_form.cleaned_data.get("email") or "").strip()
            customer_obj = close_form.cleaned_data.get("_customer_obj")

            # ✅ AJUSTE: primero ACTIVE (sin created_by)
            ticket = Ticket.objects.filter(
                plate__iexact=plate, status="ACTIVE"
            ).select_related("vehicle_type").order_by("-check_in").first()

            # ✅ AJUSTE: si no, PENDING sin cierre (sin created_by)
            if not ticket:
                ticket = Ticket.objects.filter(
                    plate__iexact=plate,
                    status="PENDING",
                    closure__isnull=True
                ).select_related("vehicle_type").order_by("-check_in").first()

            if not ticket:
                messages.error(request, "No existe un ticket activo o pendiente.")
            else:
                # -------------------------
                # CASO PENDING: SOLO CASH/TRANSFER
                # -------------------------
                if ticket.status == "PENDING":
                    if method not in ("CASH", "TRANSFER"):
                        messages.error(request, "Un ticket PENDIENTE solo puede pagarse con EFECTIVO o TRANSFERENCIA.")
                    else:
                        customer = None

                        if method in ("CASH", "TRANSFER") and invoice_required:
                            if not customer_obj and id_number:
                                customer_obj = Customer.objects.filter(id_number=id_number).first()

                            if customer_obj:
                                customer = customer_obj
                            else:
                                if not id_number:
                                    close_form.add_error("id_number", "La cédula/NIT es obligatoria para factura.")
                                    messages.error(request, "Para factura electrónica debes ingresar cédula/NIT.")
                                    customer = None
                                elif not full_name or not email:
                                    if not full_name:
                                        close_form.add_error("full_name", "El nombre/razón social es obligatorio si el cliente no existe.")
                                    if not email:
                                        close_form.add_error("email", "El correo es obligatorio si el cliente no existe.")
                                    messages.error(request, "Cliente no encontrado. Completa nombre y correo para registrarlo.")
                                    customer = None
                                else:
                                    customer = Customer.objects.create(
                                        id_number=id_number,
                                        full_name=full_name,
                                        email=email,
                                        active=True,
                                        is_company=False
                                    )
                                    force_credit_disabled(customer)

                        if method in ("CASH", "TRANSFER") and invoice_required and close_form.errors:
                            pass
                        else:
                            now = timezone.now()

                            if not ticket.check_out:
                                ticket.check_out = now
                            ticket.closed_by = request.user
                            ticket.status = "PAID"
                            ticket.company = None
                            ticket.save(update_fields=["check_out", "closed_by", "status", "company"])

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

                            if not invoice_required and getattr(payment_obj, "customer_id", None):
                                payment_obj.customer = None
                                payment_obj.save(update_fields=["customer"])

                            # =========================
                            # ✅ NUEVO: PREVIEW FACTURA ELECTRÓNICA (PRINT)
                            # =========================
                            if invoice_required:
                                emit_electronic_invoice_preview(
                                    id_number=(getattr(customer, "id_number", "") or id_number).strip(),
                                    full_name=(getattr(customer, "full_name", "") or full_name).strip(),
                                    email=(getattr(customer, "email", "") or email).strip(),
                                    total_amount_cop=int(getattr(payment_obj, "amount_cop", 0) or 0),
                                )

                            return redirect("parking:print_receipt", payment_id=payment_obj.id)

                # -------------------------
                # CASO ACTIVE: congelar total con lógica por tramos
                # -------------------------
                elif ticket.status == "ACTIVE":
                    now = timezone.now()

                    customer = None
                    if method in ("CASH", "TRANSFER") and invoice_required:
                        if not customer_obj and id_number:
                            customer_obj = Customer.objects.filter(id_number=id_number).first()

                        if customer_obj:
                            customer = customer_obj
                        else:
                            if not id_number:
                                close_form.add_error("id_number", "La cédula/NIT es obligatoria para factura.")
                                messages.error(request, "Para factura electrónica debes ingresar cédula/NIT.")
                                customer = None
                            elif not full_name or not email:
                                if not full_name:
                                    close_form.add_error("full_name", "El nombre/razón social es obligatorio si el cliente no existe.")
                                if not email:
                                    close_form.add_error("email", "El correo es obligatorio si el cliente no existe.")
                                messages.error(request, "Cliente no encontrado. Completa nombre y correo para registrarlo.")
                                customer = None
                            else:
                                customer = Customer.objects.create(
                                    id_number=id_number,
                                    full_name=full_name,
                                    email=email,
                                    active=True,
                                    is_company=False
                                )
                                force_credit_disabled(customer)

                    if method in ("CASH", "TRANSFER") and invoice_required and close_form.errors:
                        pass
                    else:
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

                        # ✅ cálculo nuevo
                        amount = int(estimate_amount_cop(ticket, now=now) or 0)
                        ticket.total_amount_cop = amount

                        if method in ("CASH", "TRANSFER"):
                            ticket.status = "PAID"
                            ticket.company = None
                            ticket.save()

                            payment_obj, _ = Payment.objects.update_or_create(
                                ticket=ticket,
                                defaults={
                                    "method": method,
                                    "status": "PAID",
                                    "transfer_ref": transfer_ref if method == "TRANSFER" else "",
                                    "company": None,
                                    "amount_cop": ticket.total_amount_cop,
                                    "invoice_required": invoice_required,
                                    "customer": customer
                                }
                            )

                            # =========================
                            # ✅ NUEVO: PREVIEW FACTURA ELECTRÓNICA (PRINT)
                            # =========================
                            if invoice_required:
                                emit_electronic_invoice_preview(
                                    id_number=(getattr(customer, "id_number", "") or id_number).strip(),
                                    full_name=(getattr(customer, "full_name", "") or full_name).strip(),
                                    email=(getattr(customer, "email", "") or email).strip(),
                                    total_amount_cop=int(getattr(payment_obj, "amount_cop", 0) or 0),
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
                                    "transfer_ref": ""
                                }
                            )
                            return redirect("parking:print_receipt", payment_id=payment_obj.id)


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
    last_transactions = list(
        Ticket.objects.filter(closure__isnull=True)
        .select_related("vehicle_type")
        .annotate(last_dt=Coalesce("check_out", "check_in"))
        .order_by("-last_dt")[:3]
    )
    pay_map = {p.ticket_id: p for p in Payment.objects.filter(ticket_id__in=[t.id for t in last_transactions])}

    for t in last_transactions:
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
        "last_transactions": last_transactions,
        "pending_tickets": pending_tickets,
        "paid_tickets": paid_tickets,
        "paid_payments_by_ticket_id": paid_payments_by_ticket_id,

        "can_close": can_close,
        "closable_count": closable_count,
        "pending_count": pending_count,
        "active_count": active_count,

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
    active_count = Ticket.objects.filter(status="ACTIVE").count()

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
        form = CustomerForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Cliente registrado correctamente.")
            return redirect("parking:gestion")
        messages.error(request, "Error al registrar. Verifique los datos.")
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
        id_val = request.GET.get("id", "").strip()
        customer = Customer.objects.filter(id_number=id_val).first()
        if customer:
            return JsonResponse({
                "found": True,
                "full_name": customer.full_name,
                "email": customer.email,
                "is_company": customer.is_company,
                "credit_enabled": getattr(customer, "credit_enabled", False),
            })
        return JsonResponse({"found": False})

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
            Ticket.objects.filter(company=company, status="ASSIGNED", closure__isnull=True)
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
                base_qs = Ticket.objects.filter(company=company, status="ASSIGNED", closure__isnull=True)

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

                        receipt_tickets.append({
                            "id": t.id,
                            "plate": (t.plate or ""),
                            "check_in": ci,
                            "check_out": co,
                            "total": t_total,
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
                        total_amount_cop=int(receipt_data["receipt"].get("amount_total_cop") or 0),
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


from django.core.validators import validate_email
from django.core.exceptions import ValidationError

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
                total_amount_cop=int(getattr(p, "amount_cop", 0) or 0),
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

    # ✅ FORMATEOS PARA HTML
    receipt["print_amount"] = fmt_cop(receipt.get("amount_cop", 0))

    # (opcional) si quieres también mostrar “payment_id” etc. no hace falta

    data["receipt"] = receipt
    request.session["last_monthly_payment"] = data
    request.session.modified = True

    finish_url = request.GET.get("next") or _monthly_panel_url()

    return render(request, "parking/monthly_receipt.html", {
        "data": data,
        "receipt": receipt,
        "finish_url": finish_url,
    })



@staff_member_required(login_url="parking:login")
def company_settle_receipt(request):
    """
    Recibo consolidado usando SESSION (no requiere modelo nuevo).
    Ajuste: formatea montos (print_*) para el HTML, sin usar intcomma.
    """
    data = request.session.get("last_company_settlement")
    if not data:
        messages.error(request, "No hay un recibo reciente para mostrar.")
        return redirect(f"{reverse('parking:gestion')}?mode=company_settle")

    # ===== AJUSTE NECESARIO =====
    receipt = data.get("receipt") or {}

    # Formato COP igual al resto del sistema
    receipt["print_amount_parking"] = fmt_cop(receipt.get("amount_parking_cop", 0))
    receipt["print_amount_service"] = fmt_cop(receipt.get("amount_service_cop", 0))
    receipt["print_amount_total"] = fmt_cop(receipt.get("amount_total_cop", 0))

    data["receipt"] = receipt
    # ===========================

    return render(request, "parking/company_settle_receipt.html", data)

@login_required
def active_vehicles_report(request):
    active_tickets = list(
        Ticket.objects.filter(status="ACTIVE")
        .select_related("vehicle_type")
        .order_by("check_in")
    )

    is_admin = bool(request.user.is_staff or request.user.is_superuser)

    next_url = _safe_next(request, request.GET.get("next") or "")

    # ✅ MENÚ PRINCIPAL según rol
    if is_admin:
        menu_url = f"{reverse('parking:gestion')}?mode=menu"
    else:
        menu_url = f"{reverse('parking:operator_panel')}?mode=menu"

    finish_url = menu_url

    return render(request, "parking/active_vehicles_report.html", {
        "active_tickets": active_tickets,
        "active_count": len(active_tickets),
        "now": timezone.now(),
        "finish_url": finish_url,  # 👈 SOLO esto usa el template
    })



@staff_member_required
def admin_dashboard(request):
    return render(request, "parking/admin_dashboard.html", {"is_admin_dashboard": True})
