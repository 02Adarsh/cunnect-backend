import datetime
import os
import time

from celery import shared_task

_redis_state = {"checked": False, "ok": False}


def redis_ok():
    """Ek hi baar Redis probe — broken URL pe requests kabhi nahi atkenge."""
    if not _redis_state["checked"]:
        try:
            import redis as _r

            c = _r.Redis.from_url(os.getenv("REDIS_URL", ""),
                                  socket_connect_timeout=4, socket_timeout=4)
            c.ping()
            _redis_state["ok"] = True
            print("[CELERY] redis connected OK")
        except Exception as exc:
            _redis_state["ok"] = False
            print("[CELERY] redis unavailable — direct mode:", exc)
        _redis_state["checked"] = True
    return _redis_state["ok"]


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
def push_tokens_task(tokens, title, message, high=False, route=None,
                     data=None):
    """FCM push background me — request turant wapas."""
    from api_app.views import _push_tokens

    _push_tokens(tokens, title, message, high=high, _direct=True,
                 route=route, data=data)


@shared_task(ignore_result=True)
def ums_scrape_task(uid):
    """UMS dashboard background scrape — cache update + attendance push."""
    from api_app.views import (_UMS_STATE, _scrape_ums_dashboard,
                               _ums_attendance_notify, _ums_auto_session)

    state = _UMS_STATE.get(uid) or _ums_auto_session(uid)
    if not state or not state.get("scraper"):
        return
    if time.time() - float(state.get("scraping_at") or 0) < 25:
        return
    state["scraping_at"] = time.time()
    dashboard = _scrape_ums_dashboard(state["scraper"],
                                      state.get("cookies") or {}, state=state)
    if state.get("last_scrape_ok") and isinstance(dashboard, dict):
        state["dashboard"] = dashboard
        state["dashboard_at"] = time.time()
        _ums_attendance_notify(uid, dashboard)


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
