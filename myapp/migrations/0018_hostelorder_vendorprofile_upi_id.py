from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("myapp", "0017_printorder_bw_page_ranges_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendorprofile",
            name="upi_id",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AlterField(
            model_name="vendorprofile",
            name="vendor_type",
            field=models.CharField(
                choices=[
                    ("food", "Food Vendor"),
                    ("printout", "Printout Vendor"),
                    ("hostel", "Hostel Essentials Vendor"),
                ],
                default="food",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="HostelOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True,
                                           primary_key=True, serialize=False,
                                           verbose_name="ID")),
                ("order_no", models.CharField(max_length=24, unique=True)),
                ("orderer_uid", models.CharField(blank=True, default="",
                                                 max_length=60)),
                ("orderer_name", models.CharField(blank=True, default="",
                                                  max_length=120)),
                ("orderer_mobile", models.CharField(blank=True, default="",
                                                    max_length=20)),
                ("recipient_name", models.CharField(max_length=120)),
                ("recipient_mobile", models.CharField(max_length=20)),
                ("address", models.TextField(blank=True,
                                             default="Chandigarh University")),
                ("payment_ref", models.CharField(blank=True, default="",
                                                 max_length=120)),
                ("paid", models.BooleanField(default=False)),
                ("status", models.CharField(
                    choices=[
                        ("pending", "Pending"),
                        ("accepted", "Accepted"),
                        ("delivered", "Delivered"),
                        ("cancelled", "Cancelled"),
                    ],
                    default="pending", max_length=16)),
                ("total", models.DecimalField(decimal_places=2, default=1799,
                                              max_digits=8)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("student", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="hostel_orders",
                    to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
