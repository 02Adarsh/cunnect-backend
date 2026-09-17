# v61: vendor storefront description + hostel product stock.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0032_hostel_products_v60'),
    ]

    operations = [
        migrations.AddField(
            model_name='vendorprofile',
            name='store_description',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='hostelproduct',
            name='stock',
            field=models.IntegerField(default=0),
        ),
    ]
