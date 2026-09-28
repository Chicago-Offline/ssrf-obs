import argparse
import json
import logging
import os
import sys
import threading

import yaml

from . import registry as registry_mod, rules
from .db import DB

DEFAULT_CONFIG = os.environ.get("SSRF_OBS_CONFIG", "/data/config.yml")


def load_cfg(path):
    with open(os.path.expanduser(path)) as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("db", "/data/observations.db")
    cfg.setdefault("stations", "/data/stations.yml")
    return cfg


def cmd_run(args, cfg):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from . import service, web
    reg = registry_mod.load(cfg["stations"])
    logging.info("registry: %d station(s): %s", len(reg), ", ".join(sorted(reg)))
    db = DB(cfg["db"])
    if (cfg.get("web") or {}).get("enabled", True):
        threading.Thread(target=web.serve, args=(cfg, reg, db),
                         daemon=True, name="web").start()
    service.run(cfg, reg, db)
    return 0


def cmd_serve(args, cfg):
    """Web surface only -- no MQTT subscriber."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from . import web
    reg = registry_mod.load(cfg["stations"])
    web.serve(cfg, reg, DB(cfg["db"]))
    return 0


def cmd_report(args, cfg):
    db = DB(cfg["db"])
    freqs = sorted({r[0] for r in db.db.execute(
        "SELECT DISTINCT freq_hz FROM observations").fetchall()})
    out = {}
    for f in freqs:
        st = rules.channel_status(db.observation_rows(f))
        if st:
            out[f"{f/1e6:.4f} MHz"] = st
    print(json.dumps(out, indent=2, default=str) if out
          else "no observations yet")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="ssrf-obs",
        description="rf-survey aggregator: verify, store, grade evidence")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="subscribe and ingest forever (+ web)")
    sub.add_parser("report", help="per-channel verification rollup")
    sub.add_parser("serve", help="observers page + JSON feed only")
    args = p.parse_args(argv)
    cfg = load_cfg(args.config)
    return {"run": cmd_run, "report": cmd_report,
            "serve": cmd_serve}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
