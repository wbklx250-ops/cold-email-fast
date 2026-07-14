# Railway Deployment Dockerfile - Cold Email Infrastructure Platform
# PINNED to Debian Bookworm for PowerShell compatibility

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # PID 1 init so Chromium/Chromedriver children are reaped in Railway
    dumb-init \
    # Chrome/Chromium runtime dependencies for Selenium. The actual browser
    # packages are pinned below; the current Bookworm Chromium 150 build crashes
    # headless in Railway with Trace/breakpoint trap.
    # General utilities
    wget \
    curl \
    gnupg \
    ca-certificates \
    apt-transport-https \
    software-properties-common \
    # Build tools for Python packages
    gcc \
    libpq-dev \
    # Chrome dependencies
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxkbcommon0 \
    libxshmfence1 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Pin Chromium + ChromeDriver together. Letting apt install the latest package
# pulled Chromium 150.0.7871.46, which crashes before Selenium can create a
# session in the Railway container. 149.0.7827.196 was the last known-good pair.
# Use Debian's immutable snapshot because superseded versions are removed from
# the regular deb.debian.org package pool and make otherwise repeatable builds
# fail with HTTP 404.
ARG CHROMIUM_VERSION=149.0.7827.196-1~deb12u1
ARG CHROMIUM_DEB_BASE=https://snapshot.debian.org/archive/debian-security/20260626T014759Z/pool/updates/main/c/chromium
RUN set -eux; \
    wget -O /tmp/chromium-common.deb "$CHROMIUM_DEB_BASE/chromium-common_${CHROMIUM_VERSION}_amd64.deb"; \
    wget -O /tmp/chromium-sandbox.deb "$CHROMIUM_DEB_BASE/chromium-sandbox_${CHROMIUM_VERSION}_amd64.deb"; \
    wget -O /tmp/chromium.deb "$CHROMIUM_DEB_BASE/chromium_${CHROMIUM_VERSION}_amd64.deb"; \
    wget -O /tmp/chromium-driver.deb "$CHROMIUM_DEB_BASE/chromium-driver_${CHROMIUM_VERSION}_amd64.deb"; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        /tmp/chromium-common.deb \
        /tmp/chromium-sandbox.deb \
        /tmp/chromium.deb \
        /tmp/chromium-driver.deb; \
    rm -rf /var/lib/apt/lists/* /tmp/chromium*.deb; \
    chromium --version; \
    chromedriver --version

# Install PowerShell Core (Debian 12 Bookworm)
RUN curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/microsoft.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends powershell \
    && rm -rf /var/lib/apt/lists/*

# Install PowerShell modules for Exchange Online (pinned for stability)
RUN pwsh -Command "Set-PSRepository -Name PSGallery -InstallationPolicy Trusted" \
    && pwsh -Command "Install-Module -Name ExchangeOnlineManagement -RequiredVersion 3.1.0 -Force -Scope AllUsers -AcceptLicense -AllowClobber"

# Set environment paths
ENV CHROME_PATH=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV PWSH_PATH=/usr/bin/pwsh
ENV EXO_REQUIRED_VERSION=3.1.0

WORKDIR /app

RUN mkdir -p /tmp/screenshots /app/logs

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV SCREENSHOT_DIR=/tmp/screenshots
ENV LOG_DIR=/app/logs
ENV HEADLESS_MODE=true

EXPOSE 8000

# start.sh is copied via `COPY . .` above (build context is backend/)
RUN chmod +x start.sh

ENTRYPOINT ["dumb-init", "--"]
CMD ["./start.sh"]
