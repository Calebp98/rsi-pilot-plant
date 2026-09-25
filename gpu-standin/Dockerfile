# Stand-in for the Lucid Neocloud GPU server: sshd only, reachable only over Fly's private network.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server python3 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -s /bin/bash lucid && usermod -p "*" lucid && mkdir -p /run/sshd /home/lucid/.ssh && chown lucid /home/lucid/.ssh && chmod 700 /home/lucid/.ssh \
 && rm -f /etc/ssh/ssh_host_*
COPY sshd_config /etc/ssh/sshd_config
COPY nvidia-smi /usr/local/bin/nvidia-smi
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /usr/local/bin/nvidia-smi /entrypoint.sh
CMD ["/entrypoint.sh"]
