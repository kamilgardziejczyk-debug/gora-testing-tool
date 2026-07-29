# syntax=docker/dockerfile:1
#
# Gora testing tool - HIL test node image.
#
# Targets a Raspberry Pi 4 test node (linux/arm64). This layer covers the GPIO
# (!GpioControl), serial (!ProgramEsptool, !SubghzSim), BLE (!BleCentral) and
# J-Link (!ProgramJlink) scenarios; MQTT support is added in a later step.
#
# Python is pinned at 3.11 because the codebase uses PEP 604 unions in
# evaluated positions - e.g. `-> Wrapper | None` in parser/parser.py - which
# require 3.10 or newer at import time.
#
# Also bundles the GitHub Actions self-hosted runner. When GH_PAT/GH_REPO are
# set at `docker run` time, entrypoint.sh registers this container as a
# runner and workflow job steps (e.g. `python main.py -t scenario.yml`) run
# inside it directly; otherwise it falls back to the one-shot CLI behavior
# below. See RUNNER_VERSION for the pinned release - check
# https://github.com/actions/runner/releases for newer ones.

ARG PYTHON_VERSION=3.11

# ---------------------------------------------------------------------------
# Stage 1: build the virtualenv.
#
# RPi.GPIO is a C extension with no prebuilt arm64 wheel, so it is compiled
# here and only the finished venv is copied forward. That keeps the compiler
# out of the runtime image.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

# Requirements are copied ahead of the source so editing a wrapper does not
# invalidate the slow dependency layer. requirements.txt pulls in the per-tool
# files with `-r`, so those have to be present at install time too.
COPY requirements.txt requirements-rpi.txt ./
COPY tools/ble_gatt/requirements.txt tools/ble_gatt/
COPY tools/mqtt_listener/requirements.txt tools/mqtt_listener/
COPY tools/subghz_sim/requirements.txt tools/subghz_sim/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-rpi.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG RUNNER_VERSION=2.336.0
ARG RUNNER_ARCH=arm64

# tzdata lets `-e TZ=Europe/Dublin` give the HTML report local timestamps;
# without it every report row is stamped UTC regardless of where the node is.
# curl/jq/ca-certificates are for the runner tarball download below and for
# entrypoint.sh's registration-token API calls at container start.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        curl \
        jq \
        ca-certificates \
        sudo \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

# The GitHub Actions self-hosted runner. installdependencies.sh apt-get
# installs the .NET-runtime-adjacent libs (libicu, libssl, libkrb5, etc.)
# itself, so they aren't enumerated here.
RUN mkdir -p /opt/actions-runner \
    && cd /opt/actions-runner \
    && curl -fsSL -o runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz" \
    && tar xzf runner.tar.gz \
    && rm runner.tar.gz \
    && ./bin/installdependencies.sh \
    && rm -rf /var/lib/apt/lists/*

# The runner refuses to run as root unless told to; this container already
# runs as root for the gpio/dialout reasons below, so opt in explicitly.
ENV RUNNER_ALLOW_RUNASROOT=1

# SEGGER J-Link tools (JLinkExe, used by !ProgramJlink). Fetched unversioned
# from SEGGER's own "latest" URL - the vendor doesn't publish a stable
# versioned URL for arm64, so a rebuild can pick up a newer J-Link release;
# `dpkg -s jlink` inside the container shows exactly which one landed.
# `-d accept_license_agreement=accepted` is SEGGER's documented way to skip
# the interactive EULA click-through for scripted installs.
#
# The .deb's postinst reloads udev rules via udevadm to pick up already-
# connected probes; udevadm doesn't exist in this slim image (no udev
# daemon), so the real call would fail the whole install. Devices instead
# reach the container via `-v /dev/bus/usb:/dev/bus/usb --privileged` (see
# README), so nothing here actually depends on live udev rule reloading -
# a no-op stub is enough to let the install finish.
RUN echo '#!/bin/sh' > /usr/bin/udevadm \
    && echo 'exit 0' >> /usr/bin/udevadm \
    && chmod +x /usr/bin/udevadm \
    && curl -fsSL -o /tmp/jlink.deb -d accept_license_agreement=accepted \
        https://www.segger.com/downloads/jlink/JLink_Linux_arm64.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/jlink.deb \
    && rm -f /tmp/jlink.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Runs as root deliberately. With /dev bind-mounted, root avoids having to
# match the host's dialout and gpio GIDs, which vary between Raspberry Pi OS
# releases and would otherwise need pinning per node.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--help"]
