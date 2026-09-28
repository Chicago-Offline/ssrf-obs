"""Station registry: manual enrollment, small trusted set (NETWORK.md).

stations.yml:
  bowmanville:
    pubkey: <base64 Ed25519 raw public key>   # from: survey station-init
    note: attic discone, R820T2 x2
"""
import os
import yaml


def load(path):
    with open(os.path.expanduser(path)) as f:
        data = yaml.safe_load(f) or {}
    return {sid: rec["pubkey"] for sid, rec in data.items() if rec.get("pubkey")}
