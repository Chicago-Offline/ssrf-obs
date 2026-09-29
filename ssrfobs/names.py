"""Optional freq -> station/system name resolution for the observers page.

The evidence DB records what was *heard* (a frequency), never who owns it.
Identity lives in ssrf-lite, which is deliberately a separate, curated repo
(rf-survey NETWORK.md S3). So this module is an ADDITIVE lookup layer:

  * it never blocks or fails ingest, grading, or rendering
  * it needs no new dependencies (stdlib json/urllib only)
  * if the index is missing, unreachable, or garbage, every lookup simply
    returns None and the page renders exactly as it did before

Source of truth is ssrf-lite's published flat channel index
(site/codeplug.json), a JSON array of radio-centric records:

    {"callsign": "W9XYZ", "rx_mhz": 442.5, "tx_mhz": 447.5,
     "service": "amateur", "mode": "DMR", "name": "Some Repeater", ...}

Here rx_mhz is the far end's OUTPUT (what a radio listens to) and tx_mhz is
its INPUT (what a radio transmits). An observer hears outputs far more often
than inputs, so output-side matches win ties, but input-side matches are kept
and labelled -- hearing a repeater's input is real evidence, just of a user's
HT rather than the repeater.

MATCHING IS EXACT BY DEFAULT, AND THAT IS DELIBERATE.

rf-survey already snaps a sweep hit to the channel raster when -- and only
when -- the measurement can actually resolve one (sweep.py snap_channel:
it refuses outright when the rtl_power bin is wider than the raster, because
+/-half a bin then spans more than one raster point). Observations carry that
decision in meta as "channel_snap". A snapped freq_hz is already on-grid, so
a correct entry matches exactly and no fuzz is needed.

For an UNSNAPPED hit, nearest-match is not a weaker answer, it is a wrong
one: proximity to a raster point is not evidence (a coarse bin lands near
some channel by chance, often the wrong one). Measured 2026-09-28 against
prod, a 2.5 kHz window named 9 of 59 unsnapped frequencies -- every one a
guess, and it mapped three adjacent bins (159.6586/159.6591/159.6595) onto a
single channel, manufacturing three sightings from one. So tolerance_khz
defaults to 0 and the page reports such channels as unresolved instead.

Config (all optional), under "names:" in config.yml:

    names:
      enabled: true
      path: /data/names.json        # local override, tried first
      url: https://chicago-offline.github.io/ssrf-lite/codeplug.json
      refresh_s: 21600              # re-fetch the URL at most this often
      tolerance_khz: 0              # >0 opts into nearest-match; see above

A local "path" is tried before "url" so a private overlay (e.g.
muehlstein-ssrf-private) can be mounted into /data without publishing it.
"""
import bisect
import json
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_URL = "https://chicago-offline.github.io/ssrf-lite/codeplug.json"
DEFAULT_REFRESH_S = 6 * 3600
DEFAULT_TOLERANCE_KHZ = 0.0   # exact match only; see module docstring
FETCH_TIMEOUT_S = 15
MAX_NAMES = 3          # how many colliding names to surface per frequency


def _hz(mhz):
    """MHz float -> int Hz, or None. Tolerates strings and junk."""
    try:
        v = float(mhz)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return int(round(v * 1e6))


class NameIndex:
    """Frequency -> identity lookup. Thread-safe, fail-soft, never raises."""

    def __init__(self, path=None, url=DEFAULT_URL,
                 refresh_s=DEFAULT_REFRESH_S,
                 tolerance_khz=DEFAULT_TOLERANCE_KHZ):
        self.path = os.path.expanduser(path) if path else None
        self.url = url
        self.refresh_s = max(60, int(refresh_s or DEFAULT_REFRESH_S))
        self.tolerance_hz = int(float(tolerance_khz or 0) * 1000)
        self._lock = threading.Lock()
        self._by_hz = {}       # int Hz -> [entry, ...]
        self._sorted = []      # sorted int Hz keys, for nearest match
        self._loaded_at = 0.0
        self._source = None
        self._count = 0

    # ---------------------------------------------------------------- loading

    def _read_records(self):
        """Return (records, source_label). Local file wins over the URL."""
        if self.path and os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f), self.path
        if not self.url:
            return None, None
        req = urllib.request.Request(
            self.url, headers={"User-Agent": "ssrf-obs name-index"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8")), self.url

    @staticmethod
    def _build(records):
        by_hz = {}
        for rec in records or ():
            if not isinstance(rec, dict):
                continue
            name = (rec.get("name") or "").strip()
            call = (rec.get("callsign") or "").strip() or None
            if not name and not call:
                continue
            rx = _hz(rec.get("rx_mhz"))
            tx = _hz(rec.get("tx_mhz"))
            # output side first: that is what an observer normally hears
            for hz, side in ((rx, "out"), (tx, "in")):
                if hz is None or (side == "in" and hz == rx):
                    continue
                by_hz.setdefault(hz, []).append({
                    "name": name or call,
                    "callsign": call,
                    "service": rec.get("service") or None,
                    "mode": rec.get("mode") or None,
                    # Published tone/CC claims, carried through so the
                    # observers page can show what the record says a
                    # repeater uses. Claims, never measurements.
                    "ctcss": rec.get("ctcss"),
                    "dcs": rec.get("dcs"),
                    "color_code": rec.get("color_code"),
                    "side": side,
                })
        return by_hz

    def refresh(self, force=False):
        """(Re)load the index. Returns True if an index is loaded."""
        with self._lock:
            fresh = (time.time() - self._loaded_at) < self.refresh_s
            if self._by_hz and fresh and not force:
                return True
            try:
                records, source = self._read_records()
            except Exception as e:                       # noqa: BLE001
                # Stale data beats no data; only warn.
                log.warning("name index unavailable (%s): %s",
                            self.path or self.url, e)
                self._loaded_at = time.time()            # do not hammer it
                return bool(self._by_hz)
            if records is None:
                self._loaded_at = time.time()
                return bool(self._by_hz)
            try:
                by_hz = self._build(records)
            except Exception:                            # noqa: BLE001
                log.exception("name index malformed: %s", source)
                self._loaded_at = time.time()
                return bool(self._by_hz)
            self._by_hz = by_hz
            self._sorted = sorted(by_hz)
            self._loaded_at = time.time()
            self._source = source
            self._count = sum(len(v) for v in by_hz.values())
            log.info("name index: %d channel(s) over %d frequency(ies) from %s",
                     self._count, len(by_hz), source)
            return True

    # ---------------------------------------------------------------- lookup

    def _nearest_key(self, hz):
        """Closest indexed frequency within tolerance, else None."""
        if not self._sorted or self.tolerance_hz <= 0:
            return None
        i = bisect.bisect_left(self._sorted, hz)
        best, best_d = None, None
        for j in (i - 1, i):
            if 0 <= j < len(self._sorted):
                d = abs(self._sorted[j] - hz)
                if d <= self.tolerance_hz and (best_d is None or d < best_d):
                    best, best_d = self._sorted[j], d
        return best

    def lookup(self, freq_hz):
        """Identity for an observed frequency, or None."""
        if not freq_hz:
            return None
        try:
            hz = int(freq_hz)
        except (TypeError, ValueError):
            return None
        self.refresh()
        with self._lock:
            entries = self._by_hz.get(hz)
            exact = entries is not None
            if not entries:
                key = self._nearest_key(hz)
                if key is None:
                    return None
                entries = self._by_hz.get(key) or []
            if not entries:
                return None
            # output-side wins; then alphabetical for a stable page
            ranked = sorted(entries, key=lambda e: (e["side"] != "out",
                                                    e["name"].lower()))
            seen, names = set(), []
            for e in ranked:
                if e["name"].lower() not in seen:
                    seen.add(e["name"].lower())
                    names.append(e["name"])
            top = ranked[0]
            return {"name": top["name"],
                    "callsign": top["callsign"],
                    "service": top["service"],
                    "mode": top["mode"],
                    "ctcss": top.get("ctcss"),
                    "dcs": top.get("dcs"),
                    "color_code": top.get("color_code"),
                    "side": top["side"],
                    "exact": exact,
                    "names": names[:MAX_NAMES],
                    "matches": len(names)}

    # ---------------------------------------------------------------- status

    def status(self):
        with self._lock:
            return {"source": self._source,
                    "channels": self._count,
                    "frequencies": len(self._by_hz),
                    "loaded_at": self._loaded_at or None,
                    "tolerance_khz": self.tolerance_hz / 1000.0}


def from_config(cfg):
    """Build a NameIndex from config, or None when disabled/unconfigured.

    Never raises: a bad "names:" block degrades to "no names".
    """
    try:
        n = (cfg or {}).get("names")
        if n is None:
            n = {}
        if not isinstance(n, dict):
            log.warning("names: config is not a mapping; ignoring")
            return None
        if not n.get("enabled", True):
            return None
        idx = NameIndex(path=n.get("path"),
                        url=n.get("url", DEFAULT_URL),
                        refresh_s=n.get("refresh_s", DEFAULT_REFRESH_S),
                        tolerance_khz=n.get("tolerance_khz",
                                            DEFAULT_TOLERANCE_KHZ))
    except Exception:                                    # noqa: BLE001
        log.exception("names: could not build index; continuing without")
        return None
    idx.refresh(force=True)
    return idx
