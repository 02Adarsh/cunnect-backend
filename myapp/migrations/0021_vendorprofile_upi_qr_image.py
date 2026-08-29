from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0020_hostelorder_customer_upi_txn_last4"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendorprofile",
            name="upi_qr_image",
            field=models.ImageField(blank=True, null=True, upload_to="vendor_qr/"),
        ),
    ]
