from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0023_vendorprofile_logo"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendorprofile",
            name="kitchen_open",
            field=models.BooleanField(default=True),
        ),
    ]
