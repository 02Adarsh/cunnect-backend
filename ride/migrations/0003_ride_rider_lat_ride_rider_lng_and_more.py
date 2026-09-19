from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0002_riderejection"),
    ]

    operations = [
        migrations.AddField(
            model_name="ride",
            name="rider_lat",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ride",
            name="rider_lng",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ride",
            name="rider_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
