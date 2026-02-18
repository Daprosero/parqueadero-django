from django.db import migrations

DEFAULTS = [
    ("NONE", "Ninguno"),
    ("EMBARILLADO", "Embarillado"),
    ("DESEMBARILLADO", "Des-embarillado"),
    ("ENCARPADO", "Encarpado"),
    ("OTHER", "Otros"),
]

def forwards(apps, schema_editor):
    WorkType = apps.get_model("parking", "WorkType")
    Ticket = apps.get_model("parking", "Ticket")

    # Crear WorkType base si no existen
    for code, name in DEFAULTS:
        WorkType.objects.get_or_create(code=code, defaults={"name": name, "active": True})

    # Mapear work_type_code -> work_type_fk
    for t in Ticket.objects.all().only("id", "work_type_code"):
        code = (t.work_type_code or "NONE").strip().upper()
        wt = WorkType.objects.filter(code=code).first()
        if wt:
            t.work_type_fk_id = wt.id
            t.save(update_fields=["work_type_fk"])

def backwards(apps, schema_editor):
    Ticket = apps.get_model("parking", "Ticket")
    Ticket.objects.update(work_type_fk=None)

class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0024_worktype_alter_systemsettings_options_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
