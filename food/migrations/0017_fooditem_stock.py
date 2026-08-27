from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("food", "0016_foodoffer"),
    ]

    operations = [
        migrations.AddField(
            model_name="fooditem",
            name="stock",
            field=models.IntegerField(default=0),
        ),
    ]
