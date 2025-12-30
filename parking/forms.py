from django import forms
from .models import Ticket, RatePlan, VehicleType

class TicketCreateForm(forms.ModelForm):
    class Meta:
        model = Ticket
        fields = ["plate", "vehicle_type", "rate_plan"]

    def clean(self):
        cleaned = super().clean()
        vehicle_type = cleaned.get("vehicle_type")
        rate_plan = cleaned.get("rate_plan")
        if vehicle_type and rate_plan and rate_plan.vehicle_type_id != vehicle_type.id:
            raise forms.ValidationError("La tarifa seleccionada no corresponde al tipo de vehículo.")
        if rate_plan and not rate_plan.active:
            raise forms.ValidationError("La tarifa seleccionada está inactiva.")
        return cleaned


class ChargeForm(forms.Form):
    plate = forms.CharField(max_length=12, label="Placa")
