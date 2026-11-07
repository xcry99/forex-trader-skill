"""Time for a 24/5 market.

Three clocks matter:

* **UTC** - what the Python API and this module use internally.
* **New York** - the clock the sessions are written in (London opens at 03:00
  ET, New York at 08:00 ET), so the DST rules are the same ones the original
  stock skills used.
* **Broker server time** - MetaTrader 5 stamps every bar and tick with the
  *server's* wall clock (usually EET: UTC+2 in winter, UTC+3 in summer). A bar
  stamped 23:57 is 21:57 UTC, not 23:57 UTC. :func:`bar_utc` undoes that.

Nothing here imports MetaTrader5.
"""

import datetime

# ------------------------------------------------------------------ UTC / NY


def utc_now():
    """Naive UTC, second precision."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def _last_sunday(year, month):
    d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1) if month < 12 else datetime.date(year, 12, 31)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def ny_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 02:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 02:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_ny(utc):
    """Naive UTC -> naive New York."""
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=ny_offset(utc))


def ny_to_utc(local):
    """Naive New York -> naive UTC."""
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if ny_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def parse_now(text, default=None):
    """ISO time -> naive UTC. A time without an offset is taken as New York.

    ``--now "2026-10-19 10:15"`` means 10:15 in New York, which is what a
    trader means. Append ``Z`` or ``+00:00`` for UTC.
    """
    if not text:
        return default or utc_now()
    t = str(text).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ValueError('--now wants a time like "2026-10-19 10:15" (New York) or ...Z (UTC)')
    if dt.tzinfo is None:
        return ny_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def iso(utc):
    return utc.replace(microsecond=0).isoformat() + "Z" if utc else None


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


# ------------------------------------------------------------------ broker clock

def server_offset_hours(cfg=None, now=None):
    """Broker server offset from UTC, in hours.

    ``mt5.server_utc_offset`` wins when set. Otherwise assume the usual
    MetaTrader EET rule: UTC+3 from the last Sunday of March to the last
    Sunday of October, UTC+2 the rest of the year. Axi's demo terminal
    follows it.
    """
    now = now or utc_now()
    ov = ((cfg or {}).get("mt5") or {}).get("server_utc_offset")
    if ov is not None:
        try:
            return float(ov)
        except (TypeError, ValueError):
            pass
    y = now.year
    start = datetime.datetime(y, 3, _last_sunday(y, 3).day, 1, 0)
    end = datetime.datetime(y, 10, _last_sunday(y, 10).day, 1, 0)
    return 3.0 if start <= now < end else 2.0


def bar_utc(epoch, offset_hours):
    """A MetaTrader bar/tick stamp (server wall clock as an epoch) -> naive UTC."""
    stamp = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
    return stamp.replace(tzinfo=None) - datetime.timedelta(hours=offset_hours)


def utc_epoch(utc, offset_hours):
    """Naive UTC -> the epoch MetaTrader expects for the same server instant."""
    return int(utc.replace(tzinfo=datetime.timezone.utc).timestamp() + offset_hours * 3600)


def detect_server_offset(gateway, cfg=None, now=None):
    """Best-effort live detection: compare the newest tick with the wall clock.

    Only trustworthy while the market is open; otherwise the EET rule is used.
    """
    now = now or utc_now()
    ov = ((cfg or {}).get("mt5") or {}).get("server_utc_offset")
    if ov is not None:
        return float(ov)
    try:
        tick = gateway.tick("EURUSD")
        t = tick.get("time_epoch")
        if t:
            delta = int(t) - int(now.replace(tzinfo=datetime.timezone.utc).timestamp())
            hours = round(delta / 3600.0)
            if 0 <= hours <= 4 and abs(delta - hours * 3600) < 1800:
                return float(hours)
    except Exception:
        pass
    return server_offset_hours(cfg, now)


# ------------------------------------------------------------------ market calendar

# Full-day FX closures. Good Friday, Christmas and New Year are the ones that
# actually close the interbank market; the rest of the year is 24/5.
FX_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-04-03": "Good Friday",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-03-26": "Good Friday",
    "2027-12-24": "Christmas Day (observed)",
    "2027-12-27": "Christmas Day (observed)",
}


def holiday(d):
    return FX_HOLIDAYS.get(d.isoformat())


def market_open(now_utc=None):
    """(is_open, reason). Forex runs Sunday 17:00 ET to Friday 17:00 ET."""
    now_utc = now_utc or utc_now()
    ny = to_ny(now_utc)
    d, t = ny.date(), ny.time()
    name = holiday(d)
    if name:
        return False, "FX holiday: " + name
    if d.weekday() == 5:                       # Saturday
        return False, "weekend"
    if d.weekday() == 6 and t < datetime.time(17, 0):   # Sunday before the open
        return False, "weekend (opens Sunday 17:00 ET)"
    if d.weekday() == 4 and t >= datetime.time(17, 0):  # Friday after the close
        return False, "weekend (closed Friday 17:00 ET)"
    return True, ""


# ------------------------------------------------------------------ sessions

# name -> (start hour, start minute, end hour, end minute) in New York time.
# A session whose end is <= its start crosses midnight.
SESSIONS = {
    "sydney":   (17, 0, 2, 0),
    "tokyo":    (19, 0, 4, 0),
    "asia":     (19, 0, 4, 0),
    "london":   (3, 0, 12, 0),
    "overlap":  (8, 0, 12, 0),
    "newyork":  (8, 0, 17, 0),
    "ny":       (8, 0, 17, 0),
}
SESSION_ORDER = ("asia", "london", "overlap", "newyork")


def session_bounds(name):
    key = str(name or "").strip().lower()
    if key not in SESSIONS:
        raise ValueError("unknown session %r (pick one of %s)" % (name, ", ".join(SESSION_ORDER)))
    h1, m1, h2, m2 = SESSIONS[key]
    return datetime.time(h1, m1), datetime.time(h2, m2)


def session_window(day, name):
    """NY date of the session's *start* -> (start_utc, end_utc)."""
    start_t, end_t = session_bounds(name)
    start = datetime.datetime.combine(day, start_t)
    end = datetime.datetime.combine(day, end_t)
    if end <= start:
        end += datetime.timedelta(days=1)
    return ny_to_utc(start), ny_to_utc(end)


def session_day(now_utc, name):
    """Which session instance is running (or most recently ran) at ``now_utc``."""
    ny = to_ny(now_utc)
    for back in (0, 1):
        day = ny.date() - datetime.timedelta(days=back)
        s, e = session_window(day, name)
        if s <= now_utc < e:
            return day
    # Not inside the session: use today's instance if it has not started yet,
    # else yesterday's completed one.
    day = ny.date()
    s, e = session_window(day, name)
    return day if now_utc < s else day


def current_sessions(now_utc=None):
    """Names of the sessions open right now."""
    now_utc = now_utc or utc_now()
    out = []
    for name in SESSION_ORDER:
        if name == "overlap":
            continue
        day = session_day(now_utc, name)
        s, e = session_window(day, name)
        if s <= now_utc < e:
            out.append(name)
    return out


def session_open_utc(day, name):
    return session_window(day, name)[0]


def session_close_utc(day, name):
    return session_window(day, name)[1]


def flat_by_utc(day, flat_by, name="newyork"):
    """NY wall-clock 'HH:MM' on the session's NY day -> naive UTC."""
    h, m = [int(x) for x in str(flat_by).split(":")]
    end = datetime.datetime.combine(day, datetime.time(h, m))
    return ny_to_utc(end)


def clock_label(utc):
    return to_ny(utc).strftime("%H:%M")


def day_label(d):
    return d.strftime("%a %d %b %Y")
