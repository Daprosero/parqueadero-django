from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required

from .forms import TicketCreateForm, ChargeForm
from .models import Ticket


def home(request):
    return redirect("login")


@login_required
def post_login_redirect(request):
    user = request.user

    # Admin -> Django Admin por defecto
    if user.is_staff or user.is_superuser:
        return redirect("/admin/")

    # Operario -> panel único
    return redirect("operator_panel")


@login_required
def operator_panel(request):
    mode = request.GET.get("mode", "checkin")  # checkin | charge

    checkin_form = TicketCreateForm()
    charge_form = ChargeForm()
    amount = None

    # ---- REGISTRO (checkin) ----
    if request.method == "POST" and mode == "checkin":
        checkin_form = TicketCreateForm(request.POST)
        if checkin_form.is_valid():
            ticket = checkin_form.save(commit=False)
            ticket.created_by = request.user
            ticket.save()
            messages.success(request, f"Ingreso registrado: {ticket.plate}")
            checkin_form = TicketCreateForm()  # limpiar formulario

    # ---- COBRO (charge) ----
    if request.method == "POST" and mode == "charge":
        charge_form = ChargeForm(request.POST)
        if charge_form.is_valid():
            plate = charge_form.cleaned_data["plate"].strip().upper()
            ticket = Ticket.objects.filter(
                plate__iexact=plate, status="ACTIVE"
            ).order_by("-check_in").first()

            if not ticket:
                messages.error(request, "No existe un ticket activo para esa placa.")
            else:
                now = timezone.now()
                amount = ticket.compute_amount_cop(end_time=now)
                ticket.check_out = now
                ticket.total_amount_cop = amount
                ticket.status = "CLOSED"
                ticket.closed_by = request.user
                ticket.save()
                messages.success(request, f"Cobro realizado: {plate} -> {amount} COP")

    return render(
        request,
        "parking/operator_panel.html",
        {
            "mode": mode,
            "checkin_form": checkin_form,
            "charge_form": charge_form,
            "amount": amount,
        },
    )

@staff_member_required
def admin_dashboard(request):
    return render(request, "parking/admin_dashboard.html", {"is_admin_dashboard": True})

