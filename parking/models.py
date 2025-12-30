from django.conf import settings
from django.db import models
from django.utils import timezone
from decimal import Decimal
import math

class VehicleType(models.Model):
    name = models.CharField(max_length=50, unique=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class RatePlan(models.Model):
    BILLING_UNIT_CHOICES = [
        ("MINUTES", "Minutes"),
        ("HOUR", "Hour"),
        ("DAY", "Day"),
    ]

    name = models.CharField(max_length=80)
    vehicle_type = models.ForeignKey(VehicleType, on_delete=models.PROTECT, related_name="rate_plans")
    price_cop = models.PositiveIntegerField()  # COP
    billing_unit = models.CharField(max_length=10, choices=BILLING_UNIT_CHOICES, default="HOUR")
    unit_size = models.PositiveIntegerField(default=60)  # e.g. 60 minutes for hourly, 15 for fractions
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("name", "vehicle_type")

    def __str__(self):
        return f"{self.name} - {self.vehicle_type.name} ({self.price_cop} COP)"


class Ticket(models.Model):
    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("CLOSED", "Closed"),
    ]

    plate = models.CharField(max_length=12, db_index=True)
    vehicle_type = models.ForeignKey(VehicleType, on_delete=models.PROTECT)
    rate_plan = models.ForeignKey(RatePlan, on_delete=models.PROTECT)
    check_in = models.DateTimeField(default=timezone.now)
    check_out = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="ACTIVE")
    total_amount_cop = models.PositiveIntegerField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tickets_created")
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="tickets_closed")

    def __str__(self):
        return f"{self.plate} - {self.status}"

    def compute_amount_cop(self, end_time=None) -> int:
        """
        Simple billing:
        - MINUTES: ceil(minutes / unit_size) * price
        - HOUR: ceil(minutes / 60) * price (unit_size ignored if HOUR; you can change if needed)
        - DAY: ceil(days) * price
        """
        end_time = end_time or timezone.now()
        delta = end_time - self.check_in
        total_minutes = max(1, int(delta.total_seconds() // 60))

        if self.rate_plan.billing_unit == "MINUTES":
            units = math.ceil(total_minutes / max(1, self.rate_plan.unit_size))
        elif self.rate_plan.billing_unit == "HOUR":
            units = math.ceil(total_minutes / 60)
        else:  # DAY
            days = delta.total_seconds() / 86400.0
            units = math.ceil(max(1.0, days))

        return int(units * self.rate_plan.price_cop)
