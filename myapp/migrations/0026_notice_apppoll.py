from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("myapp", "0025_ordercounter"),
    ]

    operations = [
        migrations.CreateModel(
            name="Notice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=200)),
                ("message", models.TextField(blank=True, default="")),
                ("image", models.ImageField(blank=True, null=True, upload_to="notices/")),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="AppPoll",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("question", models.CharField(max_length=240)),
                ("image", models.ImageField(blank=True, null=True, upload_to="polls/")),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="AppPollOption",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("text", models.CharField(max_length=160)),
                ("image", models.ImageField(blank=True, null=True, upload_to="polls/options/")),
                ("poll", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="options", to="myapp.apppoll")),
            ],
        ),
        migrations.CreateModel(
            name="AppPollVote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("option", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="votes", to="myapp.apppolloption")),
                ("poll", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="votes", to="myapp.apppoll")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="app_poll_votes", to=settings.AUTH_USER_MODEL)),
            ],
            options={"unique_together": {("poll", "user")}},
        ),
    ]
