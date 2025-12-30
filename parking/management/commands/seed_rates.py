import random
from django.core.management.base import BaseCommand
from parking.models import VehicleType, RatePlan

class Command(BaseCommand):
    help = "Crea tipos de vehículo y tarifas aleatorias entre 1000 y 1500 COP."

    def handle(self, *args, **options):
        vehicle_names = ["Moto", "Carro", "Camioneta"]
        rate_names = ["Hora"]

        vtypes = []
        for name in vehicle_names:
            vt, _ = VehicleType.objects.get_or_create(name=name)
            vtypes.append(vt)

        created = 0
        for vt in vtypes:
            for rn in rate_names:
                price = random.randint(1000, 1500)
                rp, was_created = RatePlan.objects.get_or_create(
                    name=rn,
                    vehicle_type=vt,
                    defaults={
                        "price_cop": price,
                        "billing_unit": "HOUR",
                        "unit_size": 60,
                        "active": True,
                    },
                )
                if not was_created:
                    # si ya existía, actualiza el precio aleatorio
                    rp.price_cop = price
                    rp.save()
                created += 1

        self.stdout.write(self.style.SUCCESS(f"Listo. Tarifas creadas/actualizadas: {created}"))
