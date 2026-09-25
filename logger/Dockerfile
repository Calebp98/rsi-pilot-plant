FROM python:3.13-slim
ARG TARGETARCH
ARG TINFOIL_PROXY_VERSION=v0.2.3
# Checksums from the release's SHA256SUMS; bump both with the version.
ARG TINFOIL_PROXY_SHA256_amd64=eb219044e46b65a11096a063de0a01acab562a42f7941e7aba5bc8150577c16d
ARG TINFOIL_PROXY_SHA256_arm64=efa8dd34d378d8da7562c8b01bbd71f136c4cf1a540e1e8fa8e3562d1b11ba49
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/* \
 && arch="${TARGETARCH:-amd64}" && eval "sum=\$TINFOIL_PROXY_SHA256_${arch}" \
 && curl -fsSL -o /usr/local/bin/tinfoil-proxy \
      "https://github.com/tinfoilsh/tinfoil-proxy/releases/download/${TINFOIL_PROXY_VERSION}/tinfoil-proxy-linux-${arch}" \
 && echo "${sum}  /usr/local/bin/tinfoil-proxy" | sha256sum -c - \
 && chmod 755 /usr/local/bin/tinfoil-proxy
RUN pip install --no-cache-dir cryptography==50.0.1 asyncssh==2.24.0 tinfoil==0.14.0
WORKDIR /app
COPY app.py ssh_gateway.py model_gateway.py ./
EXPOSE 8080 2222
CMD ["python", "-u", "app.py"]
