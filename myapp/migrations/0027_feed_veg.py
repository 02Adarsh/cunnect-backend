from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("myapp", "0026_notice_apppoll"),
    ]

    operations = [
        migrations.CreateModel(
            name="FeedReaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("notice", "Notice"), ("poll", "Poll")], max_length=10)),
                ("object_id", models.IntegerField()),
                ("emoji", models.CharField(max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="feed_reactions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "unique_together": {("kind", "object_id", "user")},
                "indexes": [models.Index(fields=["kind", "object_id"], name="myapp_feedr_kind_obj_idx")],
            },
        ),
        migrations.CreateModel(
            name="FeedComment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("notice", "Notice"), ("poll", "Poll")], max_length=10)),
                ("object_id", models.IntegerField()),
                ("text", models.CharField(max_length=600)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="feed_comments", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["created_at"],
                "indexes": [models.Index(fields=["kind", "object_id"], name="myapp_feedc_kind_obj_idx")],
            },
        ),
    ]
