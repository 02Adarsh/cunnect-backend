from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("food", "0020_notification_audience"),
    ]

    operations = [
        migrations.AddField(
            model_name="fooditem",
            name="is_veg",
            field=models.BooleanField(default=True),
        ),
    ]
