import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0004_ride_student_lat_ride_student_lng_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="ride",
            name="contact_phone",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="ride",
            name="booking_for_other",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="ride",
            name="other_name",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.CreateModel(
            name="RiderBlock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("kind", models.CharField(default="date", max_length=8)),
                ("weekday", models.IntegerField(default=0)),
                ("start_min", models.IntegerField(default=0)),
                ("end_min", models.IntegerField(default=1439)),
                ("date", models.DateField(blank=True, null=True)),
                ("label", models.CharField(blank=True, default="",
                                           max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("rider", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="blocks", to="ride.ridevendor")),
            ],
            options={
                "ordering": ["kind", "weekday", "date", "start_min"],
                "verbose_name": "Rider unavailability",
                "verbose_name_plural": "Rider unavailability slots",
            },
        ),
    ]
