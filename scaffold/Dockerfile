# Scaffold: OpenCode agent + tap + dashboard + browser terminal, the Docker equivalent of the Fly Sprite.
# Holds no Tinfoil key; in docker compose it sits on a network where only the logger is reachable.
FROM node:24-trixie-slim
ARG TARGETARCH
ARG OPENCODE_VERSION=1.18.30
ARG CADDY_VERSION=2.11.4
ARG CADDY_SHA512_amd64=8220d1f013b6f27510247b2360c9e0ca9f018feebd82515f07635318b34ff9777ccc8fd0b6e6f2486ce3a33fe389fbb7db12d05baa474f4587509fb4f5ebf1c9
ARG CADDY_SHA512_arm64=d5a7c423853c24a799765e0e8210d5c7c22a8f56ed37a3cae2fb9f58be138853c02b4efd6b59d576e6d8c7c0d30b9c1592deeaa6a536ff69bcca23b8c1ea709c
# ttyd isn't packaged in trixie; static release binaries, checksums from the release's SHA256SUMS.
ARG TTYD_VERSION=1.7.7
ARG TTYD_SHA256_amd64=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
ARG TTYD_SHA256_arm64=b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-cryptography tmux supervisor openssh-client curl ca-certificates git jq procps \
 && rm -rf /var/lib/apt/lists/* \
 && arch="${TARGETARCH:-amd64}" && eval "sum=\$CADDY_SHA512_${arch}" \
 && curl -fsSL -o /tmp/caddy.tgz \
      "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${arch}.tar.gz" \
 && echo "${sum}  /tmp/caddy.tgz" | sha512sum -c - \
 && tar -xzf /tmp/caddy.tgz -C /usr/local/bin caddy && rm /tmp/caddy.tgz \
 && tarch="$([ "$arch" = arm64 ] && echo aarch64 || echo x86_64)" && eval "tsum=\$TTYD_SHA256_${arch}" \
 && curl -fsSL -o /usr/local/bin/ttyd "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${tarch}" \
 && echo "${tsum}  /usr/local/bin/ttyd" | sha256sum -c - && chmod 755 /usr/local/bin/ttyd && ttyd --version \
 && npm install -g "opencode-ai@${OPENCODE_VERSION}" && npm cache clean --force \
 && opencode --version

# node's image already has uid 1000 ("node"); rename it so paths read like the Sprite's.
RUN usermod -l agent -d /home/agent -m node && groupmod -n agent node
COPY tap/ /opt/plant/tap/
COPY dashboard/ /opt/plant/dashboard/
COPY session-template/ /opt/plant/dashboard/session-template/
COPY bin/agent /opt/plant/bin/agent
COPY supervisord.conf /opt/plant/supervisord.conf
COPY entrypoint.sh /opt/plant/entrypoint.sh
RUN chmod 755 /opt/plant/bin/agent /opt/plant/entrypoint.sh /opt/plant/dashboard/*.sh

# Warm OpenCode's provider cache while the build still has internet; at runtime the scaffold has none.
USER agent
WORKDIR /home/agent
RUN mkdir -p /tmp/warm && cp /opt/plant/dashboard/session-template/opencode.json /tmp/warm/ \
 && cd /tmp/warm && (timeout 120 opencode models tinfoil || true) && rm -rf /tmp/warm \
 && ls ~/.cache/opencode 2>/dev/null || true

# The entrypoint starts as root to read the keys mounted at /run/plant whatever their host owner, then drops to agent.
USER root
EXPOSE 8080
ENTRYPOINT ["/opt/plant/entrypoint.sh"]
