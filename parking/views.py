from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib import messages

from .forms import TicketCreateForm, ChargeForm
from .models import Ticket

@login_required
def operator_checkin(request):
    if request.method == "POST":
        form = TicketCreateForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.created_by = request.user
            ticket.save()
            messages.success(request, f"Ingreso registrado: {ticket.plate}")
            return redirect("operator_checkin")
    else:
        form = TicketCreateForm()
    return render(request, "parking/operator_checkin.html", {"form": form})


@login_required
def operator_charge(request):
    amount = None
    ticket = None

    if request.method == "POST":
        form = ChargeForm(request.POST)
        if form.is_valid():
            plate = form.cleaned_data["plate"].strip().upper()
            ticket = Ticket.objects.filter(plate__iexact=plate, status="ACTIVE").order_by("-check_in").first()
            if not ticket:
                messages.error(request, "No existe un ticket activo para esa placa.")
            else:
                # Calcular y cerrar
                now = timezone.now()
                amount = ticket.compute_amount_cop(end_time=now)
                ticket.check_out = now
                ticket.total_amount_cop = amount
                ticket.status = "CLOSED"
                ticket.closed_by = request.user
                ticket.save()
                messages.success(request, f"Cobro realizado: {plate} -> {amount} COP")
                ticket = None  # para limpiar vista
    else:
        form = ChargeForm()

    active_tickets = Ticket.objects.filter(status="ACTIVE").order_by("-check_in")[:20]

    return render(
        request,
        "parking/operator_charge.html",
        {"form": form, "amount": amount, "ticket": ticket, "active_tickets": active_tickets},
    )
