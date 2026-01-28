from django.core.management.base import BaseCommand
from parking.models import VehicleType, ClientType, RatePlan

class Command(BaseCommand):
    help = "Crea datos base reproducibles: vehículos, clientes y tarifas."

    def handle(self, *args, **options):
        vehicle_names = ["Camión", "Tractomula", "Doble Troque"]
        client_names = ["Taller", "Parqueadero"]

        vehicles = {}
        for name in vehicle_names:
            vt, _ = VehicleType.objects.get_or_create(name=name, defaults={"active": True})
            vt.active = True
            vt.save(update_fields=["active"])
            vehicles[name] = vt

        for name in client_names:
            ct, _ = ClientType.objects.get_or_create(name=name, defaults={"active": True})
            ct.active = True
            ct.save(update_fields=["active"])

        # Precios ejemplo (AJÚSTALOS)
        prices_hora = {
            "Camión": 12000,
            "Tractomula": 15000,
            "Doble Troque": 14000,
        }
        prices_noche = {
            "Camión": 30000,
            "Tractomula": 40000,
            "Doble Troque": 35000,
        }

        for vname, vt in vehicles.items():
            # Hora (parqueadero)
            RatePlan.objects.update_or_create(
                name="Hora",
                vehicle_type=vt,
                defaults={
                    "price_cop": prices_hora[vname],
                    "billing_unit": "HOUR",
                    "unit_size": 60,
                    "active": True,
                }
            )

            # Noche (taller) -> precio POR NOCHE (fijo)
            RatePlan.objects.update_or_create(
                name="Noche",
                vehicle_type=vt,
                defaults={
                    "price_cop": prices_noche[vname],
                    "billing_unit": "HOUR",  # no se usa en el cálculo, pero queda consistente
                    "unit_size": 60,
                    "active": True,
                }
            )

        self.stdout.write(self.style.SUCCESS("✅ Catálogos listos (vehículos, clientes, tarifas Hora/Noche)."))
