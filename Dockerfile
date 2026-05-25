# syntax=docker/dockerfile:1.18

# This file is designed for production server deployment, not local development work
# For a containerized local dev environment, see: https://github.com/mastodon/mastodon/blob/main/docs/DEVELOPMENT.md#docker

# Please see https://docs.docker.com/engine/reference/builder for information about
# the extended buildx capabilities used in this file.
# Make sure multiarch TARGETPLATFORM is available for interpolation
# See: https://docs.docker.com/build/building/multi-platform/
ARG BASE_REGISTRY="docker.io"

# Python version to use, change with [--build-arg PYTHON_VERSION="3.13"]
ARG PYTHON_VERSION="3.12"
# Node.js version to use in base image, change with [--build-arg NODE_MAJOR_VERSION="22"]
# renovate: datasource=node-version depName=node
ARG NODE_MAJOR_VERSION="24"
# Debian image to use for base image, change with [--build-arg DEBIAN_VERSION="bookworm"]
ARG DEBIAN_VERSION="trixie"
# Node.js image to use for base image based on combined variables (ex: 24-trixie-slim)
FROM ${BASE_REGISTRY}/node:${NODE_MAJOR_VERSION}-${DEBIAN_VERSION}-slim AS node
# Python image to use for base image based on combined variables (ex: 3.12-slim-trixie)
FROM ${BASE_REGISTRY}/python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS python

# Resulting version string is vX.X.X-MASTODON_VERSION_PRERELEASE+MASTODON_VERSION_METADATA
# Example: v4.3.0-nightly.2023-11-09+pr-123456
# Overwrite existence of 'alpha.X' in version.rb [--build-arg MASTODON_VERSION_PRERELEASE="nightly.2023-11-09"]
ARG MASTODON_VERSION_PRERELEASE=""
# Append build metadata or fork information [--build-arg MASTODON_VERSION_METADATA="pr-123456"]
ARG MASTODON_VERSION_METADATA=""
# Will be available as source commit reference
ARG SOURCE_COMMIT=""

# Timezone used by the Docker container and runtime, change with [--build-arg TZ=Europe/Berlin]
ARG TZ="Etc/UTC"
# Linux UID (user id) for the mastodon user, change with [--build-arg UID=1234]
ARG UID="991"
# Linux GID (group id) for the mastodon user, change with [--build-arg GID=1234]
ARG GID="991"

# Apply Mastodon build options based on options above
ENV \
  MASTODON_VERSION_PRERELEASE="${MASTODON_VERSION_PRERELEASE}" \
  MASTODON_VERSION_METADATA="${MASTODON_VERSION_METADATA}" \
  SOURCE_COMMIT="${SOURCE_COMMIT}" \
  TZ="${TZ}"

# Configure runtime environment
# BIND: IP to bind Mastodon to when serving traffic
# NODE_ENV/MASTODON_ENV: production settings for Node.js and the FastAPI backend
# DEBIAN_FRONTEND: suppress interactive prompts
ENV \
  BIND="0.0.0.0" \
  NODE_ENV="production" \
  MASTODON_ENV="production" \
  DEBIAN_FRONTEND="noninteractive"

# Set default shell used for running commands
SHELL ["/bin/bash", "-o", "pipefail", "-o", "errexit", "-c"]

ARG TARGETPLATFORM

RUN echo "Target platform is $TARGETPLATFORM"

RUN \
  # Remove automatic apt cache Docker cleanup scripts
  rm -f /etc/apt/apt.conf.d/docker-clean; \
  # Sets timezone
  echo "${TZ}" > /etc/localtime; \
  # Creates mastodon user/group and sets home directory
  groupadd -g "${GID}" mastodon; \
  useradd -l -u "${UID}" -g "${GID}" -m -d /opt/mastodon mastodon; \
  # Creates /mastodon symlink to /opt/mastodon
  ln -s /opt/mastodon /mastodon;

# Set /opt/mastodon as working directory
WORKDIR /opt/mastodon

# hadolint ignore=DL3008,DL3005
RUN \
  # Mount Apt cache and lib directories from Docker buildx caches
  --mount=type=cache,id=apt-cache-${TARGETPLATFORM},target=/var/cache/apt,sharing=locked \
  --mount=type=cache,id=apt-lib-${TARGETPLATFORM},target=/var/lib/apt,sharing=locked \
  # Update package list and upgrade system packages
  apt-get update; \
  apt-get dist-upgrade -yq; \
  # Install runtime components
  apt-get install -y --no-install-recommends \
  curl \
  file \
  procps \
  tini \
  tzdata \
  wget \
  # PostgreSQL client library (asyncpg runtime dep)
  libpq5 \
  libssl3t64 \
  # libvips (Debian trixie ships 8.15.x)
  libvips42 \
  # ffmpeg (Debian trixie ships 7.1.x)
  ffmpeg \
  ;

# Build Python dependencies with uv
FROM python AS python-deps

ARG TARGETPLATFORM

# hadolint ignore=DL3008
RUN \
  --mount=type=cache,id=apt-cache-${TARGETPLATFORM},target=/var/cache/apt,sharing=locked \
  --mount=type=cache,id=apt-lib-${TARGETPLATFORM},target=/var/lib/apt,sharing=locked \
  apt-get update; \
  apt-get install -y --no-install-recommends \
  build-essential \
  libpq-dev \
  libssl-dev \
  # libvips dev headers needed to compile pyvips C extension
  libvips-dev \
  ;

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
  UV_LINK_MODE=copy \
  VIRTUAL_ENV=/opt/venv

# Install Python dependencies from lockfile into a named virtualenv
COPY pyproject.toml uv.lock /opt/mastodon/

RUN \
  --mount=type=cache,id=uv-cache-${TARGETPLATFORM},target=/root/.cache/uv,sharing=locked \
  uv venv /opt/venv && \
  cd /opt/mastodon && \
  uv sync --frozen --no-dev --no-install-project --python /opt/venv/bin/python;

# Build frontend assets
FROM python AS assets

ARG TARGETPLATFORM

# Copy Node.js binaries/libraries into layer
COPY --from=node /usr/local/bin /usr/local/bin
COPY --from=node /usr/local/lib /usr/local/lib

# Limit Node.js heap and yarn network concurrency for memory-constrained builds
ENV NODE_OPTIONS="--max-old-space-size=512" \
  YARN_NETWORK_CONCURRENCY="1"

RUN \
  # Mount local Corepack and Yarn caches from Docker buildx caches
  --mount=type=cache,id=corepack-cache-${TARGETPLATFORM},target=/usr/local/share/.cache/corepack,sharing=locked \
  --mount=type=cache,id=yarn-cache-${TARGETPLATFORM},target=/usr/local/share/.cache/yarn,sharing=locked \
  # Remove pre-installed Yarn binaries (only present on Node <26)
  rm -f /usr/local/bin/yarn*; \
  # Install Corepack
  npm i -g corepack;

# Copy Mastodon sources into layer
COPY . /opt/mastodon/

# hadolint ignore=DL3008
RUN \
  --mount=type=cache,id=corepack-cache-${TARGETPLATFORM},target=/usr/local/share/.cache/corepack,sharing=locked \
  --mount=type=cache,id=yarn-cache-${TARGETPLATFORM},target=/usr/local/share/.cache/yarn,sharing=locked \
  yarn install --immutable;

RUN \
  --mount=type=cache,id=corepack-cache-${TARGETPLATFORM},target=/usr/local/share/.cache/corepack,sharing=locked \
  --mount=type=cache,id=yarn-cache-${TARGETPLATFORM},target=/usr/local/share/.cache/yarn,sharing=locked \
  # Build frontend assets
  yarn build:production; \
  # Cleanup temporary files
  rm -fr /opt/mastodon/tmp;

# Prep final Mastodon layer
FROM python AS mastodon

ARG TARGETPLATFORM

# Copy Mastodon sources into final layer
COPY . /opt/mastodon/

# Copy installed Python virtualenv
COPY --from=python-deps /opt/venv /opt/venv
# Copy uv for runtime use (migrations, etc.)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN \
  # Smoketest media processors
  vips --version; \
  ffmpeg -version; \
  ffprobe -version;

RUN \
  # Pre-create and chown system volume to mastodon user
  mkdir -p /opt/mastodon/public/system; \
  chown mastodon:mastodon /opt/mastodon/public/system; \
  # Set mastodon user as owner of tmp folder
  mkdir -p /opt/mastodon/tmp; \
  chown -R mastodon:mastodon /opt/mastodon/tmp;

# Activate the virtualenv for all subsequent commands and the running container
ENV VIRTUAL_ENV=/opt/venv \
  PATH="/opt/venv/bin:${PATH}"

# Set the running user for resulting container
USER mastodon
# Expose default uvicorn port
EXPOSE 8000
# Set container tini as default entry point
ENTRYPOINT ["/usr/bin/tini", "--"]
# Default command: run the FastAPI app with uvicorn
CMD ["uvicorn", "app.python.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
