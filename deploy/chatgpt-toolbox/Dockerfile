FROM node:22-bookworm-slim@sha256:4d676821dff059fd00d277ee4261ef34ea712317fed0737c03941481b5760c96

ARG DESKTOP_COMMANDER_VERSION=0.2.50

LABEL org.opencontainers.image.title="OBS isolated ChatGPT toolbox" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.base.name="node:22-bookworm-slim" \
      org.opencontainers.image.base.digest="sha256:4d676821dff059fd00d277ee4261ef34ea712317fed0737c03941481b5760c96" \
      io.obs.chatgpt-toolbox.desktop-commander.version="0.2.50"

ENV DEBIAN_FRONTEND=noninteractive \
    DESKTOP_COMMANDER_DISABLE_TELEMETRY=1 \
    HOME=/session/home \
    XDG_CONFIG_HOME=/session/xdg/config \
    XDG_CACHE_HOME=/session/xdg/cache \
    XDG_DATA_HOME=/session/xdg/data \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    TOOLBOX_WORKSPACE=/workspace/runtime/git/obs-vault-active \
    TOOLBOX_ACTIVE_VAULT=/workspace/runtime/git/obs-vault-active \
    TOOLBOX_SESSION=/session

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        procps \
        python3 \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/toolbox
COPY package.json package-lock.json ./
RUN test "$(node -p "require('./package-lock.json').packages['node_modules/@wonderwhy-er/desktop-commander'].version")" = "$DESKTOP_COMMANDER_VERSION" \
    && npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force \
    && test "$(node -p "require('./node_modules/@wonderwhy-er/desktop-commander/package.json').version")" = "$DESKTOP_COMMANDER_VERSION"

RUN groupadd --gid 10001 toolbox \
    && useradd --uid 10001 --gid 10001 --home-dir /session/home --shell /bin/bash --no-create-home toolbox \
    && mkdir -p \
        /opt/toolbox/bin \
        /session/home \
        /session/xdg/config \
        /session/xdg/cache \
        /session/xdg/data \
        /workspace/runtime/git/obs-vault-active \
    && chown -R 10001:10001 /session /workspace \
    && chmod 0700 /session/home /session/xdg/config /session/xdg/cache /session/xdg/data \
    && chmod 0750 /workspace

COPY --chmod=0755 bin/entrypoint.sh /usr/local/bin/toolbox-entrypoint
COPY --chmod=0755 bin/self_test.py /opt/toolbox/bin/self-test
COPY --chmod=0755 bin/boundary_probe.py /opt/toolbox/bin/boundary-probe

VOLUME ["/session"]
USER 10001:10001
WORKDIR /workspace/runtime/git/obs-vault-active
ENTRYPOINT ["/usr/local/bin/toolbox-entrypoint"]
CMD ["idle"]
