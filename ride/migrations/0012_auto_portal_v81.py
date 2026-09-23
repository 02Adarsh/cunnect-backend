from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0011_auto_partners"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ridevendor",
            name="auto_online",
            field=models.BooleanField(default=True),
        ),
        migrations.CreateModel(
            name="AutoCall",
            fields=[
                ("id", models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name="ID")),
                ("student_name", models.CharField(
                    blank=True, default="", max_length=120)),
                ("student_uid", models.CharField(
                    blank=True, default="", max_length=64)),
                ("student_phone", models.CharField(
                    blank=True, default="", max_length=15)),
                ("lat", models.FloatField(default=26.621884)),
                ("lng", models.FloatField(default=80.687916)),
                ("status", models.CharField(
                    choices=[("pending", "Pending"),
                             ("accepted", "Accepted"),
                             ("declined", "Declined"),
                             ("expired", "Expired")],
                    default="pending", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("responded_at", models.DateTimeField(
                    null=True, blank=True)),
                ("rider", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="auto_calls", to="ride.ridevendor")),
                ("student", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="auto_calls", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
