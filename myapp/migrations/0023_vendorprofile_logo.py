from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0022_umssaved"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendorprofile",
            name="logo",
            field=models.ImageField(blank=True, null=True, upload_to="vendor_logo/"),
        ),
    ]
