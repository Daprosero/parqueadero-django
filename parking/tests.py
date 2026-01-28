from datetime import datetime
from django.utils import timezone
from datetime import datetime, time, timedelta
import math

start = datetime(2026, 1, 25, 2, 23)  # naive a propósito
end   = datetime(2026, 1, 25, 13, 0)
def _localize(dt):
    tz = timezone.get_current_timezone()
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, tz)
    return timezone.localtime(dt, tz)


# ============================================================
# Segmentación de cobro:
#   - Día: 06:00 -> 18:00  (HOUR / hora-fracción)
#   - Noche: 18:00 -> 06:00 (NIGHT / noche-fracción)
#
# IMPORTANTE: Para el day_type de la NOCHE se usa la fecha
#             del inicio de la noche (18:00).
#             Ej: 2026-01-25 02:00 pertenece a la noche
#                 iniciada el 2026-01-24 18:00.
# ============================================================

DAY_START = time(6, 0)
NIGHT_START = time(18, 0)
def iter_pricing_segments(start_dt, end_dt):
    if not start_dt or not end_dt or end_dt <= start_dt:
        return

    s = _localize(start_dt)
    e = _localize(end_dt)

    cur = s
    tz = cur.tzinfo

    while cur < e:
        cur_t = cur.time()
        in_day = (cur_t >= DAY_START) and (cur_t < NIGHT_START)

        if in_day:
            billing_unit = "HOUR"
            anchor_date = cur.date()
            boundary = datetime.combine(cur.date(), NIGHT_START).replace(tzinfo=tz)
        else:
            billing_unit = "NIGHT"
            if cur_t >= NIGHT_START:
                anchor_date = cur.date()
                next_day = cur.date() + timedelta(days=1)
                boundary = datetime.combine(next_day, DAY_START).replace(tzinfo=tz)
            else:
                anchor_date = cur.date() - timedelta(days=1)
                boundary = datetime.combine(cur.date(), DAY_START).replace(tzinfo=tz)

        seg_end = min(boundary, e)
        if seg_end > cur:
            yield (cur, seg_end, billing_unit, anchor_date)
        cur = seg_end
list(iter_pricing_segments(start, end))