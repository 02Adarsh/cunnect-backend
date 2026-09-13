import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

app = Celery("cunnect")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

_broker = os.getenv("REDIS_URL", "")
if _broker:
    app.conf.broker_url = _broker
    app.conf.broker_connection_retry_on_startup = True
app.conf.result_backend = None  # Redis ki memory bachao
app.conf.task_always_eager = not _broker  # bina Redis ke direct-run fallback
