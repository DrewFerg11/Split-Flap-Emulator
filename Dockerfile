# syntax=docker/dockerfile:1
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS build
ARG BUILD_JOBS=4
WORKDIR /src
RUN --mount=type=secret,id=ca_bundle \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates build-essential git ninja-build libglib2.0-dev libgcrypt20-dev \
      libslirp-dev libpixman-1-dev libjson-c-dev python3 python3-venv flex bison pkg-config xz-utils \
    && rm -rf /var/lib/apt/lists/*
COPY qemu/ qemu/
COPY model/ model/
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then \
      export SSL_CERT_FILE=/run/secrets/ca_bundle PIP_CERT=/run/secrets/ca_bundle \
        GIT_SSL_CAINFO=/run/secrets/ca_bundle; \
    fi; \
    qemu/build.sh --jobs "$BUILD_JOBS" --install-dir /opt/flapsim/qemu \
    && mkdir -p /source-archives \
    && tar --exclude-vcs --exclude='__pycache__' -cJf /source-archives/qemu.tar.xz \
      -C build/qemu/src .
ARG SOURCE_REVISION=unrecorded
COPY . .
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then \
      export SSL_CERT_FILE=/run/secrets/ca_bundle PIP_CERT=/run/secrets/ca_bundle; \
    fi; \
    python3 -m venv /build-venv \
    && /build-venv/bin/pip wheel --wheel-dir /wheels '.[control]' \
    && tar --exclude-vcs --exclude=build --exclude='*.egg-info' --exclude='__pycache__' \
      -cJf /source-archives/flapsim.tar.xz . \
    && python3 tools/build_provenance.py --revision "$SOURCE_REVISION" \
      --out /source-archives/provenance.json

FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS runtime
RUN --mount=type=secret,id=ca_bundle \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-venv libglib2.0-0 libgcrypt20 libslirp0 \
      libpixman-1-0 libjson-c5 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/flapsim/venv \
    && mkdir -p /work \
    && chown 10001:10001 /work
ARG SOURCE_REVISION=unrecorded
ARG EMULATOR_VERSION=unrecorded
LABEL org.opencontainers.image.source="https://github.com/DrewFerg11/Split-Flap-Emulator" \
      org.opencontainers.image.revision="$SOURCE_REVISION" \
      org.opencontainers.image.version="$EMULATOR_VERSION" \
      org.opencontainers.image.licenses="GPL-2.0-only"
COPY --from=build /wheels /wheels
RUN /opt/flapsim/venv/bin/pip install --no-index --find-links=/wheels 'flapsim[control]' \
    && rm -rf /wheels
COPY --from=build /opt/flapsim/qemu /opt/flapsim/qemu
COPY --from=build /source-archives /usr/share/doc/flapsim/sources
COPY --chmod=0644 LICENSE /usr/share/doc/flapsim/LICENSE
ENV PATH="/opt/flapsim/venv/bin:$PATH" \
    FLAPSIM_QEMU_PREFIX=/opt/flapsim/qemu \
    FLAPSIM_PROVENANCE=/usr/share/doc/flapsim/sources/provenance.json
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["flapsim"]
CMD ["--help"]
