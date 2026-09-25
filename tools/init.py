"""Generate every key and token a local (docker compose) pilot plant needs, into ./state.

  docker compose run --rm init          # runs this in the logger image, which has cryptography + asyncssh
  docker compose run --rm init --force  # replace existing state (the old log is kept under state/old-*)

Nothing here is a third-party credential. TINFOIL_API_KEY is never written: compose passes it from your
shell to the logger only.

  state/logger.env        logger config: registered clients, read token, SSH gateway targets, model tokens
  state/logger/data/      logger volume, pre-seeded with the SSH gateway's host and upstream keys
  state/gpu.env           stand-in GPU: its sshd host key, and the gateway's upstream key as the only authorized key
  state/scaffold/         scaffold keys: tap signing key, agent SSH key, pinned gateway host key, tokens, console password
"""

import base64
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

import asyncssh
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

STATE = Path(os.environ.get("STATE_DIR", "/state"))
GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "logger")
GPU_TARGET = os.environ.get("GPU_TARGET", "lucid@gpu:2200")
ORIGINS = os.environ.get("ALLOWED_ORIGINS", "http://127.0.0.1:8080,http://localhost:8080")


def ssh_key(path, comment):
    key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
    key.write_private_key(str(path))
    path.chmod(0o600)
    return key, key.export_public_key().decode().strip()


def write(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)


def main():
    if STATE.joinpath("logger.env").exists():
        if "--force" not in sys.argv:
            sys.exit(f"{STATE} already initialised; pass --force to replace it (the old state is moved aside)")
        old = STATE / f"old-{time.strftime('%Y%m%dT%H%M%S')}"
        old.mkdir()
        for p in list(STATE.iterdir()):
            if not p.name.startswith("old-") and p.name != ".gitkeep":
                shutil.move(str(p), old / p.name)
        print(f"moved previous state to {old}")

    data, scaffold = STATE / "logger/data", STATE / "scaffold"
    data.mkdir(parents=True, exist_ok=True)
    scaffold.mkdir(parents=True, exist_ok=True)

    # Logger side: the SSH gateway loads these instead of generating its own on first boot.
    _, host_pub = ssh_key(data / "ssh_host_ed25519", "lucid-logger host")
    _, upstream_pub = ssh_key(data / "upstream_ed25519", "lucid-logger upstream")

    # Stand-in GPU: its own host key (pinned by the gateway) and the gateway's upstream key.
    gpu_key = asyncssh.generate_private_key("ssh-ed25519", comment="gpu-standin host")
    gpu_priv = gpu_key.export_private_key().decode()
    gpu_pub = gpu_key.export_public_key().decode().strip()
    write(STATE / "gpu.env", f"HOST_KEY={base64.b64encode(gpu_priv.encode()).decode()}\nAUTHORIZED_KEYS={upstream_pub}\n")

    # Scaffold: the tap signs log records with a raw ed25519 key (PKCS8 PEM); the agent SSHes with an OpenSSH key.
    tap = Ed25519PrivateKey.generate()
    write(scaffold / "tap_ed25519.pem", tap.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()).decode())
    tap_pub = base64.b64encode(tap.public_key().public_bytes(serialization.Encoding.Raw,
                                                             serialization.PublicFormat.Raw)).decode()
    _, agent_pub = ssh_key(scaffold / "id_ed25519", "scaffold agent")
    write(scaffold / "id_ed25519.pub", agent_pub + "\n", 0o644)
    write(scaffold / "known_hosts_lucid", f"[{GATEWAY_HOST}]:2222 {' '.join(host_pub.split()[:2])}\n", 0o644)
    read_token, model_token = secrets.token_hex(32), secrets.token_urlsafe(32)
    console_password = secrets.token_urlsafe(18)
    write(scaffold / "read_token", read_token + "\n")
    write(scaffold / "model_token", model_token + "\n")
    write(scaffold / "console_password", console_password + "\n")

    write(STATE / "logger.env", "\n".join([
        f"CLIENT_KEYS=scaffold:{tap_pub}",
        f"READ_TOKEN={read_token}",
        f"ALLOWED_ORIGINS={ORIGINS}",
        f"SSH_CLIENT_KEYS=scaffold:{' '.join(agent_pub.split()[:2])}",
        f"SSH_TARGETS=gpu={GPU_TARGET}",
        f"SSH_TARGET_HOSTKEYS=gpu={' '.join(gpu_pub.split()[:2])}",
        "SSH_TARGET_LABELS=gpu=Docker stand-in (no real GPU)",
        f"MODEL_CLIENT_TOKENS=scaffold:{model_token}",
    ]) + "\n")

    # The scaffold runs as uid 1000 and reads state/scaffold through a bind mount.
    if os.geteuid() == 0:
        for p in [scaffold, *scaffold.iterdir()]:
            os.chown(p, 1000, 1000)
    print(f"initialised {STATE}")
    print(f"console: http://127.0.0.1:8080  user lucid  password {console_password}")
    print("         (also in state/scaffold/console_password)")


if __name__ == "__main__":
    main()
