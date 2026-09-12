from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0024_vendorprofile_kitchen_open"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderCounter",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.IntegerField(default=0)),
            ],
        ),
    ]
