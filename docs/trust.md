# What is and isn't proven

## What the log proves

- **The logger didn't alter or drop what it received.** Each entry is chained to the previous one's hash and signed by the logger's key. Editing, reordering or deleting an entry breaks verification (tested: edited payload, edited timestamp, deleted entry, re-hash without keys).
- **The logger didn't invent client entries.** Scaffold entries carry the scaffold's own signature over the exact payload bytes.
- **No replays.** `client_seq` must increase per client, across rotations and restarts.
- **Sessions link.** Each rotated log's first entry names the previous log and its final hash.
- **Model traffic went through the gateway.** The tap and the gateway record each call independently; the dashboard and `verify_run.py` pair them.
- **The model requests reached an attested Tinfoil enclave.** `tinfoil-proxy` refuses to send a request until the router enclave's attestation checks out against the release Tinfoil publishes through Sigstore, and it pins the connection to the attested key.

## What it doesn't prove

- **That the scaffold sent everything.** The tap is self-reporting. Whatever controls the scaffold could skip or alter records before signing them. The gateway's own records cover model calls and GPU commands, but anything the agent does purely locally (editing files, running local programs) is visible only through what it later sends.
- **That the logger's operator is honest.** The logger is an ordinary Fly Machine (or container), not a confidential VM. `fly ssh console` gives anyone in the Fly org a shell on it, which bypasses the gateway and could rewrite files. The signing key is on the same volume, so a rewritten history could be re-signed; only a verifier who already saw an earlier head (the dashboard remembers heads, `verify_log.py` pins keys) would notice. `/v1/info` publishes the source file hashes and the image ref so you can compare against this repo, but that's the logger's own claim.
- **Confidentiality from the hosting providers.** Fly's edge terminates TLS for the logger's HTTPS service and can read that hop. A Sprite isn't a confidential VM. A Runpod Community Cloud pod belongs to a third-party host who can read everything on it. Use test data and a scoped key.
- **That the dashboard page isn't lying.** The page's JavaScript is served by the scaffold. The independent check is running `tools/verify_log.py` from your own machine and comparing the pinned keys out of band.
- **The router-to-model hop.** The client verifies Tinfoil's router enclave, not the model enclave; the router forwards over an attested channel, and you rely on the router's open-source code for that.
- **Tinfoil's build pipeline**, unless you reproduce the enclave image yourself (`tinfoilsh/cvmimage` at the release tag, then compare hashes with the release manifest).
- **AMD / Intel** hardware and signing keys.

## Side doors to close in a real deployment

- The GPU server's SSH port: allow only the logger's static egress IP (`fly ips allocate-egress`), and remove every key but the gateway's upstream key.
- Provider consoles: Runpod's web terminal and proxy SSH, and `fly ssh console`, all reach machines without going through the gateway or the log.
- The console password gives a full shell on the scaffold, which holds the agent's SSH key, the model token and the log read token. Rotate it if it reaches anyone you wouldn't give those to.
- The view-only link exposes the full log: every prompt, reply, command and output.
