import os
import threading

from django.apps import AppConfig


class ApiAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api_app"

    def ready(self):
        # ⭐ FREE tier: Celery worker web process ke andar hi chalta hai
        if os.getenv("RUN_CELERY_WORKER") == "1" and os.getenv("REDIS_URL"):
            threading.Thread(target=_run_embedded_worker, daemon=True).start()


def _run_embedded_worker():
    import time as _t

    _t.sleep(2)
    try:
        from myproject.celery import app

        from api_app.tasks import daily_cleanup_task
        daily_cleanup_task.apply_async(countdown=10, expires=3600)

        print("[CELERY-EMBED] worker starting (threads pool x4)")
        worker = app.Worker(concurrency=4, pool="threads", loglevel="INFO")
        worker.start()
    except Exception as exc:
        print("[CELERY-EMBED] failed:", exc)
