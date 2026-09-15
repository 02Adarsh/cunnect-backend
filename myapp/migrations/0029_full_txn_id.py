from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0028_printorder_txn_last4"),
    ]

    operations = [
        migrations.AddField(
            model_name="printorder",
            name="txn_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="hostelorder",
            name="txn_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
