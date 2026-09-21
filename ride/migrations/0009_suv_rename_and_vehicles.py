from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0008_ride_payment_confirmed"),
    ]

    operations = [
        # ⭐ v77: "Car XL" is called SUV now (the data is kept, only the
        # column names change).
        migrations.RenameField(
            model_name="ridevendor",
            old_name="xl_active",
            new_name="suv_active",
        ),
        migrations.RenameField(
            model_name="ridevendor",
            old_name="xl_base",
            new_name="suv_base",
        ),
        migrations.RenameField(
            model_name="ridevendor",
            old_name="xl_per_km",
            new_name="suv_per_km",
        ),
        # ⭐ the car the rider is driving for this ride (plate hidden from
        # the student until the payment is verified)
        migrations.AddField(
            model_name="ride",
            name="vehicle_name",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AddField(
            model_name="ride",
            name="vehicle_plate",
            field=models.CharField(blank=True, default="", max_length=24),
        ),
        # ⭐ the partner's saved vehicles (name + plate, per category)
        migrations.CreateModel(
            name="RideVehicle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False,
                                           verbose_name="ID")),
                ("vehicle_type", models.CharField(default="mini",
                                                  max_length=12)),
                ("name", models.CharField(blank=True, default="",
                                          max_length=80)),
                ("plate", models.CharField(blank=True, default="",
                                           max_length=24)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("rider", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="vehicles", to="ride.ridevendor")),
            ],
            options={
                "ordering": ["vehicle_type", "name"],
            },
        ),
    ]
