from django.db import migrations


def forwards(apps, schema_editor):
    RatePlan = apps.get_model("parking", "RatePlan")

    for rp in RatePlan.objects.all():
        old = (getattr(rp, "name", "") or "").strip().lower()

        if old == "hora":
            rp.client_kind = "PARKING"
            rp.billing_unit = "HOUR"
        elif old == "noche":
            rp.client_kind = "WORKSHOP"
            rp.billing_unit = "NIGHT"
        else:
            rp.client_kind = rp.client_kind or "PARKING"
            rp.billing_unit = rp.billing_unit or "HOUR"

        rp.save(update_fields=["client_kind", "billing_unit"])


class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0013_alter_rateplan_options_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
