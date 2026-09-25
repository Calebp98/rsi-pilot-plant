"""Append-only, hash-chained, signed interaction log.

Each entry:
  seq, received_at, client, payload (exact JSON string the client signed), client_sig,
  prev (hash of previous entry), hash = sha256(canonical record), logger_sig = ed25519(hash).

Clients sign their payload bytes with their own ed25519 key, so the logger cannot invent a
client's entries. The logger signs and chains every entry, so history cannot be edited
without breaking the chain. Anyone holding the public keys can re-verify everything.

Env / Fly secrets:
  CLIENT_KEYS      "name:base64pubkey,name2:base64pubkey"  (registered clients)
  READ_TOKEN       bearer token for reading entries
  ALLOWED_ORIGINS  comma-separated origins allowed to read from a browser (CORS)
"""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import model_gateway
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DATA = Path(os.environ.get("DATA_DIR", "/data"))
LOG = DATA / "log.jsonl"
KEY = DATA / "logger_ed25519.pem"
PORT = int(os.environ.get("PORT", "8080"))
READ_TOKEN = os.environ.get("READ_TOKEN", "")
ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}
MAX_BODY = 8 * 1024 * 1024
GENESIS = "0" * 64
HERE = Path(__file__).parent
SOURCE_SHA256 = {f: hashlib.sha256((HERE / f).read_bytes()).hexdigest() for f in ("app.py", "ssh_gateway.py", "model_gateway.py")}
STARTED = datetime.now(timezone.utc).isoformat(timespec="seconds")


def b64(b):
    return base64.b64encode(b).decode()


def canon(obj):
    """Canonical JSON: sorted keys, no whitespace, UTF-8. Records only hold strings and ints."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_key(path=KEY):
    DATA.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        k = Ed25519PrivateKey.generate()
        path.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
        path.chmod(0o600)
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def raw_pub(pub):
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


SIGNER = load_key()
LOGGER_PUB = b64(raw_pub(SIGNER.public_key()))
CLIENTS = {}
for item in os.environ.get("CLIENT_KEYS", "").split(","):
    if ":" in item:
        name, key = item.strip().split(":", 1)
        CLIENTS[name] = Ed25519PublicKey.from_public_bytes(base64.b64decode(key))

# The SSH gateway is a client like any other, with its own key, so its entries verify the same way.
GATEWAY_KEY = load_key(DATA / "gateway_ed25519.pem")
CLIENTS["gateway"] = GATEWAY_KEY.public_key()

# A log ID identifies one chain. A new session rotates the log: the client's signed "rotate" request is
# the last entry of the old chain, the old file is archived, and the new chain's first entry ("log_opened",
# gateway-signed) names the previous log ID and its final hash, so logs link into a verifiable sequence.
LOG_ID_FILE = DATA / "log_id"


def new_log_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + os.urandom(4).hex()


if not LOG_ID_FILE.exists():
    LOG_ID_FILE.write_text(new_log_id())
LOG_ID = LOG_ID_FILE.read_text().strip()
ARCHIVES = sorted(p.name for p in DATA.glob("archive-*.jsonl"))

lock = threading.Lock()
entries = [json.loads(line) for line in LOG.read_text().splitlines() if line] if LOG.exists() else []
last_client_seq = {}
for e in entries:
    p = json.loads(e["payload"])
    if p.get("kind") == "log_opened":  # carry replay protection across rotations
        for c, n in (p["data"].get("client_seqs") or {}).items():
            last_client_seq[c] = max(last_client_seq.get(c, 0), n)
    last_client_seq[e["client"]] = max(last_client_seq.get(e["client"], 0), p.get("client_seq", 0))


def head():
    return {"seq": entries[-1]["seq"], "hash": entries[-1]["hash"]} if entries else {"seq": 0, "hash": GENESIS}


def check_client(client, payload, client_sig):
    pub = CLIENTS.get(client)
    if pub is None:
        raise PermissionError(f"unknown client {client!r}")
    try:
        pub.verify(base64.b64decode(client_sig), payload.encode())
    except (InvalidSignature, ValueError):
        raise PermissionError("bad client signature")
    cseq = json.loads(payload).get("client_seq")
    if not isinstance(cseq, int):
        raise ValueError("payload.client_seq must be an int")
    return cseq


def _append_locked(client, payload, client_sig, cseq):
    """Chain, sign and durably write one entry. Caller holds `lock` and has checked the signature."""
    if cseq <= last_client_seq.get(client, 0):
        raise ValueError(f"client_seq {cseq} not above last {last_client_seq.get(client, 0)} (replay?)")
    h = head()
    record = {
        "seq": h["seq"] + 1,
        "received_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "client": client,
        "payload": payload,
        "client_sig": client_sig,
        "prev": h["hash"],
    }
    digest = hashlib.sha256(canon(record)).hexdigest()
    entry = {**record, "hash": digest, "logger_sig": b64(SIGNER.sign(bytes.fromhex(digest)))}
    with LOG.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    entries.append(entry)
    last_client_seq[client] = cseq
    return {"seq": entry["seq"], "hash": digest}


def append(client, payload, client_sig):
    """Verify the client's signature, then chain, sign and durably write the entry."""
    cseq = check_client(client, payload, client_sig)
    with lock:
        return _append_locked(client, payload, client_sig, cseq)


def _gateway_payload(kind, data):
    payload = json.dumps({"client_seq": last_client_seq.get("gateway", 0) + 1,
                          "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                          "kind": kind, "data": data}, ensure_ascii=False, separators=(",", ":"))
    return payload, b64(GATEWAY_KEY.sign(payload.encode())), last_client_seq.get("gateway", 0) + 1


def gateway_log(kind, data):
    with lock:
        payload, sig, cseq = _gateway_payload(kind, data)
        return _append_locked("gateway", payload, sig, cseq)


def rotate(client, payload, client_sig):
    """Close this chain with the client's signed rotate request and open a new one linked to it."""
    global LOG_ID, ARCHIVES
    cseq = check_client(client, payload, client_sig)
    req = json.loads(payload)
    if req.get("kind") != "rotate":
        raise ValueError("payload.kind must be 'rotate'")
    with lock:
        closing = _append_locked(client, payload, client_sig, cseq)
        closed_id, closed_count = LOG_ID, len(entries)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = f"archive-{ts}-{closed_id}.jsonl"
        LOG.rename(DATA / archive)
        LOG_ID = new_log_id()
        LOG_ID_FILE.write_text(LOG_ID)
        ARCHIVES = sorted(p.name for p in DATA.glob("archive-*.jsonl"))
        entries.clear()
        data = {"log_id": LOG_ID, "prev_log_id": closed_id, "prev_head": closing, "prev_count": closed_count,
                "prev_archive": archive, "session": (req.get("data") or {}).get("session"),
                "manifest": (req.get("data") or {}).get("manifest"), "client_seqs": dict(last_client_seq)}
        payload2, sig2, cseq2 = _gateway_payload("log_opened", data)
        opened = _append_locked("gateway", payload2, sig2, cseq2)
    return {"closed": {"log_id": closed_id, "head": closing, "archive": archive},
            "opened": {"log_id": LOG_ID, "head": opened}}


gateway = None


def info():
    return {
        "service": "lucid-logger",
        "log_id": LOG_ID,
        "archives": ARCHIVES,
        "started_at": STARTED,
        "source_sha256": SOURCE_SHA256,
        "image_ref": os.environ.get("FLY_IMAGE_REF"),
        "machine_id": os.environ.get("FLY_MACHINE_ID"),
        "region": os.environ.get("FLY_REGION"),
        "logger_pubkey": LOGGER_PUB,
        "clients": {n: b64(raw_pub(k)) for n, k in CLIENTS.items()},
        "head": head(),
        "count": len(entries),
        "ssh": gateway.info() if gateway else None,
        "model": model_gateway.info(),
        "canonical_json": "json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False)",
    }


class Handler(BaseHTTPRequestHandler):
    def cors(self):
        origin = self.headers.get("Origin")
        if origin and origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Vary", "Origin")

    def send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        auth = self.headers.get("Authorization", "")
        return bool(READ_TOKEN) and hmac.compare_digest(auth, f"Bearer {READ_TOKEN}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/v1/model/"):
            return model_gateway.relay(self, self.path[len("/v1/model/"):], gateway_log)
        if url.path == "/healthz":
            return self.send(200, {"ok": True})
        if url.path == "/v1/info":
            return self.send(200, info())
        if url.path == "/v1/head":
            return self.send(200, head())
        if url.path == "/v1/entries":
            if not self.authed():
                return self.send(401, {"error": "read token required"})
            q = parse_qs(url.query)
            after = int(q.get("after", ["0"])[0])
            limit = min(int(q.get("limit", ["500"])[0]), 2000)
            return self.send(200, {"entries": entries[after:after + limit], "head": head()})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/v1/model/"):
            return model_gateway.relay(self, self.path[len("/v1/model/"):], gateway_log)
        if path not in ("/v1/append", "/v1/rotate"):
            return self.send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            return self.send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length))
            fn = rotate if path == "/v1/rotate" else append
            return self.send(200, fn(body["client"], body["payload"], body["client_sig"]))
        except PermissionError as e:
            return self.send(403, {"error": str(e)})
        except (ValueError, KeyError, TypeError) as e:
            return self.send(400, {"error": str(e)})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    import asyncio
    from ssh_gateway import Gateway
    gateway = Gateway(DATA, gateway_log)
    model_gateway.start()
    threading.Thread(target=gateway.start, args=(asyncio.new_event_loop(),), daemon=True).start()
    print(f"lucid-logger up: {len(entries)} entries, head {head()}, pubkey {LOGGER_PUB}, "
          f"clients {sorted(CLIENTS)}, ssh targets {sorted(gateway.targets)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
