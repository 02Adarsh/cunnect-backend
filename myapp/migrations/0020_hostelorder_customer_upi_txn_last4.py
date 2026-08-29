from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0019_devicetoken"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostelorder",
            name="customer_upi",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="hostelorder",
            name="txn_last4",
            field=models.CharField(blank=True, default="", max_length=4),
        ),
    ]
