from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("food", "0018_order_customer_upi"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="txn_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
