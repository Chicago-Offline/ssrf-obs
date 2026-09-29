"""Read-only public observers page + JSON feed.

Stdlib http.server on purpose: the evidence DB is append-only and this
surface never writes, so an extra web framework would buy nothing.

Routes:
  /              HTML observers page (live window first, then the archive)
  /active.json   channels heard inside the live window, newest first
  /feed.json     recent observations, newest first (?limit=, ?freq=)
  /channels.json per-channel verification rollup (same data as `report`)
  /monitors.json catalog-channel rollup from monitor checks: last heard,
                 hit rate, which observers, per-parameter verification
  /repeaters.json amateur/GMRS repeaters heard in the last RECENT_S
  /stations.json enrolled stations + last-heard
  /names.json    status of the optional freq -> name index

Station public keys are NEVER served -- only station ids.
"""
import html
import json
import logging
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import names as names_mod, rules, ui

log = logging.getLogger(__name__)

MAX_LIMIT = 500
DEFAULT_LIMIT = 100

# How far back "on the air right now" reaches. Short on purpose: this is the
# only part of the page that answers "is anything happening", and a window
# wide enough to always look busy would stop answering that question.
ACTIVE_WINDOW_S = 900

# "Recently" for the repeater directory on the page. A week keeps weekly
# nets visible without letting a one-off from last month read as current
# activity.
RECENT_S = 7 * 86400

# Deep link into the ssrf-lite browser. The site restores filters from the
# URL hash (site/app.js applyHash), so ?q=<callsign> lands on the record.
SSRF_BROWSE = "https://chicago-offline.github.io/ssrf-lite/#browse?q="

# Only these services belong in the "repeaters heard" directory. Rail,
# business, and unresolved energy stay in the JSON feeds.
REPEATER_SERVICES = ("amateur", "gmrs")


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


def feed(db, limit=DEFAULT_LIMIT, freq_hz=None, index=None, since=None):
    """Recent observations, newest first.

    since is an epoch floor, so a caller asking "what happened in the last
    N seconds" gets a real time query instead of guessing at a row limit.
    """
    q = ("SELECT station_id, ts, receiver, freq_hz, snr_db, duration_s,"
         " decoder, gated, meta FROM observations")
    where, args = [], []
    if freq_hz:
        where.append("freq_hz=?")
        args.append(freq_hz)
    if since is not None:
        where.append("ts>=?")
        args.append(since)
    if where:
        q += " WHERE " + " AND ".join(where)
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


def active(db, window_s=ACTIVE_WINDOW_S, index=None):
    """Channels heard inside the live window, collapsed one row per channel.

    The archive already answers "what have we ever heard". This answers the
    question a visitor actually arrives with -- what is on the air now --
    and it keeps the observer set per channel, because the same channel
    heard by two observers inside one window is the first hint of a
    corroborated hit rather than one receiver's local artifact.
    """
    rows = feed(db, limit=MAX_LIMIT, index=index,
                since=time.time() - window_s)
    out = {}
    for o in rows:
        cur = out.get(o["freq_hz"])
        if cur is None:
            out[o["freq_hz"]] = dict(o, hits=1,
                                     observers=[o["station_id"]],
                                     best_snr=o["snr_db"])
            continue
        cur["hits"] += 1
        if o["station_id"] not in cur["observers"]:
            cur["observers"].append(o["station_id"])
        if o["snr_db"] is not None and (cur["best_snr"] is None
                                        or o["snr_db"] > cur["best_snr"]):
            cur["best_snr"] = o["snr_db"]
    items = sorted(out.values(), key=lambda r: r["ts"], reverse=True)
    for r in items:
        r["observers"].sort()
    return items


def last_observation_ts(db):
    """Newest observation timestamp, or None. Used for the quiet-window note
    so an empty live table can say how quiet, not just that it is empty."""
    row = db.db.execute("SELECT MAX(ts) FROM observations").fetchone()
    return row[0] if row and row[0] is not None else None


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
        if r["coverage"] != "ok" and badge == "verified":
            # Measured, but unscoreable: antenna undeclared (unverified) or
            # not rated for the band (no_reference -- rf-survey measures
            # those on every pass since 2026-09-28). Real reception either
            # way, so it must not read as a calibration pass.
            badge = "observed"
        out.append(dict(r, age_s=age_s, drift_db=drift_db, badge=badge))
    return out


def receivers(db):
    rows = db.receiver_rows()
    for r in rows:
        r["antenna"] = json.loads(r["antenna"]) if r.get("antenna") else None
        r["placement"] = json.loads(r["placement"]) if r.get("placement") else None
    return rows


def _tone_text(params):
    """Human-readable published tone/CC claim from merged param_state.

    Shows what the catalog says the channel uses, not what we observed.
    CTCSS -> "107.2 Hz", DCS -> "DCS 072", CC -> "CC 1", CSQ -> "CSQ",
    nothing claimed -> "".
    """
    if not params:
        return ""
    parts = []
    ctcss = params.get("ctcss_hz")
    if ctcss:
        obs = ctcss.get("observed")
        if ctcss.get("state") == "no_claim":
            # Catalog explicitly says CSQ (null expect)
            parts.append("CSQ")
        elif obs is not None:
            parts.append("%.1f Hz" % obs)
    dcs = params.get("dcs_code")
    if dcs and dcs.get("state") != "no_claim":
        obs = dcs.get("observed")
        if obs:
            parts.append("DCS %s" % str(obs).zfill(3))
    cc = params.get("color_code")
    if cc and cc.get("state") != "no_claim":
        obs = cc.get("observed")
        if obs is not None:
            parts.append("CC %s" % obs)
    return ", ".join(parts)


def _catalog_tone(ident):
    """Published tone/CC straight from the ssrf-lite record.

    A repeater that was heard but never monitored has no param_state to
    merge, so the record's own claim is the only tone the page can honestly
    show -- and it is presented as the claim, never as a measurement.
    """
    if not ident:
        return ""
    parts = []
    ctcss = ident.get("ctcss")
    if ctcss:
        try:
            parts.append("%.1f Hz" % float(ctcss))
        except (TypeError, ValueError):
            pass
    dcs = ident.get("dcs")
    if dcs:
        parts.append("DCS %s" % str(dcs).zfill(3))
    cc = ident.get("color_code")
    if cc is not None:
        parts.append("CC %s" % cc)
    return ", ".join(parts)


def repeaters(db, index=None, window_s=RECENT_S):
    """Amateur/GMRS repeaters heard inside the window, newest first.

    Two sources, monitors first. A monitor row knows its catalog target and
    carries the published tone claim even when the name index is down, so
    it is trusted as a repeater without needing the index. A plain
    observation only knows the frequency, so it contributes only when the
    name index resolves it to an amateur or GMRS record -- an unnamed hit
    could be anything, and this table exists to say "that repeater is
    alive", not "we heard energy".
    """
    now = time.time()
    rows, seen = [], set()
    for _key, m in monitors(db, index).items():
        if not m["last_heard"] or now - m["last_heard"] > window_s:
            continue
        svc = m.get("service")
        if svc and svc not in REPEATER_SERVICES:
            continue
        ident = identify(index, m["freq_hz"])
        call = m.get("callsign") or \
            ((m["target"] or "").split(" ") or [None])[0] or None
        tone = _tone_text(m.get("params") or {}) or _catalog_tone(ident)
        rows.append({
            "freq_hz": m["freq_hz"],
            "freq_mhz": round((m["freq_hz"] or 0) / 1e6, 4),
            "name": m.get("name") or m["target"],
            "callsign": call,
            "service": svc,
            "mode": (ident or {}).get("mode") or m.get("decoder"),
            "tone": tone or None,
            "last_heard": m["last_heard"],
            "heard_by": m["observers_heard"],
            "hit_rate": m["hit_rate"],
            "checks": m["checks"],
            "link": SSRF_BROWSE + urllib.parse.quote(call or m["target"]),
            "source": "monitor",
        })
        seen.add(m["freq_hz"])
    for f, c in channels(db, index).items():
        if f in seen or not c.get("name"):
            continue
        if c.get("service") not in REPEATER_SERVICES:
            continue
        if not c["last_heard"] or now - c["last_heard"] > window_s:
            continue
        who = sorted(r[0] for r in db.db.execute(
            "SELECT DISTINCT station_id FROM observations"
            " WHERE freq_hz=? AND ts>=? AND gated=1",
            (f, now - window_s)))
        ident = identify(index, f)
        call = c.get("callsign")
        rows.append({
            "freq_hz": f,
            "freq_mhz": round(f / 1e6, 4),
            "name": c["name"],
            "callsign": call,
            "service": c.get("service"),
            "mode": (ident or {}).get("mode"),
            "tone": _catalog_tone(ident) or None,
            "last_heard": c["last_heard"],
            "heard_by": who,
            "hit_rate": None,
            "checks": None,
            "link": SSRF_BROWSE + urllib.parse.quote(call or c["name"]),
            "source": "observed",
        })
    rows.sort(key=lambda r: -(r["last_heard"] or 0))
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
      margin:0;padding:2rem 1.25rem;max-width:64rem;margin-inline:auto}}
 h1{{color:#00E5FF;font-size:1.3rem;letter-spacing:.06em;margin:0 0 .25rem}}
 h2{{color:#00E5FF;font-size:.95rem;letter-spacing:.08em;text-transform:uppercase;
     margin:2rem 0 .6rem;border-bottom:1px solid #1d262e;padding-bottom:.35rem}}
 h2 .n{{color:#42525c;font-weight:400;letter-spacing:0;text-transform:none}}
 .sub{{color:#6d7f8b;margin:0 0 1rem}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th{{text-align:left;color:#6d7f8b;font-weight:500;padding:.35rem .6rem .35rem 0;
     border-bottom:1px solid #1d262e}}
 td{{padding:.35rem .6rem .35rem 0;border-bottom:1px solid #141b21}}
 .b{{display:inline-block;padding:0 .45rem;border-radius:3px;color:#0b0e11;
     font-weight:600;font-size:11px;letter-spacing:.04em}}
 a{{color:#00E5FF}}
 .empty{{color:#6d7f8b;font-style:italic}}
 .hint{{color:#6d7f8b;font-size:11px}}
 .unk{{color:#42525c}}
 footer{{color:#42525c;margin-top:2.5rem;font-size:12px}}
 th.sx{{cursor:pointer;user-select:none;white-space:nowrap}}
 th.sx:hover{{color:#00E5FF}}
 th.sx::after{{content:"\\2195";opacity:.3;margin-left:.3rem;font-size:10px}}
 th.asc::after{{content:"\\2191";opacity:1;color:#00E5FF}}
 th.desc::after{{content:"\\2193";opacity:1;color:#00E5FF}}
 .who{{color:#8fa3b0;font-size:12px}}
 .tone{{color:#8fa3b0;font-size:12px}}
</style>
<h1>&#128225; CHICAGO OFFLINE &mdash; RF OBSERVERS</h1>
<p class="sub">Who is listening on the Chicagoland airwaves, and which local
 repeaters they have actually heard lately.
 <span class="hint">Click any column heading to sort.</span></p>

<h2>Observers <span class="n">&amp; beacon calibration</span></h2>
<p class="sub">Enrolled receive stations, and the reference beacon each
 receiver is calibrated against (rf-survey NETWORK.md &sect;7). Calibration
 bounds <em>hardware</em> variance &mdash; dead dongle, wet feedline, moved
 antenna, drifting gain &mdash; never propagation.</p>
{observers}

<h2>Repeaters heard &mdash; last {days} days <span class="n">{repn}</span></h2>
<p class="sub">Amateur and GMRS repeaters from
 <a href="https://chicago-offline.github.io/ssrf-lite/">ssrf-lite</a> heard
 by at least one observer. Tone/CC is the record's published claim, not a
 measurement. Each entry links to its ssrf-lite record. Full evidence &mdash;
 including channels checked and never heard &mdash; stays in the JSON
 feeds below.</p>
{repeaters}

<footer>ssrf-obs &middot; append-only evidence &middot; full feeds:
 <a href="/repeaters.json">repeaters</a> &middot;
 <a href="/active.json">active</a> &middot;
 <a href="/feed.json">observations</a> &middot;
 <a href="/channels.json">channels</a> &middot;
 <a href="/monitors.json">monitors</a> &middot;
 <a href="/stations.json">stations</a> &middot;
 <a href="/beacons.json">beacons</a> &middot;
 <a href="/receivers.json">receivers</a> &middot;
 <a href="/names.json">names</a>
 &middot; generated {now}</footer>
{script}
"""

# Vanilla JS on purpose: the server is stdlib http.server and the page is
# a few dozen rows. Sorting reads td[data-v] so "3h ago" sorts by epoch
# and "146.8800" sorts as a number, not as text.
SCRIPT = """<script>
(function(){
 function key(td){
  if(!td){return '';}
  var v=td.getAttribute('data-v');
  if(v!==null){var f=parseFloat(v);return (v!==''&&!isNaN(f))?f:v;}
  var s=(td.textContent||'').trim(),g=parseFloat(s);
  return (!isNaN(g)&&/^[-+.0-9]/.test(s))?g:s;
 }
 function blank(x){return x===''||x==='-'||x==='\\u2014';}
 function sortable(t){
  var hd=t.tHead&&t.tHead.rows[0];
  if(!hd||!t.tBodies.length){return;}
  Array.prototype.forEach.call(hd.cells,function(th,i){
   function go(){
    var dir=th.getAttribute('data-dir')==='a'?'d':'a';
    Array.prototype.forEach.call(hd.cells,function(o){
     o.removeAttribute('data-dir');o.classList.remove('asc','desc');});
    th.setAttribute('data-dir',dir);
    th.classList.add(dir==='a'?'asc':'desc');
    var tb=t.tBodies[0],rows=Array.prototype.slice.call(tb.rows);
    rows.sort(function(a,b){
     var x=key(a.cells[i]),y=key(b.cells[i]);
     if(blank(x)&&blank(y)){return 0;}
     if(blank(x)){return 1;}
     if(blank(y)){return -1;}
     var c=(typeof x==='number'&&typeof y==='number')?(x-y):
       String(x).localeCompare(String(y),undefined,{numeric:true});
     return dir==='a'?c:-c;});
    rows.forEach(function(r){tb.appendChild(r);});
   }
   th.addEventListener('click',go);
   th.addEventListener('keydown',function(e){
    if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}});
  });
 }
 Array.prototype.forEach.call(
  document.querySelectorAll('table.s'),sortable);
})();
</script>"""


def _td(disp, sort=None, cls=None):
    """One cell. sort is the machine-sortable value behind the display text
    (epoch behind "3h ago", float behind "146.8800 MHz")."""
    a = ""
    if cls:
        a += ' class="%s"' % cls
    if sort is not None:
        a += ' data-v="%s"' % html.escape(str(sort), quote=True)
    return "<td%s>%s</td>" % (a, disp)


def _table(cols, rows, tid=None):
    head = "".join('<th class="sx" tabindex="0">%s</th>' % c for c in cols)
    return ('<table class="s"%s><thead><tr>%s</tr></thead><tbody>%s</tbody>'
            "</table>" % (' id="%s"' % tid if tid else "", head, rows))


def _fnum(v, fmt="%.1f", dash="-"):
    return dash if v is None else fmt % v


def _self():
    """This module, for ui.py to reuse without a circular import."""
    return sys.modules[__name__]


def render(db, registry, index=None):
    """Default page = the Activity tab.

    Page layout lives in ui.py. This stays as the entry point that
    existing callers and tests already import.
    """
    return ui.activity(db, index, web=_self())


def render_directory(db, registry=None, index=None):
    """Repeaters tab: the full catalog, never-heard included."""
    return ui.directory(db, index, web=_self())


def render_investigate(db=None, registry=None, index=None):
    """Investigate tab: not built yet, and says so."""
    return ui.investigate()


def render_network(db, registry, index=None):
    """Network tab: observers, calibration, receivers, sweep."""
    return ui.network(db, registry, index, web=_self())


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
                elif u.path in ("/repeaters", "/repeaters/"):
                    self._send(200,
                               render_directory(db, registry, index),
                               "text/html; charset=utf-8")
                elif u.path in ("/investigate", "/investigate/"):
                    self._send(200, render_investigate(),
                               "text/html; charset=utf-8")
                elif u.path in ("/network", "/network/"):
                    self._send(200,
                               render_network(db, registry, index),
                               "text/html; charset=utf-8")
                elif u.path == "/repeaters.json":
                    items = repeaters(db, index)
                    self._json({"generated": time.time(),
                                "window_s": RECENT_S,
                                "count": len(items),
                                "repeaters": items})
                elif u.path == "/active.json":
                    w = q.get("window", [ACTIVE_WINDOW_S])[0]
                    w = int(w) if str(w).isdigit() else ACTIVE_WINDOW_S
                    items = active(db, w, index)
                    self._json({"generated": time.time(),
                                "window_s": w,
                                "count": len(items),
                                "channels": items})
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
