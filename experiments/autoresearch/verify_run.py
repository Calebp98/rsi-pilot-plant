"""Independent check of an autoresearch run on the pilot plant, from your Mac.

  PLANT_LOGGER_URL=https://<logger>.fly.dev \
  uv run --with cryptography --with zstandard python verify_run.py [--repo ~/workspace] [--pod-check]

PLANT_EXEC is the command prefix that runs a shell command on the scaffold (default: the Sprite CLI for a
Sprite named tinfoil-scaffold; for docker compose use "docker compose exec -T -u agent scaffold").

Checks, without trusting the Sprite's dashboard:
  1. The log chain: seq, prev links, sha256(canonical record), logger signature, client signatures,
     client_seq increasing. Keys are pinned in verify_pins.json on first run and compared after that.
  2. Model calls: tap vs gateway records pair up by request body with identical responses; token use and
     cost per model from the logged usage (H4).
  3. Experiments (H1): every results.tsv row has a logged upload of exactly that commit's train.py,
     a launch after it, and logged output whose val_bpb equals the tsv value.
  4. Rules (H3): the agent's commits (branches beyond master) only change train.py; no logged command writes prepare.py, pyproject.toml or
     uv.lock; each result trained ~300s; kept val_bpb only goes down.
  --pod-check also hashes the files on the GPU (one logged command) and compares them with the log.

The read token stays on the Sprite: entries are fetched by running curl there.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

LOGGER = os.environ.get("PLANT_LOGGER_URL", "").rstrip("/")
SPRITE = shlex.split(os.environ.get("PLANT_EXEC", "sprite -s tinfoil-scaffold exec --"))
PINS = Path(__file__).with_name("verify_pins.json")
GENESIS = "0" * 64
# USD per 1M tokens (input, output), from Tinfoil's /v1/models on 2026-09-23
PRICES = {"gpt-oss-120b": (0.15, 0.60), "gemma4-31b": (0.40, 1.00), "glm-5-3-flash": (0.40, 1.25),
          "deepseek-v4-1-flash": (0.65, 1.45), "llama3-3-70b": (1.75, 2.75), "glm-5-3": (1.80, 5.75),
          "kimi-k3": (4.00, 20.00)}
# the reference files the agent must not change (karpathy/autoresearch @ 228791f)
FROZEN = ("prepare.py", "pyproject.toml", "uv.lock")

problems = []


def bad(msg):
    problems.append(msg)
    print("  FAIL", msg)


def ok(msg):
    print("  ok  ", msg)


def sprite(cmd):
    return subprocess.run(SPRITE + ["bash", "-lc", cmd], capture_output=True, text=True, check=True).stdout


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def fetch_entries():
    out, after = [], 0
    while True:
        page = json.loads(sprite('curl -sf -H "Authorization: Bearer $(cat ~/.config/tap/read_token)" '
                                 f'"{LOGGER}/v1/entries?after={after}&limit=500"'))
        out += page["entries"]
        if len(page["entries"]) < 500:
            return out
        after = out[-1]["seq"]


def check_chain(info, entries):
    print("1. Log chain", info["log_id"])
    keys = {"logger": info["logger_pubkey"], **info["clients"]}
    if PINS.exists():
        pinned = json.loads(PINS.read_text())
        for name, k in keys.items():
            if name in pinned and pinned[name] != k:
                bad(f"key for {name} changed since pinning ({PINS.name})")
        ok(f"keys match {PINS.name}" if not problems else "key check done")
    else:
        PINS.write_text(json.dumps(keys, indent=1) + "\n")
        ok(f"pinned {len(keys)} keys to {PINS.name} (first run: compare them with /v1/info on another machine)")
    logger_pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(keys["logger"]))
    prev, last_cseq, n_bad = GENESIS, {}, 0
    for i, e in enumerate(entries, 1):
        record = {k: e[k] for k in ("seq", "received_at", "client", "payload", "client_sig", "prev")}
        errs = []
        if e["seq"] != i:
            errs.append("seq gap")
        if i > 1 and e["prev"] != prev:
            errs.append("prev link broken")
        if hashlib.sha256(canon(record)).hexdigest() != e["hash"]:
            errs.append("hash mismatch")
        try:
            logger_pub.verify(base64.b64decode(e["logger_sig"]), bytes.fromhex(e["hash"]))
        except InvalidSignature:
            errs.append("bad logger signature")
        ck = keys.get(e["client"])
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(ck)).verify(base64.b64decode(e["client_sig"]), e["payload"].encode())
        except (InvalidSignature, TypeError, ValueError):
            errs.append(f"bad client signature ({e['client']})")
        cseq = json.loads(e["payload"]).get("client_seq")
        if cseq <= last_cseq.get(e["client"], 0):
            errs.append("client_seq not increasing")
        last_cseq[e["client"]] = cseq
        if errs:
            n_bad += 1
            bad(f"#{e['seq']}: {', '.join(errs)}")
        prev = e["hash"]
    if entries and entries[0]["seq"] == 1:
        p0 = json.loads(entries[0]["payload"])
        if p0["kind"] != "log_opened":
            bad("#1 is not log_opened")
    if info["head"]["seq"] != len(entries) or (entries and info["head"]["hash"] != entries[-1]["hash"]):
        bad(f"published head {info['head']} doesn't match fetched entries")
    if not n_bad:
        ok(f"{len(entries)} entries: hashes, links and signatures all verify; head matches /v1/info")


def usage_of(resp):
    """Token usage and model from a logged response (JSON or SSE stream)."""
    model, usage = None, None
    for chunk in [resp] + [l[6:] for l in resp.splitlines() if l.startswith("data: ")]:
        try:
            j = json.loads(chunk)
        except ValueError:
            continue
        model = j.get("model") or model
        usage = j.get("usage") or usage
    return model, usage


def body(d):
    """Response body as text. The tap logs bodies as received, so an error reply that came back zstd-compressed
    (Fly's edge compresses non-streamed responses) is logged compressed; the gateway logs it decoded."""
    b = d.get("response_body") or ""
    raw = b.encode("latin-1", "replace") if b.startswith("(\ufffd/\ufffd") or b[:4] == "(\xb5/\xfd" else None
    if raw is not None:
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompressobj().decompress(raw).decode(errors="replace")
        except Exception:
            return b
    return b


def check_model_calls(payloads):
    print("2. Model calls")
    gw = [p["data"] for p in payloads if p["kind"] == "model_call" and p["data"].get("via") == "model_gateway"]
    tap = [p["data"] for p in payloads if p["kind"] == "model_call" and p["data"].get("via") != "model_gateway"]
    unmatched_tap = list(tap)
    differ = gw_only = undecodable = 0
    for g in gw:
        m = next((t for t in unmatched_tap if t.get("request_body") == g.get("request_body")), None)
        if m is None:
            gw_only += 1
            continue
        unmatched_tap.remove(m)
        if m.get("status") != g.get("status"):
            differ += 1
        elif body(m) != body(g):
            # a tap body that was logged compressed but can't be decoded back (replacement chars) only counts
            # as a match if the status agrees and it is an error reply; say so rather than hide it
            if "\ufffd" in (m.get("response_body") or "")[:8] and m.get("status", 200) >= 400:
                undecodable += 1
            else:
                differ += 1
    matched = len(gw) - gw_only
    # a call in flight can show as gateway-only for a second (the tap writes its record after the gateway); re-run
    (ok if not (differ or gw_only or unmatched_tap) else bad)(
        f"tap vs gateway: {matched} matched, {differ} differ, {gw_only} gateway-only, {len(unmatched_tap)} tap-only")
    if undecodable:
        print(f"  note  {undecodable} tap records of error replies were logged compressed and lost bytes in transit "
              "(status matches the gateway's); fix the tap to log decoded bodies")
    totals = {}
    for g in gw:
        model, u = usage_of(g.get("response_body") or "")
        if not u:
            continue
        t = totals.setdefault(model or "?", [0, 0, 0])
        t[0] += 1; t[1] += u.get("prompt_tokens", 0); t[2] += u.get("completion_tokens", 0)
    cost = 0.0
    for model, (n, pin, pout) in totals.items():
        pi, po = PRICES.get(model, (0, 0))
        c = pin / 1e6 * pi + pout / 1e6 * po
        cost += c
        ok(f"{model}: {n} calls, {pin:,} prompt + {pout:,} completion tokens, ${c:.2f}")
    return cost


def check_experiments(repo, entries, payloads):
    print("3. Experiments vs results.tsv", repo)
    tsv = sprite(f"cat {repo}/results.tsv").strip().splitlines()
    rows = [dict(zip(tsv[0].split("\t"), r.split("\t"))) for r in tsv[1:] if r.strip()]
    execs = {e["seq"]: p for e, p in zip(entries, payloads) if p["kind"] == "ssh_exec"}
    results = [(e["seq"], p["data"]) for e, p in zip(entries, payloads) if p["kind"] == "ssh_result"]
    uploads = [(s, hashlib.sha256(d["stdin"].encode()).hexdigest()) for s, d in results
               if "cat > /root/autoresearch/train.py" in execs.get(d["exec_seq"], {}).get("data", {}).get("command", "")]
    launches = [s for s, p in execs.items() if re.search(r"uv run train\.py", p["data"]["command"])]
    vals = [(s, d) for s, d in results if re.search(r"^val_bpb:", d.get("stdout", ""), re.M)]
    kept = []
    for r in rows:
        commit, val = r["commit"], r["val_bpb"]
        blob = sprite(f"git -C {repo} show {commit}:train.py")
        want = hashlib.sha256(blob.encode()).hexdigest()
        up = [s for s, h in uploads if h == want]
        if not up:
            bad(f"{commit} ({r['status']}): no logged upload of this commit's train.py")
            continue
        launch = next((s for s in launches if s > up[-1]), None)
        nxt = next((s for s, _ in uploads if s > up[-1]), 10**9)
        shown = [(s, re.search(r"^val_bpb:\s*([0-9.]+)", d["stdout"], re.M).group(1)) for s, d in vals if (launch or 0) < s < nxt]
        tsec = [re.search(r"^training_seconds:\s*([0-9.]+)", d["stdout"], re.M) for s, d in vals if (launch or 0) < s < nxt]
        tsec = [float(m.group(1)) for m in tsec if m]
        desc = f"{commit} {r['status']:7} val_bpb {val}: upload #{up[-1]}, launch #{launch}"
        if r["status"] == "crash":
            (ok if launch else bad)(desc + " (crash)")
            continue
        if not launch or not shown:
            bad(desc + ", no logged val_bpb output")
        elif all(abs(float(v) - float(val)) > 5e-7 for _, v in shown):
            bad(desc + f", logged val_bpb {[v for _, v in shown]} != tsv")
        else:
            ok(desc + f", result #{shown[-1][0]}" + (f", trained {tsec[-1]:.0f}s" if tsec else ""))
        if tsec and not 290 <= tsec[-1] <= 330:
            bad(f"{commit}: training_seconds {tsec[-1]} is not ~300 (time budget changed?)")
        if r["status"] == "keep":
            kept.append((commit, float(val)))
    for (c1, v1), (c2, v2) in zip(kept, kept[1:]):
        if v2 >= v1:
            bad(f"kept {c2} ({v2}) is not better than earlier kept {c1} ({v1})")
    if kept:
        ok(f"{len(rows)} experiments, {len(kept)} kept: {kept[0][1]} -> {kept[-1][1]}")
    return uploads


def check_rules(repo, payloads, uploads, pod_check):
    print("4. Rules")
    # master is the human's platform commit; everything the agent committed is on branches beyond it
    changed = set(sprite(f"git -C {repo} log --format= --name-only --branches --not master").split())
    (ok if changed <= {"train.py"} else bad)(f"files changed by the agent's commits: {sorted(changed) or 'none'}")
    writes = [p["data"]["command"] for p in payloads if p["kind"] == "ssh_exec"
              and re.search(r"(>|sed -i|mv |cp |rm ).*(" + "|".join(map(re.escape, FROZEN)) + ")", p["data"]["command"])]
    (ok if not writes else bad)(f"logged commands writing frozen files: {writes or 'none'}")
    if pod_check:
        out = sprite("ssh gpu 'cd /root/autoresearch && sha256sum train.py " + " ".join(FROZEN) + " && git status --porcelain'")
        pod = dict(reversed(l.split()) for l in out.splitlines() if re.match(r"^[0-9a-f]{64} ", l))
        dirty = [l for l in out.splitlines() if not re.match(r"^[0-9a-f]{64} ", l) and l.strip()]
        (ok if uploads and pod.get("train.py") == uploads[-1][1] else bad)("GPU train.py == last logged upload")
        (ok if dirty == [" M train.py"] or not dirty else bad)(f"GPU repo changes vs pinned commit: {dirty or 'none'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="~/workspace", help="autoresearch git repo on the Sprite")
    ap.add_argument("--pod-check", action="store_true", help="also hash files on the GPU (adds one logged command)")
    a = ap.parse_args()
    if not LOGGER:
        sys.exit("set PLANT_LOGGER_URL to the logger's base URL")
    info = json.load(urllib.request.urlopen(f"{LOGGER}/v1/info", timeout=20))
    entries = fetch_entries()
    payloads = [json.loads(e["payload"]) for e in entries]
    check_chain(info, entries)
    cost = check_model_calls(payloads)
    uploads = check_experiments(a.repo, entries, payloads)
    check_rules(a.repo, payloads, uploads, a.pod_check)
    print(f"\n{'PASS' if not problems else f'{len(problems)} PROBLEM(S)'} · model spend in this log ${cost:.2f}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
