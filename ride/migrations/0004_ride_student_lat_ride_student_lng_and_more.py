from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0003_ride_rider_lat_ride_rider_lng_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="ride",
            name="student_lat",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ride",
            name="student_lng",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ride",
            name="student_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ride",
            name="share_location",
            field=models.BooleanField(default=False),
        ),
    ]
