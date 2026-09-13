import datetime
import time

from celery import shared_task


@shared_task(bind=True, ignore_result=True)
def order_alert_task(self, order_id, round_no=1):
    """Vendor ko repeat alert — Celery countdown se, web process free."""
    from api_app.views import _ORDER_ALERTS, _vendor_push
    from food.models import Order

    try:
        if order_id in _ORDER_ALERTS:
            return
        o = Order.objects.filter(id=order_id).first()
        if o is None or o.status != "pending" or o.vendor is None:
            return
        _vendor_push(o.vendor, "Order pending - alert",
                     f"{o.order_number} still waiting. Accept or reject now.")
        if round_no < 12:
            order_alert_task.apply_async(
                args=[order_id, round_no + 1], countdown=45)
    except Exception as exc:
        print("[CELERY-ALERT]", exc)


@shared_task(ignore_result=True)
def push_tokens_task(tokens, title, message, high=False):
    """FCM push background me — request turant wapas."""
    from api_app.views import _push_tokens

    _push_tokens(tokens, title, message, high=high, _direct=True)


@shared_task(ignore_result=True)
def ums_scrape_task(uid):
    """UMS dashboard background scrape — app ko cached data turant mila."""
    from api_app.views import (_UMS_STATE, _scrape_ums_dashboard,
                               _ums_auto_session)

    state = _UMS_STATE.get(uid) or _ums_auto_session(uid)
    if not state or not state.get("scraper"):
        return
    if time.time() - float(state.get("scraping_at") or 0) < 25:
        return
    state["scraping_at"] = time.time()
    _scrape_ums_dashboard(state["scraper"], state.get("cookies") or {},
                          state=state)


def _secs_to_next_3am():
    now = datetime.datetime.now()
    nxt = (now + datetime.timedelta(days=1)).replace(
        hour=3, minute=0, second=0, microsecond=0)
    if now.hour < 3:
        nxt = now.replace(hour=3, minute=0, second=0, microsecond=0)
    return max(60, int((nxt - now).total_seconds()))


@shared_task(ignore_result=True)
def daily_cleanup_task():
    """Roz 3 AM: stale device tokens saaf + khud ko reschedule."""
    try:
        from myapp.models import DeviceToken

        cutoff = datetime.datetime.now() - datetime.timedelta(days=90)
        n = DeviceToken.objects.filter(updated_at__lt=cutoff).delete()[0]
        print(f"[CLEANUP] removed {n} stale device tokens")
    except Exception as exc:
        print("[CLEANUP]", exc)
    daily_cleanup_task.apply_async(countdown=_secs_to_next_3am(),
                                   expires=3600)
