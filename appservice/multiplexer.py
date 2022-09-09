import os
import socket
import signal
import subprocess
import uuid
import http
import http.client
import json
import time
import logging

import redis

from multiprocessing import Process
from http.server import BaseHTTPRequestHandler, HTTPServer


logger = logging.getLogger("multiplexer")


NGINX_TEMPLATE = """
daemon off;
user www-data;
worker_processes  auto;

events {{
    worker_connections  1024;
}}

http {{
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for"';

    access_log  /var/log/nginx/access.log  main;
    error_log  stderr;

    sendfile        on;

    keepalive_timeout  65;

    server {{
        listen   80;

        server_name localhost;

        {routes}

        location /api/webconsole/ {{
            proxy_pass http://localhost:8081;
        }}

        location / {{
            return 404 'no route found in multiplexer \r\n';
        }}
      }}
}}
"""

PODMAN_SOCKET = '/run/podman/podman.sock'
# redis hostname - pod name - container name
REDIS_HOST = 'webconsoleapp-redis'
NGINX_PID = 0
# Pods
SESSIONS = {}
REDIS = redis.Redis(host=REDIS_HOST)


def get_sessions():
    # Add new entry to our sessions
    sessions = REDIS.get('sessions')
    if sessions is None:
        sessions = {}
    else:
        try:
            sessions = json.loads(sessions)
        except json.decoder.JSONDecodeError:
            sessions = {}

    return sessions


def write_routes(sessions):
    routes = ""
    for sessionid in sessions:
        routes += f"""
location /wss/webconsole-http/v1/sessions/{sessionid} {{
    proxy_pass http://session-{sessionid}:9090/wss/webconsole-http/v1/sessions/{sessionid};

    # Required to proxy the connection to Cockpit
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Required for web sockets to function
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    # Pass ETag header from Cockpit to clients.
    # See: https://github.com/cockpit-project/cockpit/issues/5239
    gzip off;
}}
location /wss/webconsole-ws/v1/sessions/{sessionid} {{
    proxy_pass http://session-{sessionid}:8080/wss/webconsole-ws/v1/sessions/{sessionid};

    # Required to proxy the connection to Cockpit
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Required for web sockets to function
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    # Pass ETag header from Cockpit to clients.
    # See: https://github.com/cockpit-project/cockpit/issues/5239
    gzip off;
}}"""

    open('/etc/nginx/nginx.conf', 'w').write(NGINX_TEMPLATE.format(routes=routes))


class ProxyHTTPRequestHandler(BaseHTTPRequestHandler):
    # protocol_version = 'HTTP/1.0'

    def new_session(self):
        sessionid = str(uuid.uuid4())
        name = f'session-{sessionid}'
        connection = http.client.HTTPConnection('localhost')
        connection.sock = socket.socket(socket.AF_UNIX)
        connection.sock.connect(PODMAN_SOCKET)
        body = {
                'image': 'quay.io/rhn_engineering_mpitt/ws',
                'name': name,
                # for local debugging
                # 'command': ['sleep', 'infinity'],
                # XXX: http://localhost:8080 origin is for directly connecting to appservice, without 3scale
                'command': ['sh', '-exc',
                            f"printf '[Webservice]\nUrlRoot=/wss/webconsole-http/v1/sessions/{sessionid}/\\n"
                            "Origins = https://localhost:8443 http://localhost:8080\\n' > /etc/cockpit/cockpit.conf;"
                            "exec /usr/libexec/cockpit-ws --for-tls-proxy --local-session=socat-session.sh"],
                'remove': True,
                'netns': {'nsmode': 'bridge'},
                'Networks': {'consoledot': {}},
        }
        connection.request('POST', '/v1.12/libpod/containers/create', body=json.dumps(body))
        response = connection.getresponse()
        content = response.read()
        logger.debug("/new: creating container result: %i %s", response.status, content.decode())
        connection.request('POST', f'/v1.12/libpod/containers/{name}/start')
        response = connection.getresponse()

        sessions = get_sessions()
        sessions[sessionid] = True

        dumped_sessions = json.dumps(sessions)
        REDIS.set('sessions', dumped_sessions)
        REDIS.publish('sessions', dumped_sessions)

        if response.status >= 200 and response.status < 300:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"id": sessionid}).encode())
        else:
            self.send_response(response.status)
            self.end_headers()
            self.wfile.write("creating container failed: ".encode())
            self.wfile.write(content)

    def ping(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'pong')

    def do_GET(self):
        if self.path == "/api/webconsole/v1/sessions/new":
            self.new_session()
        if self.path == "/api/webconsole/v1/ping":
            self.ping()
        else:
            self.send_response(404, 'Not found')


def watch_redis():
    redis = REDIS.pubsub()
    redis.subscribe("sessions")
    logger = logging.getLogger("multiplexer/redis")

    while True:
        message = redis.get_message()
        if message:
            logger.debug("got message: %s", message)
            sessions = get_sessions()
            write_routes(sessions)
            os.kill(NGINX_PID, signal.SIGHUP)
        time.sleep(0.01)


if __name__ == '__main__':
    write_routes(get_sessions())
    proc = subprocess.Popen(['nginx'])
    NGINX_PID = proc.pid

    logging.basicConfig(level=logging.DEBUG)

    # start redis watcher
    redis = Process(target=watch_redis)
    redis.start()

    server_address = ('0.0.0.0', 8081)
    httpd = HTTPServer(server_address, ProxyHTTPRequestHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
