# ⭐ v85: StoreSection.is_locked — hub tiles / store cards show a lock
# and do nothing on tap. Admin toggles it from the section editor.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0036_hostel_out_for_delivery_v84"),
    ]

    operations = [
        migrations.AddField(
            model_name="storesection",
            name="is_locked",
            field=models.BooleanField(default=False),
        ),
    ]
