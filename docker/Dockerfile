# syntax=docker/dockerfile:1.6
#
# Coolify-friendly multi-stage image for the Hermes web-relay server.
# Coolify builds this Dockerfile directly; the docker/compose.yml in
# this repo is for LOCAL TESTING only (it is excluded from the
# build context via .dockerignore).
#
# Final stage runs as the unprivileged "webrelay" user (uid 10000)
# and binds uvicorn on 0.0.0.0:8000. The SQLite database lives under
# /data which is declared as a VOLUME so Coolify (or `docker run`)
# can persist it.
#

# -----------------------------------------------------------------------------
# Builder stage — install build deps and the project into a venv we can copy.
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build dependencies for any wheels that need compiling (cryptography, etc.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a venv we can copy into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

# Copy ONLY the project metadata first so dependency installation is cached
# unless pyproject.toml changes.
COPY pyproject.toml README.md ./
COPY src ./src

# Install the project (which pulls in all runtime deps declared in
# pyproject.toml) plus the build backend.
RUN pip install --upgrade pip wheel \
    && pip install . \
    && pip install --no-deps "uvicorn[standard]>=0.27"


# -----------------------------------------------------------------------------
# Runtime stage — slim image, non-root user, copied venv + src.
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    WEBRELAY_DB_PATH=/data/webrelay.db

# We need a runtime uid high enough to avoid colliding with host users; 10000
# is a common Coolify convention and matches the HERMES_UID default used by
# the rest of the Hermes stack.
RUN groupadd --system --gid 10000 webrelay \
    && useradd --system --uid 10000 --gid webrelay \
        --home-dir /data --shell /usr/sbin/nologin \
        --comment "Hermes web-relay server" webrelay

# Copy the prepared venv from the builder.
COPY --from=builder /opt/venv /opt/venv

# Copy the source tree. The package is editable-installed into the venv
# already, but we also need it on disk for uvicorn's `--app-dir` lookup
# and for any in-container debugging.
WORKDIR /app
COPY --chown=webrelay:webrelay --from=builder /build/src ./src

# /data holds the SQLite database; declared as a VOLUME so Coolify (or
# `docker run`) can mount a persistent host path or named volume here.
RUN mkdir -p /data \
    && chown -R webrelay:webrelay /data
VOLUME ["/data"]

USER webrelay

EXPOSE 8000

# uvicorn is configured to trust proxy headers (X-Forwarded-Proto / For)
# because Coolify terminates TLS in front of the container. The
# `--forwarded-allow-ips=*` is required when `--proxy-headers` is set.
CMD ["python", "-m", "uvicorn", "webrelay.server.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
