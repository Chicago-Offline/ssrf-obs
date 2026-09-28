# ssrf-obs

**🌐 Part of the [rf-survey](https://rfsurvey.chicagooffline.com/) network** — see [NETWORK.md](https://github.com/Chicago-Offline/rf-survey/blob/main/NETWORK.md).

The aggregator for distributed [rf-survey](https://github.com/Chicago-Offline/rf-survey)
stations: subscribes to `rfsurvey/obs/#`, verifies Ed25519 signatures against a
manually-enrolled station registry, stores append-only evidence in SQLite, and
derives the V0–V3 validation ladder that backs
[ssrf-lite](https://github.com/Chicago-Offline/ssrf-lite) verification.

- **Idempotent** — batches dedupe by UUID; station replays are no-ops.
- **Policy lives here** — promotion rules (verified / flagged / stale) are
  aggregator-side, never on the stations.
- **Evidence, deliberately outside ssrf-lite** — the git repo stays curated;
  this DB holds the observations behind it.

## Naming what was heard

The evidence DB only ever records a *frequency*. Identity is curated in
[ssrf-lite](https://github.com/Chicago-Offline/ssrf-lite), so the observers page
resolves names through an optional, additive index built from ssrf-lite's
published channel export:

```yaml
names:
  enabled: true
  # path: /data/names.json   # local/private overlay, tried before the URL
  url: https://chicago-offline.github.io/ssrf-lite/codeplug.json
  refresh_s: 21600
  tolerance_khz: 0           # exact match only — see below
```

The `station / system` column then reads as who is **known to use** the channel.
It is not a claim that the transmitter was identified on the air — that is what
the tier column is for. If the index is unreachable or removed, every channel
renders as a bare frequency again and grading is unaffected.

**Matching is exact, and nearest-match is off by default.** rf-survey already
snaps a hit to the channel raster when the measurement can resolve one, and
records that verdict as `channel_snap` in the observation meta. A snapped
frequency is on-grid, so a correct ssrf-lite entry matches exactly. For an
*unsnapped* hit the frequency is a bin centre rather than a channel, and
proximity to a raster point is not evidence: measured against prod, a 2.5 kHz
window named 9 of 59 unsnapped frequencies — all guesses, and it collapsed three
adjacent bins (159.6586 / 159.6591 / 159.6595) onto one channel, manufacturing
three sightings from one signal. Those rows are reported as `(unresolved)`
instead, which is a narrower-bin problem, not a missing-name problem.

`/names.json` reports index source, size and load time.

## Run

```bash
pip install .
cp config.example.yml data/config.yml   # point at your broker
cp stations.example.yml data/stations.yml
ssrf-obs --config data/config.yml run
ssrf-obs --config data/config.yml report
```

Or `docker compose up -d` on a host with the `chicagooffline-net` network.
