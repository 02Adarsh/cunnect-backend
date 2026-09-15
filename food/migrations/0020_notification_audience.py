from django.db import migrations, models


def mark_old_vendor_rows(apps, schema_editor):
    """Give older vendor notifications the vendor audience,
    so they no longer show in the student section."""
    Notification = apps.get_model("food", "Notification")
    vendor_titles = [
        "New food order received",
        "New order received",
        "Order pending - alert",
        "Delivery started",
    ]
    Notification.objects.filter(title__in=vendor_titles).update(
        audience="vendor"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("food", "0019_order_txn_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="audience",
            field=models.CharField(
                choices=[("student", "Student"), ("vendor", "Vendor")],
                db_index=True,
                default="student",
                max_length=10,
            ),
        ),
        migrations.RunPython(mark_old_vendor_rows, migrations.RunPython.noop),
    ]
