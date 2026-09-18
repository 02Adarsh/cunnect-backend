from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="RideRejection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("ride", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="rejections", to="ride.ride")),
                ("vendor", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="rejections", to="ride.ridevendor")),
            ],
            options={
                "unique_together": {("ride", "vendor")},
            },
        ),
    ]
