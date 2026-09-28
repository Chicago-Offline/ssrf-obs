"""Envelope verification — mirrors rf-survey's station.canonical()."""
import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def verify(envelope, pubkey_b64):
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        pub.verify(base64.b64decode(envelope["sig"]),
                   canonical(envelope["batch"]))
        return True
    except Exception:
        return False
