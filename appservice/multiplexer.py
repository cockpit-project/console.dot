import os
import socket
import signal
import subprocess
import uuid
import http
import http.client
import json
import time

import redis

from multiprocessing import Process
from http.server import BaseHTTPRequestHandler, HTTPServer

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

        location /api/webconsole/v1/sessions/new {{
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
# nginx PID
PID = 0
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
            location /session/{sessionid} {{
                proxy_pass http://session-{sessionid}:9090/api/cockpit-9090/{sessionid};
            }}
    """

    open('/etc/nginx/nginx.conf', 'w').write(NGINX_TEMPLATE.format(routes=routes))


class ProxyHTTPRequestHandler(BaseHTTPRequestHandler):
    # protocol_version = 'HTTP/1.0'

    def do_GET(self):
        if self.path == "/api/webconsole/v1/sessions/new":
            sessionid = str(uuid.uuid4())
            name = f'session-{sessionid}'
            connection = http.client.HTTPConnection('localhost')
            connection.sock = socket.socket(socket.AF_UNIX)
            connection.sock.connect(PODMAN_SOCKET)
            body = {
                    'image': 'quay.io/rhn_engineering_mpitt/ws',
                    'name': name,
                    'command': ['sleep', 'infinity'],
                    'remove': True,
                    'netns': {'nsmode': 'bridge'},
                    'Networks': {'consoledot': {}},
            }
            connection.request('POST', '/v1.12/libpod/containers/create', body=json.dumps(body))
            response = connection.getresponse()
            content = response.read()
            print(response.status, content)
            connection.request('POST', f'/v1.12/libpod/containers/{name}/start')
            response = connection.getresponse()

            sessions = get_sessions()
            sessions[sessionid] = True

            dumped_sessions = json.dumps(sessions)
            REDIS.set('sessions', dumped_sessions)
            REDIS.publish('sessions', dumped_sessions)

            self.send_response(200)
            self.end_headers()
            if response.status != 200:
                self.wfile.write(content)
            else:
                self.wfile.write(f"container created {name}\r\n".encode('utf-8'))

            return

        self.send_response(404, 'Not found')

def watch_redis():
    p = REDIS.pubsub()
    p.subscribe("sessions")

    while True:
        message = p.get_message()
        if message:
            print(message)
            sessions = get_sessions()
            write_routes(sessions)
            os.kill(PID, signal.SIGHUP)
        time.sleep(0.01)


if __name__ == '__main__':
    write_routes(get_sessions())
    proc = subprocess.Popen(['nginx'])
    status = proc.poll()
    print("nginx status", status)
    time.sleep(1)
    status = proc.poll()
    print("nginx status", status)
    PID = proc.pid
    server_address = ('0.0.0.0', 8081)

    # start redis watcher
    p = Process(target=watch_redis)
    p.start()

    httpd = HTTPServer(server_address, ProxyHTTPRequestHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
