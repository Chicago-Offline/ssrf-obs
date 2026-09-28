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

# ---------------------------------------------------------------------
# Monitor-check policy (NETWORK.md S4, approved 2026-09-28).
#
# This is a SEPARATE grading axis from channel_status() above and the two
# must not be conflated. channel_status() asks "what did we manage to hear
# and decode" over observations, and returns None when nothing ever
# gated -- so a channel no one has ever heard simply vanishes from it.
# monitor_rollup() asks the catalog question instead: "we said this
# repeater exists, does it?", and its whole value is that silence is a
# reportable answer. STALE_DAYS (90d) stays the conservative gate for
# ssrf-lite writeback; the thresholds below drive directory display.
MONITOR_ACTIVE_S = 7 * 86400     # heard within a week -> active
MONITOR_STALE_S = 30 * 86400     # heard within a month -> stale
# "Never heard" is a claim about the world, not about our effort, so it
# needs enough effort behind it to be publishable: a channel checked twice
# on one quiet afternoon is not evidence of a dead repeater. Below this
# bar the honest answer is "watching", not "never heard".
NEVER_MIN_CHECKS = 50
NEVER_MIN_SPAN_S = 7 * 86400
# A parameter needs two hearings to graduate from measured to verified.
# Two hearings at ONE station is allowed to reach verified (Eric, 2026-09-28);
# corroborated is the stronger, separate claim of >=2 distinct observers.
HEARINGS_FOR_VERIFIED = 2


DIGITAL_DECODERS = ("dmr", "p25", "nxdn", "dstar", "ysf")


def has_decode_identity(m):
    """Did a digital decode actually recover identifying metadata?

    Two vocabularies reach this DB and both are legitimate. NETWORK.md S4
    describes the concept as "CC/TG/NAC/radio IDs" without pinning key names,
    so accept the compact spelling AND the one rf-survey's dwell_dmr() really
    emits (color_codes / talkgroups / radio_ids, passed through verbatim by
    submit.py). Reading only the compact form silently graded every successful
    DMR decode as V0, which is why nothing could ever reach "verified".

    cc/nac use "is not None" because color code 0 and NAC 0 are valid.

    sync_lines is deliberately NOT accepted: dsd-family decoders fabricate
    sync on pure noise (rf-survey dwell.py says so in its module docstring),
    so a sync count is not identity. dwell_dmr's own "active" flag makes the
    same call.
    """
    if m.get("cc") is not None or m.get("nac") is not None or m.get("tgs"):
        return True
    return bool(m.get("color_codes") or m.get("talkgroups")
                or m.get("radio_ids"))


def tier(gated, decoder, meta):
    if not gated:
        return None
    m = meta if isinstance(meta, dict) else json.loads(meta or "{}")
    if m.get("recording"):
        return 3
    if m.get("voice"):
        return 2
    if decoder in DIGITAL_DECODERS and has_decode_identity(m):
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


def _observer(station_id, receiver):
    """Observer identity across the network.

    Receiver names are station-local and collide freely -- every host has
    an "rtl:0" -- so bare receiver would invent corroboration between two
    dongles that are really one. Always pair it with the station.
    """
    return "%s/%s" % (station_id, receiver)


def param_merge(rows):
    """Network-wide per-parameter verification state.

    Mirrors rf-survey Store.param_state() but merges across stations. The
    asymmetries are load-bearing and are kept identical on purpose:

      * conflict is STICKY and always wins. One station hearing 141.3
        where the catalog claims 107.2 is a real finding; later agreement
        elsewhere does not erase it, it just means coverage differs.
      * silence NEVER downgrades. A param already verified stays verified
        through any number of silent checks -- absence of a hearing is not
        evidence against a measurement.
      * suspect never promotes. 120/180/240 Hz look like CTCSS tones but
        are almost certainly mains hum, so they must not reach verified.
    """
    out = {}
    for station_id, receiver, ts, _t, _s, _f, _d, _heard, _snr, params, _m \
            in rows:
        graded = params if isinstance(params, dict) else json.loads(
            params or "{}")
        if not graded:
            continue          # silent or nothing claimed: grades nothing
        obs = _observer(station_id, receiver)
        for key, verdict in graded.items():
            state = (verdict or {}).get("state") if isinstance(
                verdict, dict) else verdict
            observed = (verdict or {}).get("observed") if isinstance(
                verdict, dict) else None
            e = out.setdefault(key, {
                "state": "unverified", "observed": None, "hits": 0,
                "conflicts": 0, "observers": set(), "last_ts": 0.0,
                "note": None})
            if state == "conflict":
                e["conflicts"] += 1
                e["state"] = "conflict"
                e["observed"] = observed
                e["observers"].add(obs)
                e["last_ts"] = max(e["last_ts"], ts or 0)
                continue
            if e["state"] == "conflict":
                continue      # sticky: nothing later clears it
            if state == "suspect":
                e["state"] = "suspect"
                e["observed"] = observed
                e["note"] = (verdict or {}).get("note")
                e["observers"].add(obs)
                e["last_ts"] = max(e["last_ts"], ts or 0)
                continue
            if state in ("verified", "measured"):
                e["hits"] += 1
                e["observed"] = observed
                e["observers"].add(obs)
                e["last_ts"] = max(e["last_ts"], ts or 0)
                if e["state"] != "suspect":
                    e["state"] = ("verified"
                                  if e["hits"] >= HEARINGS_FOR_VERIFIED
                                  else "measured")
            elif state == "no_claim" and e["hits"] == 0:
                e["state"] = "no_claim"
                e["note"] = (verdict or {}).get("note")
    for e in out.values():
        e["corroborated"] = len(e["observers"]) >= MIN_STATIONS
        e["observers"] = sorted(e["observers"])
    return out


def monitor_rollup(rows, now=None):
    """Roll monitor checks for ONE (freq, target) up across all stations.

    rows: db.monitor_check_rows() tuples, silent checks INCLUDED.

    Returns the answers the directory needs: last_heard, last_checked,
    how often it is active, which observers heard it, and a status.

    Status is deliberately only the check-derived half of the ladder:
      active      heard within MONITOR_ACTIVE_S
      stale       heard within MONITOR_STALE_S
      dormant     heard, but longer ago than that
      never_heard checked hard enough to mean it, and always silent
      watching    silent, but not yet checked hard enough to claim more
    out_of_range and no_decoder are NOT decided here: both are catalog
    facts (where the site is, what mode it uses) and nothing in a check
    can establish them. They are layered on at the ssrf-lite join, which
    is also why a channel with no decoder mapping never reaches this
    function at all -- gen_targets skips it, so it is absent rather than
    slandered as never_heard.
    """
    now = now or time.time()
    if not rows:
        return None
    checks = len(rows)
    heard_rows = [r for r in rows if r[7]]
    ts_all = [r[2] or 0 for r in rows]
    first_checked, last_checked = min(ts_all), max(ts_all)
    observers_all = sorted({_observer(r[0], r[1]) for r in rows})
    observers_heard = sorted({_observer(r[0], r[1]) for r in heard_rows})
    last_heard = max((r[2] or 0 for r in heard_rows), default=0.0)
    span = last_checked - first_checked
    if heard_rows:
        age = now - last_heard
        if age <= MONITOR_ACTIVE_S:
            status = "active"
        elif age <= MONITOR_STALE_S:
            status = "stale"
        else:
            status = "dormant"
    elif checks >= NEVER_MIN_CHECKS and span >= NEVER_MIN_SPAN_S:
        status = "never_heard"
    else:
        status = "watching"
    return {
        "target": rows[-1][3],
        "ssrf_id": rows[-1][4],
        "freq_hz": rows[-1][5],
        "decoder": rows[-1][6],
        "status": status,
        "checks": checks,
        "hearings": len(heard_rows),
        # Hit rate over checks actually performed, not over wall time: it
        # answers "when we listen, how often is it there", which is the
        # question a scan list cares about.
        "hit_rate": round(len(heard_rows) / checks, 4) if checks else 0.0,
        "last_heard": last_heard or None,
        "last_checked": last_checked or None,
        "first_checked": first_checked or None,
        "span_s": span,
        "observers": observers_all,
        "observers_heard": observers_heard,
        "corroborated": len(observers_heard) >= MIN_STATIONS,
        "params": param_merge(rows),
    }
