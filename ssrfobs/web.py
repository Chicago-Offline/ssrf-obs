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

# How far back "on the air right now" reaches. Short on purpose: this is the
# only part of the page that answers "is anything happening", and a window
# wide enough to always look busy would stop answering that question.
ACTIVE_WINDOW_S = 900


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

# Monitor statuses are NOT the V0-V3 evidence tiers, so they deliberately
# do not reuse BADGE. A tier grades how well a signal was captured;
# a monitor status describes OUR listening. Red on never_heard is the
# point: it is the only status that accuses a catalog entry of being
# wrong, and it should look like an accusation.
MON_BADGE = {"active": "#39FF14", "stale": "#FFB300", "dormant": "#888",
             "never_heard": "#FF5252", "watching": "#00E5FF"}


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
 .lvl{{color:#FFB300}}
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
 .live{{border:1px solid #14323a;background:#0d1519;border-radius:6px;
        padding:.8rem .9rem}}
 .live h2{{margin-top:0}}
 .dot{{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;
       background:#39FF14;margin-right:.45rem;vertical-align:middle}}
 .dot.off{{background:#42525c}}
 .filt{{margin:0 0 .6rem}}
 .filt button{{background:#111a20;color:#6d7f8b;border:1px solid #1d262e;
   border-radius:3px;font:inherit;font-size:11px;padding:.15rem .55rem;
   margin-right:.3rem;cursor:pointer}}
 .filt button.on{{color:#0b0e11;background:#00E5FF;border-color:#00E5FF;
   font-weight:600}}
 .who{{color:#8fa3b0;font-size:12px}}
</style>
<h1>&#128225; CHICAGO OFFLINE &mdash; RF OBSERVERS</h1>
<p class="sub">Signed survey evidence from enrolled receive stations.
 Feed: <a href="/active.json">/active.json</a> &middot;
 <a href="/feed.json">/feed.json</a> &middot;
 <a href="/channels.json">/channels.json</a> &middot;
 <a href="/monitors.json">/monitors.json</a> &middot;
 <a href="/stations.json">/stations.json</a> &middot;
 <a href="/beacons.json">/beacons.json</a> &middot;
 <a href="/receivers.json">/receivers.json</a> &middot;
 <a href="/names.json">/names.json</a>
 <br><span class="hint">Click any column heading to sort.</span></p>

<div class="live">
<h2><span class="dot{livedot}"></span>On the air &mdash; last {window} min
 <span class="n">{livecount}</span></h2>
{active}
</div>

<h2>Observer stations</h2>
{stations}

<h2>Recent observations <span class="n">last {recentn}</span></h2>
{recent}

<h2>Channel roll-up <span class="n">every frequency ever heard</span></h2>
<p class="sub">The archive, not the live picture &mdash; one row per distinct
 frequency across the whole append-only DB, which is why it is long. Its job
 is to show <em>coverage gaps</em>: a resolved channel with no name is a
 missing <a href="https://chicago-offline.github.io/ssrf-lite/">ssrf-lite</a>
 entry worth filing. <span class="hint">(input)</span> = matched a repeater's
 input side. <span class="hint">(near)</span> = off-raster, matched inside the
 tolerance window. <span class="unk">&mdash;</span> = no entry in ssrf-lite
 yet. <span class="hint">(unresolved)</span> rows are sweep artifacts, not
 channels &mdash; the bin was too wide to identify one channel, so they are
 hidden by default.</p>
{channels}

<h2>Channel monitoring <span class="n">{monitorn}</span></h2>
<p class="sub">Catalog channels checked on a schedule whether or not the
 sweep sees energy there &mdash; the answer to &ldquo;is this repeater
 actually on the air?&rdquo; A <em>silent</em> check is the whole point: it
 is what separates a dead machine from one nobody ever pointed a receiver
 at. <code>watching</code> = checked, but not yet hard enough to claim
 anything. <code>never_heard</code> = checked hard enough to mean it and
 still always silent, which is a finding against the
 <a href="https://chicago-offline.github.io/ssrf-lite/">ssrf-lite</a>
 entry &mdash; so it takes many checks across many days before it will say
 so. Hit rate is over checks actually performed, not over wall time: it
 answers &ldquo;when we listen, how often is it there&rdquo;, which is what
 a scan list cares about. Targets are fenced by distance, so a repeater
 nobody here could hear is absent rather than slandered as silent.</p>
{monitors}

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
{script}
"""

# Vanilla JS on purpose: the server is stdlib http.server and the page is
# a few hundred rows. Sorting reads td[data-v] so "3h ago" sorts by epoch
# and "460.1250" sorts as a number, not as text.
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

 var box=document.getElementById('chfilter'),
     tbl=document.getElementById('chtable');
 if(box&&tbl){
  var btns=box.getElementsByTagName('button');
  function apply(mode){
   Array.prototype.forEach.call(tbl.tBodies[0].rows,function(r){
    var ok=(mode==='all')||
           (mode==='named'&&r.getAttribute('data-named')==='1')||
           (mode==='res'&&r.getAttribute('data-res')==='1');
    r.style.display=ok?'':'none';});
   Array.prototype.forEach.call(btns,function(b){
    if(b.getAttribute('data-m')===mode){b.classList.add('on');}
    else{b.classList.remove('on');}});
   try{localStorage.setItem('chmode',mode);}catch(e){}
  }
  Array.prototype.forEach.call(btns,function(b){
   b.addEventListener('click',function(){
    apply(b.getAttribute('data-m'));});});
  var saved=null;
  try{saved=localStorage.getItem('chmode');}catch(e){}
  apply(saved||'res');
 }
})();
</script>"""


def _td(disp, sort=None, cls=None):
    """One cell. sort is the machine-sortable value behind the display text
    (epoch behind "3h ago", float behind "460.1250 MHz")."""
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


def _ident_cell(row):
    """Identity table cell: the name, plus hints when the match is indirect."""
    name = row.get("name")
    if not name:
        if row.get("channel_snap") is False:
            # Not "we don't know who" but "the sweep couldn't resolve a
            # channel here", which is a different (and fixable) problem.
            return ('<td data-v="" class="unk">&mdash; <span class="hint"'
                    ' title="rf-survey could not snap this hit to a channel'
                    ' raster point, so the frequency is a bin centre rather'
                    ' than a channel. Needs a narrower sweep bin or a'
                    ' refined dwell before it can be named.">'
                    '(unresolved)</span></td>')
        return '<td data-v="" class="unk">&mdash;</td>'
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
    return '<td data-v="%s">%s</td>' % (html.escape(name, quote=True), cell)



def _active_html(db, index):
    """Live window table + an empty state that says how quiet it is."""
    act = active(db, index=index)
    if not act:
        last = last_observation_ts(db)
        if last is None:
            return ('<p class="empty">no observations yet &mdash; stations '
                    'are enrolled but nothing has been reported</p>'), "", " off"
        return ('<p class="empty">nothing on the air in the last %d min '
                '&mdash; most recent hit was %s</p>'
                % (ACTIVE_WINDOW_S // 60, _ago(last))), "", " off"
    rows = ""
    for o in act:
        who = ", ".join(o["observers"])
        rows += "<tr>" + "".join((
            _td(_ago(o["ts"]), o["ts"]),
            _td(_fnum(o["freq_mhz"], "%.4f"), o["freq_mhz"] or ""),
            _ident_cell(o),
            _td(html.escape(who), who, cls="who"),
            _td(o["level"] or "-", o["level"] or "", cls="lvl"),
            _td(_fnum(o["best_snr"]),
                "" if o["best_snr"] is None else o["best_snr"]),
            _td(o["hits"], o["hits"]),
        )) + "</tr>"
    tbl = _table(["when", "MHz", "station / system", "heard by", "tier",
                  "best SNR", "hits"], rows)
    n = "%d channel%s" % (len(act), "" if len(act) == 1 else "s")
    return tbl, n, ""


def render(db, registry, index=None):
    act_html, act_n, act_dot = _active_html(db, index)

    sts = stations(db, registry)
    if sts:
        rows = ""
        for s in sts:
            rows += "<tr>" + "".join((
                _td(html.escape(s["station_id"]), s["station_id"]),
                _td(_ago(s["last_batch"]), s["last_batch"] or ""),
                _td(s["observations"], s["observations"]),
                _td(s["sweep_bins"], s["sweep_bins"]),
            )) + "</tr>"
        st_html = _table(["station", "last batch", "obs", "sweep bins"], rows)
    else:
        st_html = '<p class="empty">no stations enrolled</p>'

    ch = channels(db, index)
    if ch:
        rows = ""
        n_named = n_res = 0
        for f, c in sorted(ch.items()):
            named = 1 if c.get("name") else 0
            res = 1 if c.get("channel_snap") else 0
            n_named += named
            n_res += res
            color = BADGE.get(c["status"], "#888")
            mhz = f / 1e6
            rows += '<tr data-named="%d" data-res="%d">' % (named, res)
            rows += "".join((
                _td("%.4f MHz" % mhz, "%.6f" % mhz),
                _ident_cell(c),
                _td(c["level"], c["level"], cls="lvl"),
                _td('<span class="b" style="background:%s">%s</span>'
                    % (color, c["status"].upper()), c["status"]),
                _td(_ago(c["last_heard"]), c["last_heard"] or ""),
                _td(c["v1_stations"], c["v1_stations"]),
            )) + "</tr>"
        filt = ('<div class="filt" id="chfilter">'
                '<button data-m="res">resolved channels (%d)</button>'
                '<button data-m="named">named in ssrf-lite (%d)</button>'
                '<button data-m="all">everything (%d)</button></div>'
                % (n_res, n_named, len(ch)))
        ch_html = filt + _table(
            ["frequency", "station / system", "tier", "status", "last heard",
             "V1 stations"], rows, tid="chtable")
    else:
        ch_html = ('<p class="empty">no graded channels yet &mdash; '
                   'stations are sweeping, nothing has tripped the dwell gate'
                   '</p>')

    items = feed(db, limit=25, index=index)
    if items:
        rows = ""
        for o in items:
            rows += "<tr>" + "".join((
                _td(_ago(o["ts"]), o["ts"]),
                _td(html.escape(o["station_id"]), o["station_id"]),
                _td("%.4f" % o["freq_mhz"] if o["freq_mhz"] else "?",
                    o["freq_mhz"] or ""),
                _ident_cell(o),
                _td(html.escape(o["decoder"] or "-"), o["decoder"] or ""),
                _td(o["level"] or "-", o["level"] or "", cls="lvl"),
                _td(_fnum(o["snr_db"]),
                    "" if o["snr_db"] is None else o["snr_db"]),
            )) + "</tr>"
        rc_html = _table(["when", "observer", "MHz", "station / system",
                          "decoder", "tier", "SNR dB"], rows)
    else:
        rc_html = '<p class="empty">no observations yet</p>'

    bl = beacons(db)
    if bl:
        rows = ""
        for b in sorted(bl, key=lambda x: (x["station_id"], x["receiver"],
                                           x["freq_hz"])):
            color = BADGE.get(b["badge"], "#888")
            rows += "<tr>" + "".join((
                _td(html.escape(b["station_id"]), b["station_id"]),
                _td(html.escape(b["receiver"]), b["receiver"]),
                _td(html.escape(b["ref_id"]), b["ref_id"]),
                _td("%.4f" % (b["freq_hz"] / 1e6), b["freq_hz"]),
                _td('<span class="b" style="background:%s">%s</span>'
                    % (color, b["badge"].upper()), b["badge"]),
                _td(_fnum(b["snr_db"]),
                    "" if b["snr_db"] is None else b["snr_db"]),
                _td(_fnum(b["drift_db"], "%+.1f", "n/a"),
                    "" if b["drift_db"] is None else b["drift_db"]),
                _td(_ago(b["ts"]), b["ts"]),
            )) + "</tr>"
        bc_html = _table(["station", "receiver", "reference", "MHz", "status",
                          "SNR dB", "drift", "last reading"], rows)
    else:
        bc_html = ('<p class="empty">no beacon-check runs reported yet '
                   '&mdash; run <code>survey beacon-check --serial '
                   '&lt;serial&gt;</code> on an observer</p>')

    mons = monitors(db, index)
    if mons:
        rows = ""
        tally = {}
        for _key, m in sorted(mons.items(),
                              key=lambda kv: (kv[1]["freq_hz"] or 0,
                                              kv[1]["target"] or "")):
            tally[m["status"]] = tally.get(m["status"], 0) + 1
            color = MON_BADGE.get(m["status"], "#888")
            mhz = (m["freq_hz"] or 0) / 1e6
            obs = m["observers"] or []
            heard_by = m["observers_heard"] or []
            # Show who HEARD it when anyone has, otherwise who is
            # listening. A silent row still has to name its observers or
            # "never heard" is an unattributable claim.
            who = ", ".join(heard_by or obs)
            rows += "<tr>" + "".join((
                _td("%.4f MHz" % mhz, "%.6f" % mhz),
                _td(html.escape(m["target"] or "-"), m["target"] or ""),
                _td(html.escape(m["decoder"] or "-"), m["decoder"] or ""),
                _td('<span class="b" style="background:%s">%s</span>'
                    % (color, m["status"].upper().replace("_", " ")),
                    m["status"]),
                _td(m["checks"], m["checks"]),
                _td(m["hearings"], m["hearings"]),
                _td("%.0f%%" % (m["hit_rate"] * 100), m["hit_rate"]),
                _td(_ago(m["last_heard"]), m["last_heard"] or ""),
                _td(_ago(m["last_checked"]), m["last_checked"] or ""),
                _td(html.escape(who) or '<span class="unk">&mdash;</span>',
                    who, cls="who"),
            )) + "</tr>"
        mon_html = _table(["frequency", "target", "mode", "status",
                           "checks", "heard", "hit rate", "last heard",
                           "last checked", "observers"],
                          rows, tid="montable")
        mon_n = ", ".join("%d %s" % (n, s.replace("_", " "))
                          for s, n in sorted(tally.items(),
                                             key=lambda kv: (-kv[1], kv[0])))
    else:
        mon_html = ('<p class="empty">nothing monitored yet &mdash; add a '
                    "<code>monitor:</code> block with a "
                    "<code>targets_file</code> to an observer plan</p>")
        mon_n = "none"

    return PAGE.format(active=act_html, livecount=act_n, livedot=act_dot,
                       window=ACTIVE_WINDOW_S // 60,
                       stations=st_html, channels=ch_html, recent=rc_html,
                       recentn=len(items), monitors=mon_html, monitorn=mon_n,
                       beacons=bc_html, script=SCRIPT,
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

