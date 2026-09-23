"""v80: OTP verification for the printout and hostel store sections.

Every order now carries a 4-digit hand-over OTP. The vendor has to type
it in before the job can be completed (printout) or delivered (hostel
essentials) — exactly the rule the food section already follows.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0034_loginsession"),
    ]

    operations = [
        migrations.AddField(
            model_name="printorder",
            name="delivery_otp",
            field=models.CharField(blank=True, default="", max_length=4),
        ),
        migrations.AddField(
            model_name="printorder",
            name="otp_verified",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="printorder",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="hostelorder",
            name="delivery_otp",
            field=models.CharField(blank=True, default="", max_length=4),
        ),
        migrations.AddField(
            model_name="hostelorder",
            name="otp_verified",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="hostelorder",
            name="delivered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
