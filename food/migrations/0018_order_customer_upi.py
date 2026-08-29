from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("food", "0017_fooditem_stock"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="customer_upi",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="order",
            name="txn_last4",
            field=models.CharField(blank=True, default="", max_length=4),
        ),
    ]
