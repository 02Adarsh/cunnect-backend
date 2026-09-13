try:
    from .celery import app as celery_app

    __all__ = ["celery_app"]
except Exception:  # celery install na ho to bhi app chalegi
    celery_app = None
    __all__ = []
