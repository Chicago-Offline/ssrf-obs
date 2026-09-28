"""Validation ladder + promotion rules (NETWORK.md §4).

Tiers per observation:
  V0 heard    — energy-gated activity
  V1 decoded  — digital decode metadata present (gated)
  V2 voice    — voice confirmed on dwell audio
  V3 recorded — archived clip referenced
Promotion per channel:
  verified — >=V1 from >=MIN_STATIONS stations across >=MIN_DAYS distinct days
  flagged  — stations disagree on decode identity (e.g. color code)
  stale    — nothing heard anywhere for STALE_DAYS
"""
import json
import time

MIN_STATIONS = 2
MIN_DAYS = 3
STALE_DAYS = 90


def tier(gated, decoder, meta):
    if not gated:
        return None
    m = meta if isinstance(meta, dict) else json.loads(meta or "{}")
    if m.get("recording"):
        return 3
    if m.get("voice"):
        return 2
    if decoder in ("dmr", "p25", "nxdn", "dstar", "ysf") and (
            m.get("cc") is not None or m.get("nac") is not None or m.get("tgs")):
        return 1
    return 0


def channel_status(rows, now=None):
    """rows: (station_id, ts, freq_hz, snr_db, decoder, gated, meta) for ONE freq.
    Returns {level, status, last_heard, stations, days}."""
    now = now or time.time()
    best = None
    last = 0.0
    v1_station_days = set()
    ccs = {}
    for station, ts, _f, _snr, decoder, gated, meta in rows:
        t = tier(gated, decoder, meta)
        if t is None:
            continue
        best = t if best is None else max(best, t)
        last = max(last, ts or 0)
        m = json.loads(meta or "{}") if isinstance(meta, str) else (meta or {})
        if t >= 1:
            v1_station_days.add((station, int((ts or 0) // 86400)))
            if m.get("cc") is not None:
                ccs.setdefault(m["cc"], set()).add(station)
    if best is None:
        return None
    stations = {s for s, _d in v1_station_days}
    days = {d for _s, d in v1_station_days}
    if len(ccs) > 1:
        status = "flagged"
    elif len(stations) >= MIN_STATIONS and len(days) >= MIN_DAYS:
        status = "verified"
    elif now - last > STALE_DAYS * 86400:
        status = "stale"
    else:
        status = "observed"
    return {"level": f"V{best}", "status": status, "last_heard": last,
            "stations": len({s for s, *_ in
                             [(r[0],) for r in rows if r[5]]}),
            "v1_stations": len(stations), "v1_days": len(days)}
