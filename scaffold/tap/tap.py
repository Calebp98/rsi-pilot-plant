"""Recording tap: OpenCode -> tap (127.0.0.1:3300) -> logger model gateway (HTTPS) -> tinfoil-proxy -> Tinfoil.

The tap is a second, independent witness: the logger's model gateway logs each exchange itself (gateway-signed),
and the tap logs what the Sprite saw (scaffold-signed), so the two records can be compared.

Streams responses through unchanged and, after each exchange, sends a signed record
(request body, response body, status, timing; never the Authorization header) to the logger.
Records are signed with this Sprite's ed25519 key (~/.config/tap/ed25519.pem), so the logger
can't forge them. If the logger is unreachable, records are spooled to disk and retried in order.

Config (~/.config/tap/config.json): {"logger_url": "https://<logger>.fly.dev", "client": "scaffold"}
(http:// is accepted for a logger on a private network, e.g. http://logger:8080 in docker compose.)
Model upstream: {logger_url}/v1/model/ (OpenCode's Authorization header, the model token, is passed through
unchanged and never logged).
"""

import base64
import http.client
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CFG_DIR = Path.home() / ".config/tap"
STATE = Path.home() / ".tap"
SPOOL = STATE / "spool.jsonl"
SEQ_FILE = STATE / "client_seq"
LISTEN = ("127.0.0.1", 3300)
MAX_CAPTURE = 4 * 1024 * 1024
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection", "content-length"}

CFG = json.loads((CFG_DIR / "config.json").read_text())
UP = urllib.parse.urlparse(CFG["logger_url"])
KEY_PATH = CFG_DIR / "ed25519.pem"
if not KEY_PATH.exists():
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    k = Ed25519PrivateKey.generate()
    KEY_PATH.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    KEY_PATH.chmod(0o600)
KEY = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
PUB = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

STATE.mkdir(parents=True, exist_ok=True)
seq_lock = threading.Lock()
outbox = queue.Queue()
inflight = threading.Event()  # set while the sender holds a record it hasn't delivered or spooled
status = {"sent": 0, "spooled": 0, "last_error": None, "last_sent_at": None}


def next_seq():
    with seq_lock:
        n = int(SEQ_FILE.read_text()) + 1 if SEQ_FILE.exists() else 1
        SEQ_FILE.write_text(str(n))
        return n


def sign_record(kind, data):
    """Payload is an exact JSON string; the signature covers its UTF-8 bytes."""
    payload = json.dumps({"client_seq": next_seq(), "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                          "kind": kind, "data": data}, ensure_ascii=False, separators=(",", ":"))
    return {"client": CFG["client"], "payload": payload,
            "client_sig": base64.b64encode(KEY.sign(payload.encode())).decode()}


def post(record):
    req = urllib.request.Request(CFG["logger_url"].rstrip("/") + "/v1/append", data=json.dumps(record).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def sender():
    """Deliver records in order: spooled ones first, then new ones. Spool on failure."""
    while True:
        record = outbox.get()
        inflight.set()
        with seq_lock:
            spooled = [json.loads(l) for l in SPOOL.read_text().splitlines() if l] if SPOOL.exists() else []
        pending = spooled + [record]
        delivered = 0
        for rec in pending:
            try:
                post(rec)
                delivered += 1
                status["sent"] += 1
                status["last_sent_at"] = time.time()
                status["last_error"] = None
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:200]
                if e.code == 400 and "not above last" in body:
                    delivered += 1  # already stored by the logger on an earlier attempt
                    continue
                status["last_error"] = f"{e.code} {body}"
                break
            except Exception as e:
                status["last_error"] = str(e)
                break
        rest = pending[delivered:]
        with seq_lock:
            SPOOL.write_text("".join(json.dumps(r) + "\n" for r in rest))
        status["spooled"] = len(rest)
        inflight.clear()
        if rest:
            time.sleep(5)


class Tap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/_tap/status":
            body = json.dumps({**status, "pubkey": PUB, "client": CFG["client"], "logger_url": CFG["logger_url"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self.relay()

    def do_POST(self):
        if self.path == "/_tap/rotate":
            return self.rotate()
        self.relay()

    def rotate(self):
        """New session: sign a rotate request (session id + manifest) so the logger closes this chain and opens a
        linked one. Refused while records are still queued, so nothing lands in the wrong log."""
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not outbox.empty() or inflight.is_set() or (SPOOL.exists() and SPOOL.read_text().strip()):
            return self.reply(409, {"error": "records still waiting to be delivered to the logger; try again shortly"})
        record = sign_record("rotate", {"session": body.get("session"), "manifest": body.get("manifest")})
        req = urllib.request.Request(CFG["logger_url"].rstrip("/") + "/v1/rotate", data=json.dumps(record).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return self.reply(200, json.load(r))
        except urllib.error.HTTPError as e:
            return self.reply(e.code, {"error": e.read().decode(errors="replace")[:300]})
        except Exception as e:
            return self.reply(502, {"error": str(e)})

    def reply(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def relay(self):
        started = time.time()
        length = int(self.headers.get("Content-Length", 0))
        req_body = self.rfile.read(length) if length else b""
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "host"}
        if UP.scheme == "http":
            up = http.client.HTTPConnection(UP.hostname, UP.port or 80, timeout=600)
        else:
            up = http.client.HTTPSConnection(UP.hostname, UP.port or 443, timeout=600)
        up_path = "/v1/model/" + self.path.removeprefix("/v1/").lstrip("/")
        try:
            up.request(self.command, up_path, body=req_body or None, headers=headers)
            resp = up.getresponse()
        except Exception as e:
            self.send_error(502, f"upstream: {e}")
            return self.record(started, req_body, 502, b"", str(e))

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        captured, truncated = bytearray(), False
        try:
            while chunk := resp.read1(65536):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                if len(captured) < MAX_CAPTURE:
                    captured += chunk[:MAX_CAPTURE - len(captured)]
                else:
                    truncated = True
            self.wfile.write(b"0\r\n\r\n")
            err = None
        except Exception as e:
            err = f"client disconnected: {e}"
        finally:
            up.close()
        self.record(started, req_body, resp.status, bytes(captured), err, truncated,
                    resp.getheader("tinfoil-enclave"))

    def record(self, started, req_body, code, resp_body, error=None, truncated=False, enclave=None):
        outbox.put(sign_record("model_call", {
            "method": self.command,
            "path": self.path,
            "status": code,
            "duration_ms": int((time.time() - started) * 1000),
            "request_body": req_body.decode(errors="replace"),
            "response_body": resp_body.decode(errors="replace"),
            "response_truncated": truncated,
            "enclave": enclave,
            "error": error,
        }))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"tap: {LISTEN} -> {CFG['logger_url']}/v1/model/, client={CFG['client']}, pubkey={PUB}", flush=True)
    threading.Thread(target=sender, daemon=True).start()
    if SPOOL.exists() and SPOOL.read_text().strip():
        outbox.put(sign_record("tap_start", {"note": "tap restarted with spooled records"}))
    else:
        outbox.put(sign_record("tap_start", {"pubkey": PUB}))
    ThreadingHTTPServer(LISTEN, Tap).serve_forever()
