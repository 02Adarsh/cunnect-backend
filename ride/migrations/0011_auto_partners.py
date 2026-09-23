"""v79: ride partners who drive an AUTO.

An auto partner is outside the car booking flow — the AUTO button on the
student's Ride screen alerts every one of them at once. Nothing is
priced, paid or tracked for an auto, so no Ride row is created.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0010_balance_lock_and_cunnect_food"),
    ]

    operations = [
        migrations.AddField(
            model_name="ridevendor",
            name="is_auto",
            field=models.BooleanField(default=False),
        ),
    ]
