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

## Run

```bash
pip install .
cp config.example.yml data/config.yml   # point at your broker
cp stations.example.yml data/stations.yml
ssrf-obs --config data/config.yml run
ssrf-obs --config data/config.yml report
```

Or `docker compose up -d` on a host with the `chicagooffline-net` network.
