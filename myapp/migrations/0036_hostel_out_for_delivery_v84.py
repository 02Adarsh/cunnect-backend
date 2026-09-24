from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0035_store_otp_v80"),
    ]

    operations = [
        migrations.AlterField(
            model_name="hostelorder",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("accepted", "Accepted"),
                    ("out_for_delivery", "Out for delivery"),
                    ("delivered", "Delivered"),
                    ("cancelled", "Cancelled"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
    ]
