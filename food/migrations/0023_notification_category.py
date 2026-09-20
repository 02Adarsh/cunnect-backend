from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("food", "0022_coupon_offers_v50"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="category",
            field=models.CharField(
                choices=[("food", "Food"), ("print", "Printout"),
                         ("ride", "Ride"), ("general", "General")],
                default="food", max_length=20, db_index=True),
        ),
    ]
