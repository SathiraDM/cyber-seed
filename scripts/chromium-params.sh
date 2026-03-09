#!/bin/sh

set -u # Treat unset variables as an error.

if ! check_pid_namespace >/dev/null; then
    printf "%s\n" "--no-sandbox"
fi

printf "%s\n" "--disable-dev-shm-usage"
printf "%s\n" "--ignore-gpu-blocklist"
printf "%s\n" "--simulate-outdated-no-au='Tue, 31 Dec 2099 23:59:59 GMT'"
printf "%s\n" "--start-maximized"
printf "%s\n" "--user-data-dir=/config/chromium"

# Enable remote debugging so scripts can intercept video URLs via CDP
printf "%s\n" "--remote-debugging-port=9222"
printf "%s\n" "--remote-debugging-address=0.0.0.0"

if [ -n "${CHROMIUM_APP_URL:-}" ]; then
    printf "%s\n" "--app=$CHROMIUM_APP_URL"
fi

# vim:ft=sh:ts=4:sw=4:et:sts=4
