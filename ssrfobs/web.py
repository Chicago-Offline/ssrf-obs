"""Read-only public observers page + JSON feed.

Stdlib http.server on purpose: the evidence DB is append-only and this
surface never writes, so an extra web framework would buy nothing.

Routes:
  /              HTML observers page (stations + graded channels)
  /feed.json     recent observations, newest first (?limit=, ?freq=)
  /channels.json per-channel verification rollup (same data as `report`)
  /monitors.json catalog-channel rollup from monitor checks: last heard,
                 hit rate, which observers, per-parameter verification
  /stations.json enrolled stations + last-heard
  /names.json    status of the optional freq -> name index

Station public keys are NEVER served -- only station ids.
"""
import html
import json
import logging
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import names as names_mod, rules

log = logging.getLogger(__name__)

MAX_LIMIT = 500
DEFAULT_LIMIT = 100


def identify(index, freq_hz):
    """Name a frequency via the optional index. Never raises."""
    if index is None:
        return None
    try:
        return index.lookup(freq_hz)
    except Exception:                                    # noqa: BLE001
        log.exception("name lookup failed for %s", freq_hz)
        return None


def snapped(meta):
    """Did rf-survey resolve this hit to a channel raster point?

    meta["channel_snap"] is the surveyor's own verdict: it snapped freq_hz to
    the raster because the rtl_power bin was narrow enough to identify one
    channel. False/absent means the measurement could not resolve a channel,
    so freq_hz is a bin centre, not a channel -- and must not be named.

    Accepts either a decoded dict or the raw JSON text SQLite hands back.
    """
    if isinstance(meta, (str, bytes)):
        try:
            meta = json.loads(meta or "{}")
        except (ValueError, TypeError):
            return False
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("channel_snap"))


def _with_name(row, ident):
    """Attach identity fields to a rollup/feed row. Always the same keys."""
    row["name"] = ident["name"] if ident else None
    row["callsign"] = ident["callsign"] if ident else None
    row["service"] = ident["service"] if ident else None
    row["name_side"] = ident["side"] if ident else None
    row["name_exact"] = ident["exact"] if ident else None
    row["names"] = ident["names"] if ident else []
    row["matches"] = ident["matches"] if ident else 0
    return row


def channels(db, index=None):
    """Per-channel rollup keyed by freq_hz.

    With a name index configured, each row also carries who is known to use
    that frequency. Grading never depends on it -- an unnamed channel grades
    exactly as before, it just reads as a bare frequency.
    """
    freqs = sorted({r[0] for r in db.db.execute(
        "SELECT DISTINCT freq_hz FROM observations").fetchall()})
    out = {}
    for f in freqs:
        rows = db.observation_rows(f)
        st = rules.channel_status(rows)
        if not st:
            continue
        # Only name a frequency the surveyor actually resolved to a channel.
        ok = any(snapped(r[6]) for r in rows)
        st["channel_snap"] = ok
        out[f] = _with_name(st, identify(index, f) if ok else None)
    return out


def monitors(db, index=None):
    """Catalog-channel rollup: does the published record hold up?

    Keyed by "<freq> <target>", not by frequency alone. Two targets can
    legitimately share one frequency when they are tone-discriminated, so
    frequency is not a key here -- unlike channels(), which is derived
    from observations and has no target to split on.

    Rows with ZERO hearings are kept, which is the whole difference from
    channels(). "We have checked this repeater 214 times over six weeks
    and never heard it" is the finding, not an empty result to filter out.
    """
    groups = {}
    for r in db.monitor_check_rows():
        groups.setdefault((r[5], r[3]), []).append(r)
    out = {}
    for (freq_hz, target), rows in sorted(groups.items()):
        st = rules.monitor_rollup(rows)
        if not st:
            continue
        key = "%.4f MHz %s" % (freq_hz / 1e6, target)
        out[key] = _with_name(st, identify(index, freq_hz))
    return out


def stations(db, registry):
    """Enrolled stations with last-heard + counts. No pubkeys."""
    last = dict(db.db.execute(
        "SELECT station_id, MAX(received_at) FROM batches GROUP BY station_id"
    ).fetchall())
    obs = dict(db.db.execute(
        "SELECT station_id, COUNT(*) FROM observations GROUP BY station_id"
    ).fetchall())
    sweeps = dict(db.db.execute(
        "SELECT station_id, COUNT(*) FROM sweep_summaries GROUP BY station_id"
    ).fetchall())
    return [{"station_id": sid,
             "enrolled": True,
             "last_batch": last.get(sid),
             "observations": obs.get(sid, 0),
             "sweep_bins": sweeps.get(sid, 0)}
            for sid in sorted(registry)]


def feed(db, limit=DEFAULT_LIMIT, freq_hz=None, index=None):
    """Recent observations, newest first."""
    q = ("SELECT station_id, ts, receiver, freq_hz, snr_db, duration_s,"
         " decoder, gated, meta FROM observations")
    args = []
    if freq_hz:
        q += " WHERE freq_hz=?"
        args.append(freq_hz)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(min(int(limit), MAX_LIMIT))
    items = []
    for (sid, ts, rx, f, snr, dur, dec, gated, meta) in db.db.execute(q, args):
        m = json.loads(meta or "{}")
        t = rules.tier(gated, dec, m)
        items.append(_with_name(
            {"station_id": sid, "ts": ts, "receiver": rx,
             "freq_hz": f,
             "freq_mhz": round(f / 1e6, 4) if f else None,
             "snr_db": snr, "duration_s": dur, "decoder": dec,
             "gated": bool(gated),
             "level": "V%d" % t if t is not None else None,
             "channel_snap": snapped(m),
             "meta": m},
            identify(index, f) if snapped(m) else None))
    return items


# Drift/status thresholds for the beacon badge. A reading itself never
# claims "healthy" beyond calibration.SNR is the metric: it survives a
# gain change, which absolute dB does not (rf-survey NETWORK.md S7).
STALE_S = 3 * 3600          # no reading this recent -> can't vouch for now
NOT_HEARD_DROP_DB = 6.0     # not_heard after being ok -> flag, don't hide


def beacons(db):
    """Latest beacon reading per (station_id, receiver, ref_id), with a
    same-observer SNR history for a simple drift figure. No cross-observer
    averaging -- an observer is graded only against its own history
    (NETWORK.md S7: baselines are per observer, band-local, multi-day).
    """
    latest = db.beacon_latest()
    hist = {}
    for row in db.db.execute(
            "SELECT station_id,receiver,ref_id,snr_db,ts FROM beacon_readings"
            " WHERE status='ok' AND pinned=1 AND ts>=?"
            " ORDER BY ts DESC", (time.time() - 7 * 86400,)).fetchall():
        hist.setdefault(row[:3], []).append(row[3])

    out = []
    for r in latest:
        key = (r["station_id"], r["receiver"], r["ref_id"])
        samples = hist.get(key, [])
        drift_db = None
        if r["status"] == "ok" and len(samples) >= 5:
            baseline = sorted(samples[1:])[len(samples[1:]) // 2]
            drift_db = round(samples[0] - baseline, 1)
        age_s = time.time() - r["ts"]
        badge = "stale" if age_s > STALE_S else {
            "ok": "verified", "not_heard": "flagged",
            "no_reference": "stale", "error": "flagged",
        }.get(r["status"], "stale")
        if r["coverage"] == "unverified" and badge == "verified":
            badge = "observed"  # measured, but antenna undeclared -> unscoreable
        out.append(dict(r, age_s=age_s, drift_db=drift_db, badge=badge))
    return out


def receivers(db):
    rows = db.receiver_rows()
    for r in rows:
        r["antenna"] = json.loads(r["antenna"]) if r.get("antenna") else None
        r["placement"] = json.loads(r["placement"]) if r.get("placement") else None
    return rows


def _ago(ts):
    if not ts:
        return "never"
    d = max(0, time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return "%d%s ago" % (int(d // n), unit)
    return "%ds ago" % int(d)


BADGE = {"verified": "#39FF14", "observed": "#00E5FF",
         "flagged": "#FFB300", "stale": "#888"}

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chicago Offline - RF Observers</title>
<style>
 body{{background:#0b0e11;color:#d7e0e6;font:14px/1.5 ui-monospace,Menlo,monospace;
      margin:0;padding:2rem 1.25rem;max-width:60rem;margin-inline:auto}}
 h1{{color:#00E5FF;font-size:1.3rem;letter-spacing:.06em;margin:0 0 .25rem}}
 h2{{color:#00E5FF;font-size:.95rem;letter-spacing:.08em;text-transform:uppercase;
     margin:2rem 0 .6rem;border-bottom:1px solid #1d262e;padding-bottom:.35rem}}
 .sub{{color:#6d7f8b;margin:0 0 1rem}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th{{text-align:left;color:#6d7f8b;font-weight:500;padding:.35rem .6rem .35rem 0;
     border-bottom:1px solid #1d262e}}
 td{{padding:.35rem .6rem .35rem 0;border-bottom:1px solid #141b21}}
 .b{{display:inline-block;padding:0 .45rem;border-radius:3px;color:#0b0e11;
     font-weight:600;font-size:11px;letter-spacing:.04em}}
 .lvl{{color:#FFB300}}
 a{{color:#00E5FF}}
 .empty{{color:#6d7f8b;font-style:italic}}
 .hint{{color:#6d7f8b;font-size:11px}}
 .unk{{color:#42525c}}
 footer{{color:#42525c;margin-top:2.5rem;font-size:12px}}
</style>
<h1>&#128225; CHICAGO OFFLINE &mdash; RF OBSERVERS</h1>
<p class="sub">Signed survey evidence from enrolled receive stations.
 Feed: <a href="/feed.json">/feed.json</a> &middot;
 <a href="/channels.json">/channels.json</a> &middot;
 <a href="/monitors.json">/monitors.json</a> &middot;
 <a href="/stations.json">/stations.json</a> &middot;
 <a href="/beacons.json">/beacons.json</a> &middot;
 <a href="/receivers.json">/receivers.json</a> &middot;
 <a href="/names.json">/names.json</a></p>

<h2>Stations</h2>
{stations}

<h2>Channels</h2>
<p class="sub">Station / system names are matched by frequency against the
 curated <a href="https://chicago-offline.github.io/ssrf-lite/">ssrf-lite</a>
 channel index. They say who is <em>known to use</em> the channel &mdash; not
 who was identified on the air, which is what the tier column is for.
 <span class="hint">(input)</span> = matched a repeater's input side.
 <span class="hint">(near)</span> = off-raster, matched inside the tolerance
 window. <span class="unk">&mdash;</span> = no entry in ssrf-lite yet.</p>
{channels}

<h2>Recent observations</h2>
{recent}

<h2>Beacon calibration</h2>
<p class="sub">Reference emitters (NETWORK.md &sect;7) bound
 <em>hardware</em> variance per observer &mdash; dead dongle, wet feedline,
 moved antenna, drifting gain. Never propagation, never evidence about a
 surveyed channel. <code>no_reference</code> = outside this observer's
 declared antenna reach, correctly excluded, not hidden.
 <code>unverified</code> = antenna undeclared; measured, can't be scored.</p>
{beacons}

<footer>ssrf-obs &middot; append-only evidence &middot; tiers V0 heard /
 V1 decoded / V2 voice / V3 recorded &middot; generated {now}</footer>
"""


def _ident_cell(row):
    """Identity table cell: the name, plus hints when the match is indirect."""
    name = row.get("name")
    if not name:
        if row.get("channel_snap") is False:
            # Not "we don't know who" but "the sweep couldn't resolve a
            # channel here", which is a different (and fixable) problem.
            return ('<td class="unk">&mdash; <span class="hint"'
                    ' title="rf-survey could not snap this hit to a channel'
                    ' raster point, so the frequency is a bin centre rather'
                    ' than a channel. Needs a narrower sweep bin or a'
                    ' refined dwell before it can be named.">'
                    '(unresolved)</span></td>')
        return '<td class="unk">&mdash;</td>'
    extra = []
    call = row.get("callsign")
    if call and call != name:
        extra.append(call)
    if row.get("name_side") == "in":
        extra.append("input")
    if row.get("name_exact") is False:
        extra.append("near")
    n = row.get("matches") or 0
    if n > 1:
        extra.append("+%d more" % (n - 1))
    cell = html.escape(name)
    if extra:
        cell += ' <span class="hint">(%s)</span>' % html.escape(
            ", ".join(extra))
    return "<td>%s</td>" % cell


def render(db, registry, index=None):
    sts = stations(db, registry)
    if sts:
        rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                html.escape(s["station_id"]), _ago(s["last_batch"]),
                s["observations"], s["sweep_bins"])
            for s in sts)
        st_html = ("<table><tr><th>station<th>last batch<th>obs"
                   "<th>sweep bins</tr>" + rows + "</table>")
    else:
        st_html = '<p class="empty">no stations enrolled</p>'

    ch = channels(db, index)
    if ch:
        rows = ""
        for f, c in sorted(ch.items()):
            color = BADGE.get(c["status"], "#888")
            rows += (
                '<tr><td>%.4f MHz</td>%s<td class="lvl">%s</td>'
                '<td><span class="b" style="background:%s">%s</span></td>'
                '<td>%s</td><td>%s</td></tr>' % (
                    f / 1e6, _ident_cell(c), c["level"], color,
                    c["status"].upper(),
                    _ago(c["last_heard"]), c["v1_stations"]))
        ch_html = ("<table><tr><th>frequency<th>station / system<th>tier"
                   "<th>status<th>last heard<th>V1 stations</tr>"
                   + rows + "</table>")
    else:
        ch_html = ('<p class="empty">no graded channels yet &mdash; '
                   'stations are sweeping, nothing has tripped the dwell gate'
                   '</p>')

    items = feed(db, limit=25, index=index)
    if items:
        rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td>%s<td>%s</td>"
            '<td class="lvl">%s</td><td>%s</td></tr>' % (
                _ago(o["ts"]), html.escape(o["station_id"]),
                "%.4f" % o["freq_mhz"] if o["freq_mhz"] else "?",
                _ident_cell(o),
                html.escape(o["decoder"] or "-"),
                o["level"] or "-",
                "%.1f" % o["snr_db"] if o["snr_db"] is not None else "-")
            for o in items)
        rc_html = ("<table><tr><th>when<th>observer<th>MHz"
                   "<th>station / system<th>decoder<th>tier<th>SNR dB</tr>"
                   + rows + "</table>")
    else:
        rc_html = '<p class="empty">no observations yet</p>'

    bl = beacons(db)
    if bl:
        rows = ""
        for b in sorted(bl, key=lambda x: (x["station_id"], x["receiver"],
                                           x["freq_hz"])):
            color = BADGE.get(b["badge"], "#888")
            snr = "%.1f" % b["snr_db"] if b["snr_db"] is not None else "-"
            drift = ("%+.1f" % b["drift_db"]) if b["drift_db"] is not None \
                else "n/a"
            rows += (
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%.4f</td>"
                '<td><span class="b" style="background:%s">%s</span></td>'
                "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    html.escape(b["station_id"]), html.escape(b["receiver"]),
                    html.escape(b["ref_id"]), b["freq_hz"] / 1e6,
                    color, b["badge"].upper(), snr, drift, _ago(b["ts"])))
        bc_html = ("<table><tr><th>station<th>receiver<th>reference"
                   "<th>MHz<th>status<th>SNR dB<th>drift<th>last reading"
                   "</tr>" + rows + "</table>")
    else:
        bc_html = ('<p class="empty">no beacon-check runs reported yet '
                   '&mdash; run <code>survey beacon-check --serial '
                   '&lt;serial&gt;</code> on an observer</p>')

    return PAGE.format(stations=st_html, channels=ch_html, recent=rc_html,
                       beacons=bc_html,
                       now=time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()))


def make_handler(db, registry, index=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ssrf-obs"

        def _send(self, code, body, ctype):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, indent=2, default=str),
                       "application/json; charset=utf-8")

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            freq = q.get("freq", [None])[0]
            freq = int(freq) if freq and freq.isdigit() else None
            try:
                if u.path == "/":
                    self._send(200, render(db, registry, index),
                               "text/html; charset=utf-8")
                elif u.path == "/feed.json":
                    lim = q.get("limit", [DEFAULT_LIMIT])[0]
                    lim = int(lim) if str(lim).isdigit() else DEFAULT_LIMIT
                    items = feed(db, lim, freq, index)
                    self._json({"generated": time.time(),
                                "count": len(items),
                                "observations": items})
                elif u.path == "/channels.json":
                    self._json({"%.4f MHz" % (f / 1e6): c
                                for f, c in sorted(
                                    channels(db, index).items())})
                elif u.path == "/monitors.json":
                    mons = monitors(db, index)
                    self._json({"generated": time.time(),
                                "count": len(mons),
                                "monitors": mons})
                elif u.path == "/stations.json":
                    self._json({"stations": stations(db, registry)})
                elif u.path == "/beacons.json":
                    self._json({"generated": time.time(),
                                "beacons": beacons(db)})
                elif u.path == "/receivers.json":
                    self._json({"receivers": receivers(db)})
                elif u.path == "/names.json":
                    self._json({"enabled": index is not None,
                                "index": index.status() if index else None})
                elif u.path == "/healthz":
                    self._send(200, "ok\n", "text/plain")
                else:
                    self._json({"error": "not found"}, 404)
            except Exception:
                log.exception("error serving %s", self.path)
                self._json({"error": "internal error"}, 500)

        def log_message(self, fmt, *a):
            log.info("%s %s", self.address_string(), fmt % a)

    return Handler


def serve(cfg, registry, db):
    web = cfg.get("web") or {}
    host = web.get("host", "0.0.0.0")
    port = int(web.get("port", 3100))
    index = names_mod.from_config(cfg)
    httpd = ThreadingHTTPServer((host, port),
                                make_handler(db, registry, index))
    log.info("observers page on http://%s:%d/", host, port)
    httpd.serve_forever()
