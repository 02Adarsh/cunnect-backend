"""v78.

1. Ride.awaiting_balance — a 50-50 ride stays open until the student
   clears the second half.
2. "Food Court" is renamed to "CUnnect Food" everywhere (built-in store
   section title + the flash messages in the app).
"""

from django.db import migrations, models


def rename_food_court(apps, schema_editor):
    StoreSection = apps.get_model("myapp", "StoreSection")
    StoreSection.objects.filter(title="Food Court").update(title="CUnnect Food")


def revert_food_court(apps, schema_editor):
    StoreSection = apps.get_model("myapp", "StoreSection")
    StoreSection.objects.filter(title="CUnnect Food").update(title="Food Court")


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0009_suv_rename_and_vehicles"),
        ("myapp", "0005_banner"),
    ]

    operations = [
        migrations.AddField(
            model_name="ride",
            name="awaiting_balance",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(rename_food_court, revert_food_court),
    ]
