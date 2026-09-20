from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ride", "0007_sync_riderblock_help"),
    ]

    operations = [
        migrations.AddField(
            model_name="ride",
            name="payment_confirmed",
            field=models.BooleanField(default=False),
        ),
    ]
