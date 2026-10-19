# meetup-bot — FastAPI backend + static frontend, with Playwright/Chromium.
#
# Base image ships Python + Playwright + the browser builds preinstalled, and the
# tag is pinned to the same Playwright version as requirements.txt so the library
# and the bundled browsers never drift apart.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

# A screen for a headless VM: Xvfb is the virtual display Chromium runs headed
# on, openbox manages its windows, x11vnc shares the display and noVNC serves it
# as a web page — so the automation browser can be watched from any browser.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        xvfb x11vnc openbox novnc websockify tzdata fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# Deps first so this layer is cached until requirements.txt actually changes.
# pywinauto/pyautogui are marked win32-only in requirements.txt, so pip skips
# them here (they need a Windows desktop; the desktop-cursor feature is off in a
# container, everything else runs on Playwright).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code (frontend/ is served by the same server — see backend/main.py).
COPY . .
RUN sed -i 's/\r$//' docker/entrypoint.sh && chmod +x docker/entrypoint.sh

# Headed Chromium on the virtual display (see docker/entrypoint.sh). Runs as
# root in the container, so Chromium needs --no-sandbox (PLAYWRIGHT_NO_SANDBOX).
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=4200 \
    ENV=production \
    DISPLAY_MODE=xvfb \
    PLAYWRIGHT_HEADLESS=false \
    PLAYWRIGHT_CHANNEL=chromium \
    PLAYWRIGHT_NO_SANDBOX=true \
    CHROME_PROFILE_DIRS="/app/backend/data/chrome-profile /app/backend/data/profiles"

# 4200 app, 5900 VNC, 6080 noVNC (browser view of the automation windows)
EXPOSE 4200 5900 6080

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "4200"]
