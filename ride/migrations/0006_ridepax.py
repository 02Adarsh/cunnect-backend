from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0005_riderblock_ride_contact_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="RidePax",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("name", models.CharField(blank=True, default="", max_length=80)),
                ("phone", models.CharField(blank=True, default="", max_length=20)),
                ("amount", models.DecimalField(decimal_places=2, default=0,
                                               max_digits=10)),
                ("paid", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("ride", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="pax", to="ride.ride")),
            ],
            options={
                "ordering": ["id"],
                "verbose_name": "Ride co-passenger",
                "verbose_name_plural": "Ride co-passengers",
            },
        ),
    ]
