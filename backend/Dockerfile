# Railway Deployment Dockerfile - Cold Email Infrastructure Platform
# PINNED to Debian Bookworm for PowerShell compatibility

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # PID 1 init so Chromium/Chromedriver children are reaped in Railway
    dumb-init \
    # Chrome/Chromium for Selenium
    chromium \
    chromium-driver \
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
