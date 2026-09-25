#!/bin/sh
# HOST_KEY (base64 of the OpenSSH private key) and AUTHORIZED_KEYS (the logger's upstream pubkey) come from Fly secrets.
set -e
echo "$HOST_KEY" | base64 -d > /etc/ssh/ssh_host_ed25519_key && chmod 600 /etc/ssh/ssh_host_ed25519_key
echo "$AUTHORIZED_KEYS" > /home/lucid/.ssh/authorized_keys && chown lucid /home/lucid/.ssh/authorized_keys && chmod 600 /home/lucid/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e
