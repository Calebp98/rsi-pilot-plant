# Deploying on Fly.io, a Fly Sprite and Runpod

This is the original deployment: logger on a Fly Machine (fixed image, visible redeploys), scaffold on a [Fly Sprite](https://sprites.dev) (disposable VM with checkpoints and an egress policy), GPU on a Runpod pod. Replace every `<...>` with your own names.

You need `flyctl`, the `sprite` CLI, and a Tinfoil API key. Keep secrets out of shell history: pipe them from a password manager (e.g. `op read op://...`) and **check the length before setting**: a timed-out `op read` returns empty, and piping that into `fly secrets` silently sets an empty key.

## 1. Logger

```sh
cd logger
fly launch --copy-config --no-deploy --name <logger-app> --region <region>
fly volumes create logger_data --size 1 -a <logger-app>
fly deploy --remote-only --ha=false
fly ips allocate-v6 -a <logger-app>        # the SSH gateway on :2222 is reached over IPv6; no paid IPv4 needed
curl -s https://<logger-app>.fly.dev/v1/info | jq '{logger_pubkey, ssh: .ssh.host_pubkey, upstream: .ssh.upstream_pubkey}'
```

The logger generates its signing key, gateway key, SSH host key and upstream key on first boot in `/data`. Note the SSH host key and upstream key from `/v1/info`; the scaffold pins the first, the GPU server authorises the second.

Secrets (set them together; each `fly secrets set` restarts the machine):

| Secret | Value |
|---|---|
| `TINFOIL_API_KEY` | your Tinfoil key |
| `READ_TOKEN` | `openssl rand -hex 32` (also goes on the scaffold) |
| `CLIENT_KEYS` | `scaffold:<tap pubkey, base64 raw ed25519>` (from the scaffold's `/_tap/status`, step 2) |
| `MODEL_CLIENT_TOKENS` | `scaffold:<random token>` (also goes on the scaffold) |
| `SSH_CLIENT_KEYS` | `scaffold:ssh-ed25519 AAAA…` (the scaffold agent's SSH public key) |
| `SSH_TARGETS` | `gpu=<user>@<host>:<port>` |
| `SSH_TARGET_HOSTKEYS` | `gpu=ssh-ed25519 AAAA…` (the GPU server's host key, obtained out of band, never from the open port) |
| `SSH_TARGET_LABELS` | `gpu=<free text shown on the dashboard>` (optional; marked "not checked") |
| `ALLOWED_ORIGINS` | `https://<sprite-url>` (CORS for the dashboard) |

**Don't redeploy or change secrets during an agent session.** Both restart the SSH gateway and cut in-flight commands. Check `/v1/head` for recent activity first.

## 2. Scaffold on a Sprite

Create a Sprite (these docs call it `tinfoil-scaffold`) and lay out the home directory the way the Docker image's `scaffold/entrypoint.sh` does:

| On the Sprite | From |
|---|---|
| `~/tap/tap.py` | `scaffold/tap/` |
| `~/dashboard/*` | `scaffold/dashboard/` |
| `~/dashboard/session-template/` | `scaffold/session-template/` |
| `~/.local/bin/agent` | `scaffold/bin/agent` |
| `~/.config/tap/config.json` | `{"logger_url": "https://<logger-app>.fly.dev", "client": "scaffold"}` |
| `~/.config/tap/read_token`, `model_token` | the values set on the logger |
| `~/.ssh/id_ed25519` | `ssh-keygen -t ed25519` on the Sprite; register the `.pub` as `SSH_CLIENT_KEYS` |
| `~/.ssh/known_hosts_lucid` | `[<logger-app>.fly.dev]:2222 <logger ssh host key>` |
| `~/.ssh/config` | `Host gpu` → HostName `<logger-app>.fly.dev`, Port 2222, User gpu, UserKnownHostsFile `~/.ssh/known_hosts_lucid` |

Install OpenCode, ttyd, Caddy and tmux under `~/.local` (**not** in `/.sprite`, which Fly manages and may reset). With npm 12, `npm install --prefix ~/.local/opt/opencode opencode-ai@<version>` skips install scripts: run its `postinstall.mjs` by hand. Copy files in with `sprite exec -- bash -c 'cat > path' < file` (piping a tar archive into `sprite exec` can hang).

Services (`sprite-env services`): `tap` (`python3 ~/tap/tap.py`), `dashboard`, `ttyd`, `ttyd-view`, `caddy`, using the `run-*.sh` scripts in `scaffold/dashboard/`. Put a bcrypt hash (`caddy hash-password`) in place of `BCRYPT_HASH` in the Caddyfile. The tap generates its signing key on first start; register its public key (`curl 127.0.0.1:3300/_tap/status`) as the logger's `CLIENT_KEYS`.

Make the Sprite URL public (Caddy's password guards every route, terminal included) and **never** turn off the Caddy password: the terminal is a full shell.

**Egress policy**, set from outside (it can't be changed from inside the Sprite):

```sh
sprite api /v1/sprites/<sprite>/policy/network -- -X POST -H 'Content-Type: application/json' \
  --data '{"rules":[{"domain":"<logger-app>.fly.dev","action":"allow"}]}'
```

Check from inside that Tinfoil, PyPI, GitHub and raw IPs are all blocked and the logger isn't. To install more tools later, open a temporary window with only the domains needed, install pinned versions into `~`, close it, and add the versions to the session manifest (`manifest()` in `dashboard/server.py`).

**View-only link:** write a random secret to `~/.config/dashboard/view_secret` (`python3 -c "import secrets;print(secrets.token_urlsafe(24))"`). The page is then at `https://<sprite-url>/view/<secret>/`, with no password, a read-only terminal and no write actions. Overwrite the file to revoke; no restart needed.

Take a checkpoint once it works (`sprite checkpoint`).

## 3. GPU: stand-in on Fly

For testing without a GPU:

```sh
cd gpu-standin
fly launch --copy-config --no-deploy --name <standin-app>
ssh-keygen -t ed25519 -N '' -f hostkey
fly secrets set -a <standin-app> HOST_KEY="$(base64 < hostkey | tr -d '\n')" AUTHORIZED_KEYS="<logger upstream pubkey>"
fly deploy --remote-only --ha=false
# on the logger:
fly secrets set -a <logger-app> SSH_TARGETS=gpu=lucid@<standin-app>.internal:2200 SSH_TARGET_HOSTKEYS="gpu=$(cut -d' ' -f1,2 hostkey.pub)"
```

It has no public IPs and is reachable only over Fly's private network. If the machine crash-loops on first deploy, start it by hand once the secrets are set.

## 4. GPU: Runpod pod

1. Create a pod with only `22/tcp` exposed and the environment variable `PUBLIC_KEY` set to the logger's upstream key. Register no SSH keys on the Runpod account, so that key is the only one in `authorized_keys`.
2. Get the host key out of band. Set the start command so the pod prints it to its log before starting sshd, e.g. `bash -c 'ssh-keygen -A; echo LUCID_HOSTKEY $(cat /etc/ssh/ssh_host_ed25519_key.pub); exec /start.sh'`, and read that line through Runpod's API (container logs). Don't trust whatever answers on the open port.
3. `fly secrets set -a <logger-app> 'SSH_TARGETS=gpu=root@<ip>:<port>' 'SSH_TARGET_HOSTKEYS=gpu=<key>' 'SSH_TARGET_LABELS=gpu=<description>'`
4. From the scaffold: `ssh gpu nvidia-smi -L`.

**After any pod stop/start** the container is recreated: the public port, the host key and possibly the IP change, and everything outside a volume is gone. Repeat steps 2-4.

**Idle watchdog:** `gpu-runpod/watchdog.sh` stops the pod after 60 idle minutes (no GPU utilisation, no GPU processes, no training processes), using the `RUNPOD_API_KEY` Runpod puts in the container's PID 1 environment. Install it through the gateway so the install is logged:

```sh
ssh gpu 'cat > /root/lucid-watchdog.sh && chmod +x /root/lucid-watchdog.sh' < gpu-runpod/watchdog.sh
ssh gpu 'setsid nohup /root/lucid-watchdog.sh > /dev/null 2>&1 &'
```

Reinstall it after every pod restart. To stop it, use `pkill -f '[l]ucid-watchdog'`: a plain `pkill -f lucid-watchdog.sh` inside an `ssh gpu` command matches and kills that command's own shell.
