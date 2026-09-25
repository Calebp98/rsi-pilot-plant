# Laptop only

Two ways to use Tinfoil's verified inference from your own machine, with no logger and no remote scaffold. The Tinfoil key stays on your machine.

## OpenCode through tinfoil-proxy

```sh
TINFOIL_API_KEY=... ./agent.sh [dir]
```

Starts [`tinfoil-proxy`](https://github.com/tinfoilsh/tinfoil-proxy/releases) on 127.0.0.1:3301 if it isn't running (check the binary against the release's `SHA256SUMS`), then opens OpenCode in `dir` (default `./workspace`) with `opencode.json`: provider `tinfoil` pointing at the proxy, default model gpt-oss-120b, file edits allowed, shell commands and web fetches ask first, paths outside the folder denied. The proxy verifies the router enclave's attestation and pins its key before forwarding. Switch models in OpenCode with `/models`.

Other coding agents Tinfoil documents (Cline, Kilo Code, Factory Droid, Hermes) use the same proxy URL: https://docs.tinfoil.sh/tutorials/coding-agents.md

## Chat app

A stdlib HTTP server plus Tinfoil's Python SDK, with a single-page UI.

```sh
cd chat
python3 -m venv .venv && .venv/bin/pip install tinfoil
TINFOIL_API_KEY=... .venv/bin/python server.py      # open http://127.0.0.1:8765
```

- The SDK verifies attestation at startup; the server refuses to start if verification fails. Requests go over the verified, key-pinned connection.
- Shows the reply, reasoning, tokens / cost / latency per message, model info and the verification result, with a Re-verify button.
- Agent mode gives the model `list_files`, `read_file`, `write_file` and `edit_file`, limited to `../workspace` (or `WORKSPACE=/path`). No shell tool. At most 25 tool steps per turn.

## What plain HTTP skips

Calling `https://inference.tinfoil.sh/v1` with curl or a stock OpenAI client skips verification: you're trusting ordinary TLS and Tinfoil's word. The SDK and the proxy run the open-source verifier (about 2.5 s):

| Step | Checks |
|---|---|
| fetch digest | expected code hash for `tinfoilsh/confidential-model-router` from the GitHub release, signed through Sigstore |
| verify code | that digest's provenance |
| verify enclave | the AMD SEV-SNP / Intel TDX hardware report signature |
| compare measurements | the running code's measurement matches the published one |

The report binds the enclave's TLS and HPKE keys, and the connection is pinned to them.
