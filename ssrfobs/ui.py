"""Rendered UI for rffeed.chicagooffline.com.

Design brief (Eric, 2026-09-28): the first screen answers "what are we
hearing right now", and freshness is the dominant visual property. Three
counts, then repeater rows -- frequency, callsign and last-heard are what
the eye wants, everything else is secondary.

"Last checked" is deliberately NOT given equal weight. It is a global
monitoring-health indicator, and surfaces on an individual repeater only
when that repeater's monitoring is broken.

Plain string concatenation throughout, on purpose: the older PAGE template
was a str.format() template, which forces every literal CSS brace to be
doubled. That trap has cost a retry on this codebase before.

This module takes the web module as an explicit `web=` argument rather than
importing it, to keep the dependency one-way.
"""

import html
import time

NAV = (("Activity", "/"), ("Repeaters", "/repeaters"),
       ("Investigate", "/investigate"), ("Network", "/network"))

ACTIVE_RECENT_S = 3600          # "active recently" -- Eric: 1 hour
SPARK_BUCKETS = 12              # Eric: 12 buckets, 1 per hour
SPARK_BUCKET_S = 3600
HEALTHY_CHECK_S = 300           # "checked within 5 min"
STALE_CHECK_S = 3600            # per-row warning above this
RECENT_DAYS = 7

BLOCKS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

CYAN = "#00E5FF"
AMBER = "#FFB300"
GREEN = "#39FF14"

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0b0e11;color:#e8edf2;
 font:15px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:CYAN;text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:780px;margin:0 auto;padding:28px 20px 64px}
h1{margin:0;font-size:26px;letter-spacing:-.01em}
.sub{margin:4px 0 0;color:#8a97a5;font-size:14px}
nav{margin:22px 0 0;padding-bottom:10px;border-bottom:1px solid #1e252d;
 font-size:14px}
nav a{margin-right:18px;color:#8a97a5}
nav a.on{color:#e8edf2;font-weight:600}
.counts{margin:22px 0 6px;font-size:15px;color:#c8d2dc}
.counts b{color:#fff;font-weight:650}
.health{margin:0 0 20px;font-size:13px;color:#8a97a5}
.rows{display:flex;flex-direction:column;gap:2px}
.row{padding:13px 14px;border-radius:8px;background:#12171d;
 border:1px solid #1a212a}
.row:hover{border-color:#26313c}
.r1{font-size:17px;font-weight:600;letter-spacing:-.01em}
.r1 .call{color:#8a97a5;font-weight:400}
.fresh{margin-top:3px;font-size:15px;font-weight:600}
.meta{margin-top:3px;color:#8a97a5;font-size:13px}
.spark{margin-top:5px;font-family:ui-monospace,SFMono-Regular,Menlo,
 monospace;font-size:15px;line-height:1;color:CYAN;letter-spacing:1px}
.warn{margin-top:6px;font-size:12.5px;color:AMBER}
.empty{padding:22px 0;color:#8a97a5}
.unk{color:#5f6b78}
.hint{color:#8a97a5;font-size:12px}
.b{display:inline-block;padding:1px 7px;border-radius:4px;color:#06090c;
 font-size:11px;font-weight:700;letter-spacing:.04em}
.note{margin:18px 0;padding:14px;border-radius:8px;background:#12171d;
 border:1px solid #1a212a;color:#c8d2dc;font-size:14px}
h2{margin:28px 0 10px;font-size:13px;color:#8a97a5;font-weight:600;
 text-transform:uppercase;letter-spacing:.06em}
table.s{width:100%;border-collapse:collapse;font-size:13px}
table.s th{text-align:left;padding:7px 9px;border-bottom:1px solid #26313c;
 color:#8a97a5;font-weight:600;cursor:pointer;white-space:nowrap}
table.s td{padding:7px 9px;border-bottom:1px solid #161c23;
 vertical-align:top}
table.s tr:hover td{background:#12171d}
footer{margin-top:40px;padding-top:14px;border-top:1px solid #1e252d;
 color:#5f6b78;font-size:12px}
footer a{color:#5f6b78;margin-right:12px}
""".replace("CYAN", CYAN).replace("AMBER", AMBER)

DASH = '<span class="unk">&mdash;</span>'


def _esc(v):
    return html.escape("" if v is None else str(v))


def _mhz(freq_hz):
    return "-" if not freq_hz else "%.4f" % (freq_hz / 1e6)


# ------------------------------------------------------------- freshness

def fresh_text(ts, now=None):
    """Relative while recent, clock time once it crosses a day boundary.

    Eric's mock switches format at the day boundary, which is the honest
    break: "31h ago" makes the reader do arithmetic, "Yesterday, 8:41 PM"
    does not.
    """
    if not ts:
        return "never heard"
    now = now or time.time()
    d = max(0, now - ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return "%d min ago" % int(d // 60)
    if d < 12 * 3600:
        h = int(d // 3600)
        return "%d hour%s ago" % (h, "" if h == 1 else "s")
    lt, nt = time.localtime(ts), time.localtime(now)
    clock = time.strftime("%I:%M %p", lt).lstrip("0")
    yd = time.localtime(now - 86400)
    if (lt.tm_year, lt.tm_yday) == (nt.tm_year, nt.tm_yday):
        return "Today, %s" % clock
    if (lt.tm_year, lt.tm_yday) == (yd.tm_year, yd.tm_yday):
        return "Yesterday, %s" % clock
    if d < 7 * 86400:
        return time.strftime("%A, ", lt) + clock
    return time.strftime("%b ", lt) + str(lt.tm_mday) + ", " + clock


def fresh_mark(ts, now=None):
    """(glyph, colour) -- the dominant visual property on the page."""
    if not ts:
        return "\u25cb", "#5f6b78"
    d = max(0, (now or time.time()) - ts)
    if d <= ACTIVE_RECENT_S:
        return "\u25cf", GREEN
    if d <= 6 * 3600:
        return "\u25cf", CYAN
    if d <= 36 * 3600:
        return "\u25d0", AMBER
    return "\u25cb", "#5f6b78"


# ------------------------------------------------------------- sparkline

def sparks(db, freqs, buckets=SPARK_BUCKETS, bucket_s=SPARK_BUCKET_S,
           now=None):
    """{freq_hz: blocks} of gated hits per hour, oldest bucket on the left.

    One query for every row on the page. Scaled per row against its own
    peak: this shows when a repeater was busy, not how it ranks against a
    machine on another band with a different duty cycle. A row with no
    hits in the window is omitted, so the caller draws no sparkline at all
    rather than a flat line that could be misread as measured silence.
    """
    freqs = sorted({int(f) for f in freqs if f})
    if not freqs:
        return {}
    now = now or time.time()
    since = now - buckets * bucket_s
    marks = ",".join("?" * len(freqs))
    # Activity has two independent evidence streams and a repeater can be
    # carried by either: gated sweep observations, and monitor checks that
    # actually heard the target. Reading only `observations` would leave
    # every monitor-sourced repeater with no sparkline at all.
    queries = (
        "SELECT freq_hz, CAST((? - ts) / ? AS INTEGER) AS b, COUNT(*)"
        " FROM observations WHERE gated=1 AND ts>=? AND freq_hz IN (%s)"
        " GROUP BY freq_hz, b" % marks,
        "SELECT freq_hz, CAST((? - ts) / ? AS INTEGER) AS b, COUNT(*)"
        " FROM monitor_checks WHERE heard=1 AND ts>=? AND freq_hz IN (%s)"
        " GROUP BY freq_hz, b" % marks,
    )
    grid = {}
    args = [now, bucket_s, since] + freqs
    for q in queries:
        for freq_hz, b, n in db.db.execute(q, args):
            if b is None or b < 0 or b >= buckets:
                continue
            row = grid.setdefault(int(freq_hz), [0] * buckets)
            row[buckets - 1 - int(b)] += n
    out = {}
    top = len(BLOCKS) - 1
    for freq_hz, counts in grid.items():
        peak = max(counts)
        if not peak:
            continue
        out[freq_hz] = "".join(
            BLOCKS[0] if not c else BLOCKS[max(1, int(round(c / peak * top)))]
            for c in counts)
    return out


# ---------------------------------------------------------------- health

def health(monitors_map, now=None):
    """Global monitoring health. This is what replaces per-row last-checked."""
    now = now or time.time()
    rows = list((monitors_map or {}).values())
    total = len(rows)
    ok = sum(1 for m in rows
             if m.get("last_checked")
             and now - m["last_checked"] <= HEALTHY_CHECK_S)
    if not total:
        return "\u26a0", AMBER, "No monitored repeaters configured"
    if ok == total:
        return ("\u25cf", GREEN,
                "Monitoring healthy \u00b7 %d/%d repeaters checked within"
                " 5 min" % (ok, total))
    return ("\u26a0", AMBER,
            "Monitoring degraded \u00b7 %d/%d repeaters checked within"
            " 5 min" % (ok, total))


def stale_note(last_checked, now=None):
    """Per-repeater last-checked -- ONLY when something is wrong."""
    now = now or time.time()
    if not last_checked:
        return "\u26a0 Monitoring stale \u2014 never checked"
    d = now - last_checked
    if d <= STALE_CHECK_S:
        return None
    if d < 86400:
        when = "%dh ago" % int(d // 3600)
    else:
        when = "%dd ago" % int(d // 86400)
    return "\u26a0 Monitoring stale \u2014 last checked %s" % when


# ----------------------------------------------------------------- shell

def shell(active, body, title="Chicago Repeaters",
          sub="What we\u2019re hearing from local amateur radio",
          script=""):
    nav = "".join(
        '<a class="%s" href="%s">%s</a>'
        % ("on" if label == active else "", href, label)
        for label, href in NAV)
    feeds = ("active", "feed", "repeaters", "monitors", "channels",
             "stations", "receivers", "beacons", "names")
    links = "".join('<a href="/%s.json">%s</a>' % (f, f) for f in feeds)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,'
        'initial-scale=1">'
        "<title>" + _esc(title) + " \u00b7 Chicago Offline</title>"
        "<style>" + CSS + "</style></head><body><div class=\"wrap\">"
        "<h1>" + _esc(title) + "</h1>"
        '<p class="sub">' + _esc(sub) + "</p>"
        "<nav>" + nav + "</nav>"
        + body +
        "<footer>" + links +
        '<div style="margin-top:8px">generated '
        + _esc(time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()))
        + "</div></footer></div>" + script + "</body></html>")


# -------------------------------------------------------------- activity

def _row_html(r, spark, stale):
    freq = _mhz(r.get("freq_hz"))
    call = r.get("callsign") or r.get("name") or "unidentified"
    glyph, colour = fresh_mark(r.get("last_heard"))
    bits = [b for b in (r.get("mode"), r.get("tone"),
                        r.get("locality")) if b]
    head = _esc(freq) + ' <span class="call">\u00b7 ' + _esc(call) + "</span>"
    link = r.get("link")
    if link:
        head = '<a href="' + html.escape(link, quote=True) + '">' + head \
               + "</a>"
    out = ['<div class="row"><div class="r1">' + head + "</div>"]
    out.append('<div class="fresh" style="color:' + colour + '">'
               + glyph + " " + _esc(fresh_text(r.get("last_heard")))
               + "</div>")
    if bits:
        out.append('<div class="meta">'
                   + _esc(" \u00b7 ".join(str(b) for b in bits)) + "</div>")
    if spark:
        out.append('<div class="spark" title="hits per hour, last 12 h">'
                   + spark + "</div>")
    if stale:
        out.append('<div class="warn">' + _esc(stale) + "</div>")
    out.append("</div>")
    return "".join(out)


def activity(db, index=None, web=None):
    """Default screen: three counts, then freshness-first repeater rows."""
    now = time.time()
    rows = web.repeaters(db, index)
    mons = web.monitors(db, index)

    recent = sum(1 for r in rows
                 if r.get("last_heard")
                 and now - r["last_heard"] <= ACTIVE_RECENT_S)
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    today = sum(1 for r in rows
                if r.get("last_heard") and r["last_heard"] >= midnight)

    # last_checked lives on the monitor rollup; fold to the worst case per
    # frequency so a row warns if any of its targets went unchecked.
    checked = {}
    for m in mons.values():
        f, lc = m.get("freq_hz"), m.get("last_checked")
        if f and lc:
            checked[f] = max(checked.get(f, 0), lc)

    spark_map = sparks(db, [r.get("freq_hz") for r in rows], now=now)
    glyph, colour, text = health(mons, now=now)

    body = ['<p class="counts"><b>' + str(recent) + "</b> active recently"
            " \u00b7 <b>" + str(today) + "</b> heard today"
            " \u00b7 <b>" + str(len(mons)) + "</b> monitored</p>"]
    body.append('<p class="health"><span style="color:' + colour + '">'
                + glyph + "</span> " + _esc(text) + "</p>")
    if not rows:
        body.append('<p class="empty">no amateur or GMRS repeaters heard'
                    " in the last %d days</p>" % RECENT_DAYS)
    else:
        body.append('<div class="rows">')
        for r in rows:
            f = r.get("freq_hz")
            note = (stale_note(checked.get(f), now=now)
                    if r.get("source") == "monitor" else None)
            body.append(_row_html(r, spark_map.get(f), note))
        body.append("</div>")
    return shell("Activity", "".join(body))


# ------------------------------------------------------------- directory

def directory(db, index=None, web=None):
    """Complete ssrf-lite directory, never-heard entries included."""
    now = time.time()
    mons = web.monitors(db, index)
    rows = ""
    for _key, m in sorted(mons.items(),
                          key=lambda kv: (kv[1].get("freq_hz") or 0,
                                          str(kv[1].get("target") or ""))):
        lh = m.get("last_heard")
        if not lh:
            label = "never heard"
        else:
            d = now - lh
            if d <= 7 * 86400:
                label = "active"
            elif d <= 30 * 86400:
                label = "stale"
            else:
                label = "dormant"
        glyph, colour = fresh_mark(lh, now)
        rate = m.get("hit_rate")
        name = m.get("name") or m.get("target") or "-"
        tone = web._tone_text(m.get("params")) if m.get("params") else ""
        who = ", ".join(m.get("observers_heard") or [])
        rows += "<tr>" + "".join((
            web._td(_esc(_mhz(m.get("freq_hz"))), m.get("freq_hz") or 0),
            web._td(_esc(name), name),
            web._td(_esc(m.get("callsign") or "-"), m.get("callsign") or ""),
            web._td(_esc(m.get("service") or "-"), m.get("service") or ""),
            web._td(_esc(m.get("decoder") or "-"), m.get("decoder") or ""),
            web._td(_esc(tone) if tone else DASH, tone),
            web._td('<span style="color:%s">%s</span> %s'
                    % (colour, glyph, _esc(label)), lh or 0),
            web._td(_esc(fresh_text(lh, now)), lh or 0),
            web._td(_esc(who) if who else DASH, who),
            web._td("-" if rate is None else "%.0f%%" % (rate * 100),
                    "" if rate is None else rate),
            web._td(str(m.get("checks") or 0), m.get("checks") or 0),
        )) + "</tr>"
    cols = ("frequency", "repeater", "callsign", "service", "decoder",
            "tone / CC", "status", "last heard", "heard by", "hit rate",
            "checks")
    body = ["<h2>Directory \u00b7 " + str(len(mons)) + " monitored</h2>"]
    body.append('<p class="health">Every catalog entry under monitor,'
                " including the ones we have never heard. A long run of"
                " checks with no hearing is a finding, not an empty"
                " result.</p>")
    body.append(web._table(cols, rows, tid="dirtable") if rows
                else '<p class="empty">no monitor targets configured</p>')
    return shell("Repeaters", "".join(body), title="Repeater directory",
                 sub="Every ssrf-lite entry we monitor",
                 script=web.SCRIPT)


# ------------------------------------------------------------ investigate

def investigate():
    """Placeholder. Shipping this honest rather than faking a queue."""
    body = ["<h2>Investigate</h2>",
            '<div class="note">Not built yet.<br><br>'
            "This tab is reserved for the review queue \u2014 signals that"
            " need a human or an RT3S to resolve: unidentified carriers,"
            " tone claims the observers cannot confirm, and catalog entries"
            " whose published parameters disagree with what we actually"
            " hear.<br><br>"
            "Until it exists, unresolved signals surface as unnamed"
            ' channels in <a href="/channels.json">channels.json</a> and as'
            ' never-heard rows under <a href="/repeaters">Repeaters</a>.'
            "</div>"]
    return shell("Investigate", "".join(body), title="Investigate",
                 sub="Signals that need a human")


# ---------------------------------------------------------------- network

def network(db, registry, index=None, web=None):
    """Observers, beacon calibration, receivers, raw sweep state."""
    body = []
    sts = web.stations(db, registry)
    cal = {}
    for b in web.beacons(db):
        cal.setdefault(b["station_id"], []).append(b)

    if sts:
        rows = ""
        for s in sts:
            refs = sorted(cal.get(s["station_id"], []),
                          key=lambda x: (x["receiver"], x["freq_hz"]))
            if not refs:
                # An observer with no beacon reference is still an
                # observer -- show it, and show the gap.
                rows += "<tr>" + "".join((
                    web._td(_esc(s["station_id"]), s["station_id"]),
                    web._td(DASH, ""),
                    web._td('<span class="unk">no beacon reference'
                            "</span>", ""),
                    web._td("-", ""), web._td("-", ""), web._td("-", ""),
                    web._td("-", ""),
                    web._td(web._ago(s["last_batch"]),
                            s["last_batch"] or ""),
                )) + "</tr>"
                continue
            for b in refs:
                colour = web.BADGE.get(b["badge"], "#888")
                ref = ('%s <span class="hint">@ %.4f MHz</span>'
                       % (_esc(b["ref_id"]), b["freq_hz"] / 1e6))
                rows += "<tr>" + "".join((
                    web._td(_esc(s["station_id"]), s["station_id"]),
                    web._td(_esc(b["receiver"]), b["receiver"]),
                    web._td(ref, b["ref_id"]),
                    web._td('<span class="b" style="background:%s">%s'
                            "</span>" % (colour, _esc(b["badge"]).upper()),
                            b["badge"]),
                    web._td(web._fnum(b["snr_db"]),
                            "" if b["snr_db"] is None else b["snr_db"]),
                    web._td(web._fnum(b["drift_db"], "%+.1f", "n/a"),
                            "" if b["drift_db"] is None else b["drift_db"]),
                    web._td(web._ago(b["ts"]), b["ts"]),
                    web._td(web._ago(s["last_batch"]),
                            s["last_batch"] or ""),
                )) + "</tr>"
        obs_html = web._table(
            ("station", "receiver", "calibrated to", "status", "SNR dB",
             "drift", "last reading", "last report"), rows, tid="obstable")
    else:
        obs_html = '<p class="empty">no stations enrolled</p>'
    body.append("<h2>Observers &amp; beacon calibration</h2>")
    body.append(obs_html)

    rx = web.receivers(db)
    if rx:
        rrows = ""
        for r in sorted(rx, key=lambda x: (str(x.get("station_id")),
                                           str(x.get("receiver")))):
            ant = r.get("antenna") or {}
            place = r.get("placement") or {}
            desc = ", ".join(str(v) for v in
                             (ant.get("model") or ant.get("type"),
                              place.get("site") or place.get("location"))
                             if v)
            rrows += "<tr>" + "".join((
                web._td(_esc(r.get("station_id")), r.get("station_id") or ""),
                web._td(_esc(r.get("receiver")), r.get("receiver") or ""),
                web._td(_esc(r.get("role") or "-"), r.get("role") or ""),
                web._td(web._fnum(r.get("gain")),
                        "" if r.get("gain") is None else r.get("gain")),
                web._td(_esc(desc) if desc else DASH, desc),
                web._td(web._ago(r.get("updated_at")),
                        r.get("updated_at") or ""),
            )) + "</tr>"
        body.append("<h2>Receivers</h2>")
        body.append(web._table(("station", "receiver", "role", "gain",
                                "antenna / placement", "updated"),
                               rrows, tid="rxtable"))

    last = web.last_observation_ts(db)
    chans = web.channels(db, index)
    body.append("<h2>Sweep</h2>")
    body.append('<p class="health"><b>' + str(len(chans)) + "</b> distinct"
                " channels observed \u00b7 last observation "
                + _esc(web._ago(last)) + "</p>")
    return shell("Network", "".join(body), title="Network",
                 sub="Observer stations, calibration and raw sweep data",
                 script=web.SCRIPT)
