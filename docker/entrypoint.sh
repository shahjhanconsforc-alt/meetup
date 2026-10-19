#!/bin/sh
# Container entrypoint.
#
#   DISPLAY_MODE=host (default)  the browser opens HEADED on the VM's own desktop.
#                                The host's X socket (/tmp/.X11-unix) is mounted
#                                in and DISPLAY points at it (e.g. :0).
#   DISPLAY_MODE=headless        no display at all; the browser runs headless.
#
# Everything after the entrypoint (the image's CMD) is the app; it is exec'd
# once the display is reachable.
set -eu

DISPLAY_MODE="${DISPLAY_MODE:-host}"

# A Chrome profile remembers the host that last opened it in Singleton* files.
# A new container is a new hostname, so Chrome would refuse the profile as "in
# use by another computer". Nothing else can be running it yet: drop the locks.
for dir in ${CHROME_PROFILE_DIRS:-}; do
    [ -d "$dir" ] && find "$dir" -maxdepth 3 -name 'Singleton*' -exec rm -f {} + 2>/dev/null || true
done

if [ "$DISPLAY_MODE" = "headless" ]; then
    echo "Display mode: headless (no window)."
    unset DISPLAY
    exec "$@"
fi

if [ -z "${DISPLAY:-}" ]; then
    echo "DISPLAY_MODE=host needs DISPLAY (e.g. :0) - set DISPLAY in .env, or use DISPLAY_MODE=headless." >&2
    exit 1
fi

# Wait for the desktop's X socket: after a reboot the container can start
# before anyone has logged in to the desktop.
DISPLAY_NUM="${DISPLAY#*:}"; DISPLAY_NUM="${DISPLAY_NUM%%.*}"     # ":0.0" -> "0"
SOCKET="/tmp/.X11-unix/X${DISPLAY_NUM}"
i=0
while [ ! -S "$SOCKET" ] && [ "$i" -lt 600 ]; do
    [ "$i" -eq 0 ] && echo "Waiting for the desktop's X display ($SOCKET) - log in to the VM desktop ..."
    i=$((i + 1)); sleep 1
done
[ -S "$SOCKET" ] || echo "WARNING: $SOCKET not found; the browser window will fail to open." >&2

echo "Display mode: host (DISPLAY=$DISPLAY). The browser opens on the VM desktop."
exec "$@"
