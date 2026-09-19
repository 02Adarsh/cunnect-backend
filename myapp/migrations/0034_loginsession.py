from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("myapp", "0033_vendor_control_v61"),
    ]

    operations = [
        migrations.CreateModel(
            name="LoginSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("device_id", models.CharField(blank=True, default="",
                                               max_length=64)),
                ("token_key", models.CharField(blank=True, default="",
                                               max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="login_session",
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Login session",
                "verbose_name_plural": "Login sessions",
            },
        ),
    ]
