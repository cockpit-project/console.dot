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

from multiprocessing import Process
from http.server import BaseHTTPRequestHandler, HTTPServer

import redis

import config

logger = logging.getLogger("multiplexer")

PORT_3SCALE = os.getenv('PORT_3SCALE')

NGINX_TEMPLATE = """
daemon off;
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
        listen   8080 default_server;
        listen   [::]:8080 default_server;

        server_name localhost;

        {routes}

        location {route_control} {{
            proxy_pass http://localhost:8081;
        }}

        location / {{
            return 404 'no route found in multiplexer\r\n';
        }}
      }}
}}
"""

PODMAN_SOCKET = '/run/podman/podman.sock'
NGINX_PROC = None
# Pods
SESSIONS = {}
REDIS = redis.Redis(host=os.environ["REDIS_SERVICE_HOST"], port=int(os.environ.get("REDIS_SERVICE_PORT", "6379")))


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
location {config.ROUTE_WSS}/sessions/{sessionid}/web {{
    proxy_pass http://session-{sessionid}:9090;

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
location {config.ROUTE_WSS}/sessions/{sessionid}/ws {{
    proxy_pass http://session-{sessionid}:8080;

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

    with open('/etc/nginx/nginx.conf', 'w') as f:
        f.write(NGINX_TEMPLATE.format(routes=routes, route_control=config.ROUTE_API))


class ProxyHTTPRequestHandler(BaseHTTPRequestHandler):
    def new_session_podman(self, sessionid):
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
                            f"printf '[Webservice]\nUrlRoot={config.ROUTE_WSS}/sessions/{sessionid}/web\\n"
                            f"Origins = https://localhost:{PORT_3SCALE} http://localhost:8080\\n'"
                            "> /etc/cockpit/cockpit.conf;"
                            "exec /usr/libexec/cockpit-ws --for-tls-proxy --local-session=socat-session.sh"],
                'remove': True,
                'netns': {'nsmode': 'bridge'},
                # deprecated; use this with podman ≥ 4: 'Networks': {'consoledot': {}},
                'cni_networks': ['consoledot'],
        }
        connection.request('POST', '/v1.12/libpod/containers/create', body=json.dumps(body))
        response = connection.getresponse()
        content = response.read()

        if response.status >= 200 and response.status < 300:
            logger.debug("/new: creating container result: %i %s", response.status, content.decode())
            connection.request('POST', f'/v1.12/libpod/containers/{name}/start')
            response = connection.getresponse()
            content = response.read()

        return response, content

    def new_session(self):
        sessionid = str(uuid.uuid4())

        if os.path.exists(PODMAN_SOCKET):
            response, content = self.new_session_podman(sessionid)
        else:
            # TODO: support k8s API
            raise NotImplementedError("cannot create sessions other than podman")

        if response.status >= 200 and response.status < 300:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"id": sessionid}).encode())
        else:
            self.send_response(response.status)
            self.end_headers()
            self.wfile.write("creating session container failed: ".encode())
            self.wfile.write(content)

        sessions = get_sessions()
        sessions[sessionid] = True

        dumped_sessions = json.dumps(sessions)
        REDIS.set('sessions', dumped_sessions)
        REDIS.publish('sessions', dumped_sessions)

    def ping(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'pong')

    def do_GET(self):
        logger.debug("GET %s", self.path)
        if self.path == f"{config.ROUTE_API}/sessions/new":
            self.new_session()
        if self.path == f"{config.ROUTE_API}/ping":
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
            os.kill(NGINX_PROC.pid, signal.SIGHUP)
        time.sleep(0.01)


def start_nginx():
    proc = subprocess.Popen(['nginx'])

    # wait for nginx to start up
    connection = http.client.HTTPConnection('localhost:8080')
    for _ in range(10):
        try:
            connection.connect()
            break
        except OSError:
            time.sleep(0.2)
    else:
        raise TimeoutError('timed out waiting for nginx to start up')
    return proc


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    write_routes(get_sessions())
    NGINX_PROC = start_nginx()

    # start redis watcher
    redis = Process(target=watch_redis)
    redis.start()

    server_address = ('0.0.0.0', 8081)
    httpd = HTTPServer(server_address, ProxyHTTPRequestHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        NGINX_PROC.kill()
        NGINX_PROC.wait()
