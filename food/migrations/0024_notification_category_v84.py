from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("food", "0023_notification_category"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="category",
            field=models.CharField(
                choices=[
                    ("food", "Food"),
                    ("print", "Printout"),
                    ("ride", "Ride"),
                    ("hostel", "Hostel"),
                    ("auto", "Auto"),
                    ("general", "General"),
                ],
                default="food",
                max_length=20,
            ),
        ),
    ]
