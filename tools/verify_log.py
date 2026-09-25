"""Verify a pilot-plant log from your own machine, without trusting the dashboard or the logger.

  uv run --with cryptography python tools/verify_log.py --logger http://127.0.0.1:8081 --token-file state/scaffold/read_token

Checks every entry: seq has no gaps, prev links to the previous hash, hash = sha256(canonical record), the logger's
ed25519 signature over the hash, the client's ed25519 signature over the payload bytes, and client_seq increasing
per client. Also checks the fetched chain ends at the head /v1/info publishes.

Keys come from /v1/info. On the first run they're pinned to --pins; later runs flag any key that changed.
That pin is the part you must compare out of band (e.g. /v1/info fetched from a second machine).
Exit status 0 = everything verified.
"""

import argparse
import base64
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

GENESIS = "0" * 64


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def get(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_entries(base, token):
    out = []
    while True:
        page = get(f"{base}/v1/entries?after={len(out)}&limit=500", token)
        out += page["entries"]
        if len(page["entries"]) < 500:
            return out


def verify(info, entries):
    """Return a list of problems (empty = verified)."""
    problems = []
    keys = {"logger": info["logger_pubkey"], **info["clients"]}
    logger_pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(keys["logger"]))
    prev, last_cseq = GENESIS, {}
    for i, e in enumerate(entries, 1):
        record = {k: e[k] for k in ("seq", "received_at", "client", "payload", "client_sig", "prev")}
        errs = []
        if e["seq"] != i:
            errs.append("seq gap")
        if e["prev"] != prev:
            errs.append("prev link broken")
        if hashlib.sha256(canon(record)).hexdigest() != e["hash"]:
            errs.append("hash mismatch")
        try:
            logger_pub.verify(base64.b64decode(e["logger_sig"]), bytes.fromhex(e["hash"]))
        except (InvalidSignature, ValueError):
            errs.append("bad logger signature")
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(keys[e["client"]])).verify(
                base64.b64decode(e["client_sig"]), e["payload"].encode())
        except (InvalidSignature, KeyError, TypeError, ValueError):
            errs.append(f"bad client signature ({e['client']})")
        try:
            payload = json.loads(e["payload"])
        except ValueError:
            payload = {}
            errs.append("payload is not valid JSON")
        cseq = payload.get("client_seq")
        # after a rotation, #1 (log_opened) carries the previous chain's client_seqs; replay protection continues
        if i == 1 and payload.get("kind") == "log_opened":
            last_cseq.update({c: n for c, n in ((payload.get("data") or {}).get("client_seqs") or {}).items()
                              if c != "gateway"})
        elif not isinstance(cseq, int) or cseq <= last_cseq.get(e["client"], 0):
            errs.append("client_seq not increasing")
        if isinstance(cseq, int):
            last_cseq[e["client"]] = cseq
        if errs:
            problems.append(f"#{e['seq']}: {', '.join(errs)}")
        prev = e["hash"]
    if info["head"]["seq"] != len(entries) or info["head"]["hash"] != (entries[-1]["hash"] if entries else GENESIS):
        problems.append(f"published head {info['head']} doesn't match the fetched entries")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--logger", required=True, help="logger base URL")
    ap.add_argument("--token", help="read token")
    ap.add_argument("--token-file", help="file holding the read token")
    ap.add_argument("--pins", default="log_pins.json", help="where to pin keys (default ./log_pins.json)")
    a = ap.parse_args()
    base = a.logger.rstrip("/")
    token = a.token or (Path(a.token_file).read_text().strip() if a.token_file else None)
    if not token:
        sys.exit("need --token or --token-file")

    info = get(f"{base}/v1/info")
    entries = fetch_entries(base, token)
    problems = []
    keys = {"logger": info["logger_pubkey"], **info["clients"]}
    pins = Path(a.pins)
    if pins.exists():
        pinned = json.loads(pins.read_text())
        problems += [f"key for {n} changed since pinning" for n, k in keys.items() if n in pinned and pinned[n] != k]
    else:
        pins.write_text(json.dumps(keys, indent=1) + "\n")
        print(f"pinned {len(keys)} keys to {pins} (first run: compare them with /v1/info from another machine)")
    problems += verify(info, entries)

    print(f"log {info['log_id']}: {len(entries)} entries, head #{info['head']['seq']}")
    for p in problems:
        print("  FAIL", p)
    if problems:
        sys.exit(1)
    print("  ok   hashes, links, logger and client signatures, client_seq order, head")


if __name__ == "__main__":
    main()
