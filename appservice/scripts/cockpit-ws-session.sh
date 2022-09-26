#!/bin/sh
# cockpit session container entry point
set -eux

# we cannot write to /etc as unprivileged user on k8s
mkdir -p /tmp/conf/cockpit
printf "[Webservice]\nUrlRoot=${ROUTE_WSS}/sessions/${SESSION_ID}/web\nOrigins = ${API_URL}\n" > /tmp/conf/cockpit/cockpit.conf
export XDG_CONFIG_DIRS=/tmp/conf
exec /usr/libexec/cockpit-ws --for-tls-proxy --local-session=/websocket-session.py
