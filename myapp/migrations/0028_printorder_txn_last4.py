from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0027_feed_veg"),
    ]

    operations = [
        migrations.AddField(
            model_name="printorder",
            name="txn_last4",
            field=models.CharField(blank=True, default="", max_length=4),
        ),
    ]
