# syntax=docker/dockerfile:1.18

# This file is designed for production server deployment, not local development work
# For a containerized local dev environment, see: https://github.com/mastodon/mastodon/blob/main/docs/DEVELOPMENT.md#docker

# Please see https://docs.docker.com/engine/reference/builder for information about
# the extended buildx capabilities used in this file.
# Make sure multiarch TARGETPLATFORM is available for interpolation
# See: https://docs.docker.com/build/building/multi-platform/
ARG TARGETPLATFORM=${TARGETPLATFORM}
ARG BUILDPLATFORM=${BUILDPLATFORM}
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
  # Install necessary runtime components
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
  # libvips runtime components
  libcgif0 \
  libexif12 \
  libheif1 \
  libhwy1t64 \
  libimagequant0 \
  libjpeg62-turbo \
  liblcms2-2 \
  libspng0 \
  libtiff6 \
  libwebp7 \
  libwebpdemux2 \
  libwebpmux3 \
  # ffmpeg runtime components
  libdav1d7 \
  libmp3lame0 \
  libopencore-amrnb0 \
  libopencore-amrwb0 \
  libopus0 \
  libsnappy1v5 \
  libtheora0 \
  libvorbis0a \
  libvorbisenc2 \
  libvorbisfile3 \
  libvpx9 \
  libx264-164 \
  libx265-215 \
  ;

# Build stage for media libraries (libvips, ffmpeg)
FROM ${BASE_REGISTRY}/python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS media-build

ARG TARGETPLATFORM

# Set default shell used for running commands
SHELL ["/bin/bash", "-o", "pipefail", "-o", "errexit", "-c"]

# hadolint ignore=DL3008
RUN \
  --mount=type=cache,id=apt-native-cache-${TARGETPLATFORM},target=/var/cache/apt,sharing=locked \
  --mount=type=cache,id=apt-native-lib-${TARGETPLATFORM},target=/var/lib/apt,sharing=locked \
  # Remove automatic apt cache Docker cleanup scripts
  rm -f /etc/apt/apt.conf.d/docker-clean; \
  # Install build tools for native libraries
  apt-get update; \
  apt-get install -y --no-install-recommends \
  autoconf \
  automake \
  build-essential \
  libtool \
  meson \
  nasm \
  pkg-config \
  xz-utils \
  # libvips components
  libcgif-dev \
  libexif-dev \
  libexpat1-dev \
  libgirepository1.0-dev \
  libglib2.0-dev \
  libheif-dev \
  libhwy-dev \
  libimagequant-dev \
  libjpeg62-turbo-dev \
  liblcms2-dev \
  libspng-dev \
  libtiff-dev \
  libwebp-dev \
  # ffmpeg components
  libdav1d-dev \
  liblzma-dev \
  libmp3lame-dev \
  libopus-dev \
  libsnappy-dev \
  libvorbis-dev \
  libvpx-dev \
  libx264-dev \
  libx265-dev \
  ;

# Create temporary libvips specific build layer
FROM media-build AS libvips

# libvips version to compile, change with [--build-arg VIPS_VERSION="8.15.2"]
# renovate: datasource=github-releases depName=libvips packageName=libvips/libvips
ARG VIPS_VERSION=8.18.2
# libvips download URL, change with [--build-arg VIPS_URL="https://github.com/libvips/libvips/releases/download"]
ARG VIPS_URL=https://github.com/libvips/libvips/releases/download

WORKDIR /usr/local/libvips/src
# Download and extract libvips source code
ADD ${VIPS_URL}/v${VIPS_VERSION}/vips-${VIPS_VERSION}.tar.xz /usr/local/libvips/src/
RUN tar xf vips-${VIPS_VERSION}.tar.xz;

WORKDIR /usr/local/libvips/src/vips-${VIPS_VERSION}

# Configure libvips
RUN meson setup build --prefix /usr/local/libvips --libdir=lib -Ddeprecated=false -Dintrospection=disabled -Dmodules=disabled -Dexamples=false

WORKDIR /usr/local/libvips/src/vips-${VIPS_VERSION}/build

# Compile and install libvips
RUN ninja && ninja install

# Create temporary ffmpeg specific build layer
FROM media-build AS ffmpeg

# ffmpeg version to compile, change with [--build-arg FFMPEG_VERSION="7.0.x"]
# renovate: datasource=github-tags depName=FFmpeg/FFmpeg extractVersion=^n(?<version>\d+\.\d+(\.\d+)?)$
ARG FFMPEG_VERSION=8.1.1
# ffmpeg download URL, change with [--build-arg FFMPEG_URL="https://ffmpeg.org/releases"]
ARG FFMPEG_URL=https://github.com/FFmpeg/FFmpeg/archive/refs/tags

WORKDIR /usr/local/ffmpeg/src
# Download and extract ffmpeg source code
ADD ${FFMPEG_URL}/n${FFMPEG_VERSION}.tar.gz /usr/local/ffmpeg/src/
RUN tar xf n${FFMPEG_VERSION}.tar.gz && mv FFmpeg-n${FFMPEG_VERSION} ffmpeg-${FFMPEG_VERSION};

WORKDIR /usr/local/ffmpeg/src/ffmpeg-${FFMPEG_VERSION}

# Configure and compile ffmpeg
RUN \
  ./configure \
  --prefix=/usr/local/ffmpeg \
  --toolchain=hardened \
  --disable-debug \
  --disable-devices \
  --disable-doc \
  --disable-ffplay \
  --disable-network \
  --disable-static \
  --enable-ffmpeg \
  --enable-ffprobe \
  --enable-gpl \
  --enable-libdav1d \
  --enable-libmp3lame \
  --enable-libopus \
  --enable-libsnappy \
  --enable-libvorbis \
  --enable-libvpx \
  --enable-libwebp \
  --enable-libx264 \
  --enable-libx265 \
  --enable-shared \
  --enable-version3 \
  ; \
  make -j"$(nproc)"; \
  make install;

# Build Python dependencies with uv
FROM python AS python-deps

ARG TARGETPLATFORM

# hadolint ignore=DL3008
RUN \
  --mount=type=cache,id=apt-cache-${TARGETPLATFORM},target=/var/cache/apt,sharing=locked \
  --mount=type=cache,id=apt-lib-${TARGETPLATFORM},target=/var/lib/apt,sharing=locked \
  apt-get install -y --no-install-recommends \
  build-essential \
  libpq-dev \
  libssl-dev \
  ;

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
  UV_LINK_MODE=copy \
  VIRTUAL_ENV=/opt/venv

# Copy libvips headers/libs needed by pyvips at install time
COPY --from=libvips /usr/local/libvips/lib /usr/local/lib
COPY --from=libvips /usr/local/libvips/include /usr/local/include

RUN ldconfig

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
  # Install Node.js packages (production only)
  yarn workspaces focus --production @mastodon/mastodon;

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

# Copy compiled frontend assets
COPY --from=assets /opt/mastodon/public/packs /opt/mastodon/public/packs
COPY --from=assets /opt/mastodon/public/assets /opt/mastodon/public/assets
# Copy installed Python virtualenv
COPY --from=python-deps /opt/venv /opt/venv
# Copy uv for runtime use (migrations, etc.)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# Copy libvips components
COPY --from=libvips /usr/local/libvips/bin /usr/local/bin
COPY --from=libvips /usr/local/libvips/lib /usr/local/lib
# Copy ffmpeg components
COPY --from=ffmpeg /usr/local/ffmpeg/bin /usr/local/bin
COPY --from=ffmpeg /usr/local/ffmpeg/lib /usr/local/lib

RUN \
  ldconfig; \
  # Smoketest media processors
  vips -v; \
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
