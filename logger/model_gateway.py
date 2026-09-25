"""Model gateway: the only route from the agent to Tinfoil.

  agent --HTTPS /v1/model/* (model token)--> gateway --> tinfoil-proxy (127.0.0.1:3301, verifies + pins) --> Tinfoil

The Tinfoil API key lives only here (TINFOIL_API_KEY). Clients authenticate with a model token that is
useless anywhere else (MODEL_CLIENT_TOKENS "name:token,..."). Each exchange is streamed through unchanged and
then logged as a gateway-signed "model_call" entry (request and response bodies; never either key).

tinfoil-proxy runs as a supervised child process. Tinfoil's SDK verifier also runs here periodically, so the
attestation result is published in /v1/info for anyone to inspect.
"""

import hmac
import http.client
import os
import subprocess
import threading
import time

PROXY_BIN = os.environ.get("TINFOIL_PROXY_BIN", "/usr/local/bin/tinfoil-proxy")
UPSTREAM = ("127.0.0.1", int(os.environ.get("TINFOIL_PROXY_PORT", "3301")))
TINFOIL_KEY = os.environ.get("TINFOIL_API_KEY", "")
MAX_CAPTURE = 4 * 1024 * 1024
VERIFY_EVERY = 600
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection",
       "content-length", "authorization", "host"}

TOKENS = {}
for item in os.environ.get("MODEL_CLIENT_TOKENS", "").split(","):
    if ":" in item:
        name, tok = item.strip().split(":", 1)
        TOKENS[name] = tok

state = {"proxy": {"running": False, "restarts": 0, "enclave": None, "log": []},
         "verification": None, "verify_error": None, "verified_at": None}


def client_for(auth_header):
    tok = (auth_header or "").removeprefix("Bearer ").strip()
    for name, t in TOKENS.items():
        if tok and hmac.compare_digest(tok, t):
            return name
    return None


def supervise_proxy():
    """Keep tinfoil-proxy running; remember its last log lines (it logs the enclave it verified)."""
    if not os.path.exists(PROXY_BIN):
        state["proxy"]["log"] = [f"{PROXY_BIN} missing"]
        return
    while True:
        p = subprocess.Popen([PROXY_BIN, "-p", str(UPSTREAM[1]), "-b", UPSTREAM[0]],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        state["proxy"]["running"] = True
        for line in p.stdout:
            line = line.strip()
            state["proxy"]["log"] = (state["proxy"]["log"] + [line])[-30:]
            if "enclave_host=" in line and "starting HTTP proxy" in line:
                state["proxy"]["enclave"] = line.split("enclave_host=")[-1].split()[0]
            print("tinfoil-proxy:", line, flush=True)
        p.wait()
        state["proxy"]["running"] = False
        state["proxy"]["restarts"] += 1
        time.sleep(3)


def verify_loop():
    """Tinfoil's own SDK verifier, independent of the proxy, so the result can be published."""
    while True:
        try:
            from tinfoil import TinfoilAI
            doc = TinfoilAI(api_key=TINFOIL_KEY or "x").get_verification_document()
            d = doc.to_dict() if doc else {}
            state["verification"] = {
                "ok": d.get("securityVerified"),
                "steps": {k: s.get("status") for k, s in (d.get("steps") or {}).items()},
                "repo": d.get("configRepo"), "release": d.get("releaseTag"), "release_digest": d.get("releaseDigest"),
                "host": d.get("enclaveHost"),
                "platform": ((d.get("enclaveMeasurement") or {}).get("measurement") or {}).get("type", "").split("/predicate/")[-1],
                "measurement": d.get("enclaveFingerprint"), "tls_key": d.get("tlsPublicKey"), "hpke_key": d.get("hpkePublicKey"),
                "verifier": (d.get("verifier") or {}).get("version"), "at": d.get("verifiedAt"),
            }
            state["verify_error"] = None
        except Exception as e:
            state["verify_error"] = f"{type(e).__name__}: {e}"
        state["verified_at"] = time.time()
        time.sleep(VERIFY_EVERY)


def start():
    threading.Thread(target=supervise_proxy, daemon=True).start()
    threading.Thread(target=verify_loop, daemon=True).start()


def info():
    return {"clients": sorted(TOKENS), "key_configured": bool(TINFOIL_KEY),
            "proxy": {k: v for k, v in state["proxy"].items() if k != "log"}, "proxy_log": state["proxy"]["log"][-10:],
            "verification": state["verification"], "verify_error": state["verify_error"]}


def relay(handler, sub_path, log_entry):
    """Serve one /v1/model/* request on `handler` (a BaseHTTPRequestHandler), then log it."""
    client = client_for(handler.headers.get("Authorization"))
    if client is None:
        body = b'{"error":"model token required"}'
        handler.send_response(401)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        return handler.wfile.write(body)
    started = time.time()
    length = int(handler.headers.get("Content-Length", 0))
    req_body = handler.rfile.read(length) if length else b""
    headers = {k: v for k, v in handler.headers.items() if k.lower() not in HOP}
    headers["Authorization"] = f"Bearer {TINFOIL_KEY}"
    path = "/v1/" + sub_path
    up = http.client.HTTPConnection(*UPSTREAM, timeout=600)
    status, captured, truncated, enclave, error = 502, bytearray(), False, None, None
    try:
        up.request(handler.command, path, body=req_body or None, headers=headers)
        resp = up.getresponse()
        status, enclave = resp.status, resp.getheader("tinfoil-enclave")
        handler.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                handler.send_header(k, v)
        handler.send_header("Connection", "close")
        handler.end_headers()
        while chunk := resp.read1(65536):
            handler.wfile.write(chunk)
            handler.wfile.flush()
            if len(captured) < MAX_CAPTURE:
                captured += chunk[:MAX_CAPTURE - len(captured)]
            else:
                truncated = True
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        if status == 502:
            try:
                handler.send_error(502, "upstream: " + error)
            except Exception:
                pass
    finally:
        up.close()
    log_entry("model_call", {
        "client": client, "via": "model_gateway", "method": handler.command, "path": path, "status": status,
        "duration_ms": int((time.time() - started) * 1000),
        "request_body": req_body.decode(errors="replace"), "response_body": captured.decode(errors="replace"),
        "response_truncated": truncated, "enclave": enclave, "error": error,
    })
    handler.close_connection = True
