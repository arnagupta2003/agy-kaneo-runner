FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/runner \
    PATH="/home/runner/.local/bin:/usr/local/bin:${PATH}" \
    DAGU_HOME=/var/lib/dagu

# Core tools: git, python3, curl, ca-certificates, jq, tini
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    python3 \
    tini \
    && rm -rf /var/lib/dagu && rm -rf /var/lib/apt/lists/*

# Create non-root runner user
RUN useradd -m -s /bin/bash runner \
    && mkdir -p /app /app/config /var/lib/dagu/dags /var/lib/dagu/data /var/lib/dagu/logs /workspaces \
    && chown -R runner:runner /app /var/lib/dagu /workspaces

# Install Dagu binary directly from GitHub releases (non-interactive, multi-arch)
RUN ARCH="$(dpkg --print-architecture)" \
    && DAGU_VER="$(curl -fsSL https://api.github.com/repos/dagu-org/dagu/releases/latest | jq -r '.tag_name')" \
    && curl -fsSL "https://github.com/dagu-org/dagu/releases/download/${DAGU_VER}/dagu_${DAGU_VER#v}_linux_${ARCH}.tar.gz" \
       | tar -xz -C /usr/local/bin dagu \
    && chmod +x /usr/local/bin/dagu \
    && dagu version

USER runner
WORKDIR /app

# Install Antigravity CLI (agy) into /home/runner/.local/bin/agy
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && agy --version || true

COPY --chown=runner:runner worker.py /app/worker.py
COPY --chown=runner:runner dags/kaneo-worker.yaml /var/lib/dagu/dags/kaneo-worker.yaml
COPY --chown=runner:runner config/projects.json /app/config/projects.json
RUN chmod +x /app/worker.py

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["dagu", "start-all", "--host", "0.0.0.0", "--port", "8080"]
