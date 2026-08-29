from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0021_vendorprofile_upi_qr_image"),
    ]

    operations = [
        migrations.CreateModel(
            name="UmsSaved",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uid", models.CharField(max_length=60, unique=True)),
                ("payload", models.JSONField(default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
