#!/bin/sh
# Container entrypoint: gives the automation browser a screen on a headless VM.
#
#   DISPLAY_MODE=xvfb (default)  Chromium runs HEADED on a virtual X display
#                                (Xvfb). x11vnc shares that display, and noVNC
#                                serves it as a web page, so the browser can be
#                                watched (and used, e.g. to solve a captcha) from
#                                any browser:  http://<vm-ip>:<NOVNC_PORT>/vnc.html
#   DISPLAY_MODE=headless        no display at all; set PLAYWRIGHT_HEADLESS=true.
#
# Everything after the entrypoint (the image's CMD) is the app; it is exec'd
# once the display is up.
set -eu

DISPLAY_MODE="${DISPLAY_MODE:-xvfb}"
XVFB_DISPLAY="${XVFB_DISPLAY:-:99}"
XVFB_SCREEN="${XVFB_SCREEN:-1600x900x24}"
VNC_ENABLED="${VNC_ENABLED:-true}"
VNC_PASSWORD="${VNC_PASSWORD:-}"
NOVNC_ENABLED="${NOVNC_ENABLED:-true}"

# A Chrome profile remembers the host that last opened it in Singleton* files.
# A new container is a new hostname, so Chrome would refuse the profile as "in
# use by another computer". Nothing else can be running it yet: drop the locks.
for dir in ${CHROME_PROFILE_DIRS:-}; do
    [ -d "$dir" ] && find "$dir" -maxdepth 3 -name 'Singleton*' -exec rm -f {} + 2>/dev/null || true
done

if [ "$DISPLAY_MODE" = "headless" ]; then
    echo "Display mode: headless (no virtual display, nothing to watch)."
    exec "$@"
fi

echo "Display mode: xvfb ($XVFB_DISPLAY, $XVFB_SCREEN)."
rm -f "/tmp/.X${XVFB_DISPLAY#:}-lock"
Xvfb "$XVFB_DISPLAY" -screen 0 "$XVFB_SCREEN" -nolisten tcp -ac >/tmp/xvfb.log 2>&1 &
export DISPLAY="$XVFB_DISPLAY"

SOCKET="/tmp/.X11-unix/X${XVFB_DISPLAY#:}"
i=0
while [ ! -S "$SOCKET" ] && [ "$i" -lt 100 ]; do
    i=$((i + 1)); sleep 0.1
done
if [ ! -S "$SOCKET" ]; then
    echo "Xvfb did not start:" >&2
    cat /tmp/xvfb.log >&2 || true
    exit 1
fi

# A window manager, so --start-maximized fills the screen and windows can be
# moved/raised from the viewer. Without one every window sits at 0,0 unframed.
openbox >/tmp/openbox.log 2>&1 &

if [ "$VNC_ENABLED" = "true" ]; then
    if [ -n "$VNC_PASSWORD" ]; then
        mkdir -p "$HOME/.vnc"
        x11vnc -storepasswd "$VNC_PASSWORD" "$HOME/.vnc/passwd" >/dev/null 2>&1
        VNC_AUTH="-rfbauth $HOME/.vnc/passwd"
    else
        VNC_AUTH="-nopw"
        echo "WARNING: VNC has no password - set VNC_PASSWORD in .env before exposing the viewer ports." >&2
    fi
    # -forever: keep serving after a viewer leaves; -shared: several viewers at once.
    # shellcheck disable=SC2086
    x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 $VNC_AUTH \
        -noxdamage -bg -o /tmp/x11vnc.log >/dev/null 2>&1 \
        || echo "WARNING: x11vnc failed to start (see /tmp/x11vnc.log)." >&2

    if [ "$NOVNC_ENABLED" = "true" ]; then
        websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
        echo "Browser view: http://<vm-ip>:${NOVNC_PORT:-6080}/vnc.html   (VNC client: <vm-ip>:${VNC_PORT:-5900})"
    fi
fi

exec "$@"
