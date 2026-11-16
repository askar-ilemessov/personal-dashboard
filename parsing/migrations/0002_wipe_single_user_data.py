from django.db import migrations


def wipe(apps, schema_editor):
    apps.get_model("parsing", "Draft").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("parsing", "0001_initial")]
    operations = [migrations.RunPython(wipe, migrations.RunPython.noop)]
