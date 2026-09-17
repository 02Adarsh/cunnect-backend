# v60: hostel product catalogue + items on hostel orders.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0031_admin_v50'),
    ]

    operations = [
        migrations.AddField(
            model_name='hostelorder',
            name='items',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name='HostelProduct',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('mrp', models.DecimalField(decimal_places=2, default=0, max_digits=8)),
                ('description', models.TextField(blank=True, default='')),
                ('emoji', models.CharField(blank=True, default='🛒', max_length=8)),
                ('is_active', models.BooleanField(default=True)),
                ('order', models.IntegerField(default=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['order', 'id'],
            },
        ),
        migrations.CreateModel(
            name='HostelProductPhoto',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.ImageField(upload_to='hostel_products/')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='photos', to='myapp.hostelproduct')),
            ],
        ),
    ]
